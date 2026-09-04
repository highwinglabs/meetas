"""Full-text/hybrid/RAG search and the LLM chat endpoints. (part of MeetingService)."""
from __future__ import annotations

from sqlalchemy import func, select
from core.audio.devices import resolve_system_audio_device
from core.integrity import run_integrity_check
from core.jobs.queue import JobQueue
from core.llm import LLMError
from core.llm.output import visible_answer
from core.search.hybrid import hybrid_search
from core.search.rag import rag_answer
from core.search.vectorstore import embed_meeting as _embed_meeting
from core.providers import ASRError
from core.store.db import session_scope
from core.store.fts import ensure_fts, search as fts_search
from core.store.models import Meeting, TranscriptSegment, Project
from core.logging_setup import get_logger
from core.services._common import UnknownMeetingError

_GENERAL_CHAT_SYSTEM = (
    "Du bist ein hilfreicher, sachlicher allgemeiner Assistent. Beantworte die "
    "Frage direkt auf Deutsch, sofern keine andere Sprache gewuenscht wird. "
    "Dir werden keine Meeting-Transkripte und keine Internetquellen bereitgestellt. "
    "Behaupte deshalb weder, eine Antwort aus Meetings abzuleiten, noch aktuelle "
    "Informationen online geprueft zu haben. Mache Unsicherheit klar kenntlich."
)


log = get_logger("ma.service")


