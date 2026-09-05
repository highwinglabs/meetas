"""Meeting analysis (Phase 3): transcript -> validated structured JSON analysis.

The :class:`AnalysisProcessor` asks the (local) LLM engine to return a
**mandatory JSON structure** (nine fixed areas, every statement with a source)
for a stored transcript. The answer is parsed and *strictly validated*:

* all nine areas must be present (empty lists allowed),
* every statement needs >= 1 source with a segment_id that really exists,
* unknown responsible persons / deadlines are normalised to "nicht angegeben".

If the model returns invalid or incomplete JSON, it is asked **once** to fix its
own output (with the concrete error list). If the second attempt is still
invalid, the analysis fails clearly and **nothing is stored** -- we never invent
facts. The (slow) LLM call happens outside any open DB session; the resumable
``analyze`` job + meeting status are updated before and after it.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import timezone
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from sqlalchemy import select

from core.analysis import schema
from core.jobs.queue import JobQueue
from core.llm.base import LLMCancelledError, LLMEngine, LLMError
from core.logging_setup import get_logger
from core.store.db import session_scope
from core.store.models import Analysis, Meeting, TranscriptSegment

log = get_logger("ma.analysis")

# Keep the prompt comfortably within a small local model's context window.
_MAX_TRANSCRIPT_CHARS = 24000


def build_prompt(
    title: str,
    segments: Sequence[Tuple[float, float, Optional[str], str]],
    lang: Optional[str] = None,
    kind: str = "summary",
    override_system: Optional[str] = None,
    output_lang: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for the structured JSON analysis.

    ``segments`` is a sequence of ``(start_s, end_s, speaker_id, text)`` in
    playback order. The user prompt carries positional ``S<n>`` segment ids so
    the model can (and must) cite real segments as sources. ``output_lang`` adds
    an explicit instruction to write all text content in that language.
    """
    rows = _trim(schema.to_seg_rows(segments))
    return (override_system or schema.system_prompt(lang, output_lang),
            schema.build_user_prompt(title, rows, lang))


def _trim(rows: Sequence[Dict[str, Any]]) -> list:
    """Bound the transcript length (small local model context)."""
    out: list = []
    total = 0
    for r in rows:
        line = schema._line(r)
        if total + len(line) > _MAX_TRANSCRIPT_CHARS and out:
            break
        out.append(r)
        total += len(line)
    return out