class SearchMixin:

    def feature_flags(self) -> dict:
        """Phase-5 feature availability for the UI (no side effects, no network)."""
        live_ready = False
        if self.config.live_transcription:
            le = self._live_engine()
            live_ready = bool(le is not None and le.is_ready())
        # System-audio (loopback / what-you-hear) is only offered when the user
        # enabled it AND a matching capture device actually exists. Never raises.
        system_audio = bool(
            self.config.system_audio_enabled and resolve_system_audio_device() is not None
        )
        return {
            "live_transcription": bool(self.config.live_transcription),
            "speaker_diarization": bool(self.config.speaker_diarization),
            "live_ready": live_ready,
            "diarization_engine": self._diar_engine().name,
            "system_audio": system_audio,
        }

    def integrity_report(self) -> dict:
        """Run a read-only consistency report for the local workspace."""
        with session_scope() as s:
            return run_integrity_check(self.config, s)

    def search(self, query: str, limit: int = 25, project_id: str | None = None,
               meeting_id: str | None = None,
               meeting_ids: list[str] | None = None) -> list[dict]:
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            limit = 25
        with session_scope() as s:
            ensure_fts(s)
            # The REST shape sends ``[]`` for the unfiltered scope; only a
            # non-empty list represents an explicit meeting selection.
            allowed_ids = set(meeting_ids) if meeting_ids else None
            if meeting_id:
                meeting = s.get(Meeting, meeting_id)
                if meeting is None or meeting.deleted_at is not None:
                    raise UnknownMeetingError(meeting_id)
            if allowed_ids is not None:
                existing = set(s.scalars(select(Meeting.id).where(
                    Meeting.id.in_(allowed_ids), Meeting.deleted_at.is_(None))).all())
                if existing != allowed_ids:
                    missing = next(iter(allowed_ids - existing), None)
                    raise UnknownMeetingError(missing or "")
            if project_id:
                project = s.get(Project, project_id)
                if project is None or project.deleted_at is not None:
                    raise KeyError(project_id)
                allowed = set(s.scalars(select(Meeting.id).where(
                    Meeting.project_id == project_id,
                    Meeting.deleted_at.is_(None))).all())
                allowed_ids = allowed if allowed_ids is None else allowed_ids & allowed
            if meeting_id is not None and allowed_ids is not None and meeting_id not in allowed_ids:
                return []
            hits = fts_search(s, query, limit=limit, meeting_id=meeting_id,
                              meeting_ids=allowed_ids if meeting_id is None and
                              allowed_ids is not None else None)
            return hits

    def embed_meeting(self, meeting_id: str) -> dict:
        """Compute + store a local vector for every segment of the meeting.

        Offline (hashing backend) by default; idempotent. Requires a transcript.
        """
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            n_seg = s.scalar(select(func.count()).select_from(TranscriptSegment)
                             .where(TranscriptSegment.meeting_id == meeting_id)) or 0
            if n_seg == 0:
                raise ASRError("Kein Transkript vorhanden -- bitte zuerst transkribieren.")
            engine = self._embed_manager.engine()
            n = _embed_meeting(s, meeting_id, engine)
            job = JobQueue.get_or_create(s, meeting_id, "embed")
            JobQueue.mark_done(s, job)
            s.commit()
        return {"meeting_id": meeting_id, "status": "done", "embeddings": n,
                "backend": engine.name, "dim": int(engine.dim)}

    def search_hybrid(self, query: str, limit: int = 25,
                      meeting_id: str | None = None,
                      project_id: str | None = None,
                      meeting_ids: list[str] | None = None) -> list[dict]:
        """FTS5 + vector hybrid search (RRF-fused). Falls back to FTS alone when
        no vector index exists yet for the searched scope."""
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            limit = 25
        engine = self._embed_manager.engine()
        with session_scope() as s:
            ensure_fts(s)
            return hybrid_search(s, query, engine, self.config, limit=limit,
                                 meeting_id=meeting_id, project_id=project_id,
                                 meeting_ids=meeting_ids)

    def ask(self, query: str, limit: int = 25,
            meeting_id: str | None = None, project_id: str | None = None,
            meeting_ids: list[str] | None = None,
            model_name: str | None = None,
            history: list[dict] | None = None) -> dict:
        """Grounded RAG: retrieve segments for `query` and let the local LLM
        (``analyse`` profile) answer strictly from that context, with sources."""
        try:
            limit = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            limit = 25
        if not self.config.rag_enabled:
            return {"answer": None, "error": "RAG ist in der Konfiguration deaktiviert."}
        retrieval_query = query
        if history:
            previous_questions = [
                str(turn.get("content") or "").strip()
                for turn in history if turn.get("role") == "user"
            ]
            if previous_questions:
                # Include the last question for pronoun-style follow-ups while
                # keeping the current question as the primary search intent.
                retrieval_query = f"{previous_questions[-1][:2000]} {query}"
        with session_scope() as s:
            ensure_fts(s)
            hits = hybrid_search(s, retrieval_query, self._embed_manager.engine(), self.config,
                                 limit=limit, meeting_id=meeting_id,
                                 project_id=project_id, meeting_ids=meeting_ids)
            if project_id and meeting_id is None and not meeting_ids:
                # Project chat combines meeting transcripts with locally
                # extracted document chunks. Both remain strictly project-scoped.
                hits.extend(self._project_document_hits(s, project_id, retrieval_query, limit=10))
        if not hits:
            return {"answer": "Keine passende Stelle in den Projektinhalten gefunden."
                    if project_id else "Keine passende Stelle im Transkript gefunden.",
                    "sources": [], "citations": [], "grounded": False, "hits": 0}
        engine = (self._llm_providers.engine_for_model(model_name)
                  if model_name else self._llm_engine())
        out = rag_answer(query, hits, engine, history=history)
        out["hits"] = len(hits)
        return out

    def general_chat(self, question: str, model_name: str | None = None) -> dict:
        """Answer a general question without retrieving or exposing meeting data.

        This deliberately stays separate from :meth:`ask`: RAG keeps its strict
        source contract, while a general answer is explicitly marked as
        ungrounded and carries no transcript sources.
        """
        question = (question or "").strip()
        if not question:
            raise ValueError("Bitte eine Frage eingeben.")
        engine = (self._llm_providers.engine_for_model(model_name)
                  if model_name else self._llm_engine())
        result = engine.complete(question, system=_GENERAL_CHAT_SYSTEM)
        answer = visible_answer(result.text or "")
        if not answer:
            raise LLMError("Das lokale Modell hat keine Antwort geliefert.")
        return {
            "answer": answer,
            "model": result.model or getattr(engine, "model_name", model_name or ""),
            "local": True,
            "grounded": False,
            "sources": [],
        }