class AnalysisProcessor:
    """Runs the analysis pipeline for one meeting.

    Receiving the engine (rather than building it) keeps this class testable
    with a ``MockLLM`` and means no real LLM request is ever made in tests.
    """

    def __init__(self, config) -> None:
        self.config = config

    def process(
        self,
        meeting_id: str,
        engine: LLMEngine,
        kind: str = "summary",
        override_system: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        still_current: Optional[Callable[[], bool]] = None,
        output_lang: Optional[str] = None,
    ) -> Dict[str, Any]:
        # 1) Preconditions (do not flip status to failed for a bad precondition).
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None:
                raise KeyError(meeting_id)
            segs = s.scalars(
                select(TranscriptSegment).where(
                    TranscriptSegment.meeting_id == meeting_id
                ).order_by(TranscriptSegment.start_s)
            ).all()
            if not segs:
                raise LLMError(
                    "Kein Transkript vorhanden. Bitte das Meeting zuerst "
                    "transkribieren, bevor es analysiert wird."
                )
            rows = _trim(schema.to_seg_rows(
                [(seg.start_s, seg.end_s, seg.speaker_id, seg.text) for seg in segs]
            ))
            title = meeting.title
            lang = meeting.lang
            # 2) Mark the resumable job + status (committed before the slow call).
            job = JobQueue.get_or_create(s, meeting_id, "analyze")
            JobQueue.mark_running(s, job)
            meeting.status = "analyzing"
            s.commit()

        def _cancelled() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return True
            return still_current is not None and not still_current()

        def _finish_cancelled() -> Dict[str, Any]:
            # A newer analysis for this meeting replaced this one: never touch
            # the job/status rows in that case.
            if still_current is not None and not still_current():
                return {"meeting_id": meeting_id, "status": "cancelled", "kind": kind}
            with session_scope() as s:
                job = JobQueue.get_or_create(s, meeting_id, "analyze")
                if job.status in ("pending", "running"):
                    JobQueue.mark_cancelled(s, job)
                m = s.get(Meeting, meeting_id)
                if m is not None and m.status == "analyzing":
                    m.status = "ready"
                s.commit()
            log.info("analyze_cancelled meeting=%s kind=%s", meeting_id[:8], kind)
            return {"meeting_id": meeting_id, "status": "cancelled", "kind": kind}

        if _cancelled():
            return _finish_cancelled()

        valid_ids = schema.segment_ids(rows)
        system_prompt = override_system or schema.system_prompt(lang)
        user_prompt = schema.build_user_prompt(title, rows, lang)

        # 3) The (slow) LLM call happens outside any open DB session.
        t0 = time.monotonic()
        try:
            result = engine.complete(user_prompt, system=system_prompt,
                                     cancel_event=cancel_event)
            if _cancelled():
                return _finish_cancelled()
            raw = result.text
            model_used = result.model
            parsed = schema.extract_json(raw)
            normalised, errors = schema.validate_analysis(parsed, valid_ids)
            # 3b) One automatic self-correction, then fail clearly (no invention).
            if errors:
                # Technical diagnosis only (answer length + reason) -- never the
                # full transcript or LLM answer, per the logging policy.
                log.warning(
                    "analyze_json_invalid meeting=%s raw_len=%d found_object=%s "
                    "errors=%d first=%s",
                    meeting_id[:8], len(raw or ""),
                    isinstance(parsed, dict), len(errors),
                    (errors[0] if errors else ""),
                )
                if _cancelled():
                    return _finish_cancelled()
                fix_prompt = schema.build_fix_prompt(errors, raw, rows)
                result2 = engine.complete(fix_prompt, system=schema.FIX_SYSTEM_PROMPT,
                                          cancel_event=cancel_event)
                if _cancelled():
                    return _finish_cancelled()
                raw = result2.text
                model_used = result2.model or model_used
                parsed = schema.extract_json(raw)
                normalised, errors = schema.validate_analysis(parsed, valid_ids)
                if errors:
                    detail = "; ".join(errors[:8])
                    log.warning(
                        "analyze_json_still_invalid meeting=%s raw_len=%d "
                        "found_object=%s errors=%d",
                        meeting_id[:8], len(raw or ""),
                        isinstance(parsed, dict), len(errors),
                    )
                    raise LLMError(
                        "LLM lieferte ungültiges oder unvollständiges Analyse-JSON "
                        "(auch nach Korrekturversuch). Fehler: "
                        f"{detail}. Es wurde nichts gespeichert (keine Fakten erfunden)."
                    )
            content = json.dumps(normalised, ensure_ascii=False, indent=2)
            markdown = schema.render_markdown(normalised, lang=output_lang)
        except LLMCancelledError:
            return _finish_cancelled()
        except Exception as exc:  # noqa: BLE001 - normalise any failure to job/DB state
            if _cancelled():
                return _finish_cancelled()
            with session_scope() as s:
                job = JobQueue.get_or_create(s, meeting_id, "analyze")
                JobQueue.mark_failed(s, job, str(exc))
                m = s.get(Meeting, meeting_id)
                if m is not None:
                    m.status = "failed"
                s.commit()
            log.error("analyze_failed meeting=%s kind=%s error=%s",
                      meeting_id[:8], kind, exc)
            raise

        if _cancelled():
            return _finish_cancelled()

        # 4) Persist the validated analysis (upsert per meeting+kind) and finalise.
        with session_scope() as s:
            job = JobQueue.get_or_create(s, meeting_id, "analyze")
            JobQueue.mark_done(s, job)
            analysis = s.scalar(
                select(Analysis).where(
                    Analysis.meeting_id == meeting_id, Analysis.kind == kind
                )
            )
            if analysis is None:
                analysis = Analysis(meeting_id=meeting_id, kind=kind)
                s.add(analysis)
                s.flush()
            analysis.model = model_used
            analysis.output_lang = output_lang
            analysis.content = content
            m = s.get(Meeting, meeting_id)
            if m is not None:
                m.status = "done"
            s.commit()
            if analysis.created_at:
                created = analysis.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                created_at = created.astimezone(timezone.utc).isoformat()
            else:
                created_at = None

        log.info(
            "analyze_done meeting=%s kind=%s model=%s chars=%d dur=%.2fs",
            meeting_id[:8], kind, model_used, len(content),
            round(time.monotonic() - t0, 3),
        )
        return {
            "meeting_id": meeting_id,
            "status": "done",
            "kind": kind,
            "model": model_used,
            "output_lang": output_lang,
            "content": content,
            "markdown": markdown,
            "created_at": created_at,
        }
