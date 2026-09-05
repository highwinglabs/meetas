"""Manual/on-demand transcription runs. (part of MeetingService)."""
from __future__ import annotations

import json
import threading
from core.jobs.queue import JobQueue
from core.store.db import session_scope
from core.transcribe.processor import TranscriptionProcessor
from core.store.models import Meeting
from core.logging_setup import get_logger
from core.services._common import UnknownMeetingError

log = get_logger("ma.service")


class TranscriptionMixin:

    def transcribe(self, meeting_id: str, language: str | None = None,
                   model_name: str | None = None,
                   allow_download: bool = False) -> dict:
        with self._transcription_locks_guard:
            lock = self._transcription_locks.setdefault(meeting_id, threading.Lock())
        with lock:
            with session_scope() as s:
                meeting = s.get(Meeting, meeting_id)
                if meeting is None or meeting.deleted_at is not None:
                    raise UnknownMeetingError(meeting_id)
                # An explicit request wins; otherwise honour the per-meeting
                # language chosen at recording start so a manual (re)transcribe
                # does not silently switch to auto-detection. A stored
                # "auto"/empty still means auto-detect.
                if language in (None, "", "auto"):
                    language = self._decode_settings(
                        meeting.settings_json).get("language")
            if language in ("", "auto"):
                language = None
            engine = self._asr_engine(model_name)
            # Gate model availability (network + confirmation) before running.
            engine.prepare_model(allow_download=allow_download)
            result = self._processor.process(meeting_id, engine, language=language)
            # Phase 5 (opt-in): automatically label speakers on the fresh transcript.
            # Off by default so the batch behaviour and existing tests are unchanged.
            if self.config.speaker_diarization and result.get("segments"):
                try:
                    self.diarize(meeting_id)
                except Exception:
                    log.exception("auto_diarize_failed meeting=%s", meeting_id[:8])
            return result

    def start_transcription(self, meeting_id: str, language: str | None = None,
                            model_name: str | None = None,
                            allow_download: bool = False) -> dict:
        """Queue a persistent transcription job without blocking the UI request."""
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            job = JobQueue.get_or_create(s, meeting_id, "transcribe")
            if meeting_id in self._manual_transcriptions or job.status == "running":
                return {"meeting_id": meeting_id, "status": "running", "job_id": job.id}
            if job.status == "failed":
                job.retries += 1
            job.status = "pending"
            job.error = None
            # Keep an explicitly selected model/language with the meeting so
            # crash recovery can retry the same request instead of silently
            # falling back to a different global default.
            persisted = self._decode_settings(meeting.settings_json)
            if isinstance(model_name, str) and model_name.strip():
                persisted["quality_asr_model"] = model_name.strip()[:256]
            if language not in (None, "", "auto"):
                persisted["language"] = language
            meeting.settings_json = json.dumps(
                self._normalise_meeting_settings(persisted), ensure_ascii=False)
            # Show the deliberate run immediately while the worker waits for
            # an executor slot.
            meeting.status = "transcribing"
            s.commit()
        with self._pipeline_lock:
            if meeting_id in self._manual_transcriptions:
                return {"meeting_id": meeting_id, "status": "running", "job_id": job.id}
            self._manual_transcriptions.add(meeting_id)
        try:
            self._pipeline_executor.submit(
                self._gated(self._run_manual_transcription), meeting_id, language,
                model_name, allow_download)
        except Exception:
            with self._pipeline_lock:
                self._manual_transcriptions.discard(meeting_id)
            with session_scope() as s:
                failed_job = JobQueue.get_or_create(s, meeting_id, "transcribe")
                JobQueue.mark_failed(s, failed_job, "Transkriptionsjob konnte nicht gestartet werden.")
                meeting = s.get(Meeting, meeting_id)
                if meeting is not None:
                    meeting.status = "failed"
                s.commit()
            raise
        return {"meeting_id": meeting_id, "status": "pending", "job_id": job.id}

    def _run_manual_transcription(self, meeting_id: str, language: str | None,
                                  model_name: str | None, allow_download: bool) -> None:
        try:
            self.transcribe(meeting_id, language=language, model_name=model_name,
                            allow_download=allow_download)
        except Exception as exc:
            # Model preparation can fail before TranscriptionProcessor starts,
            # so persist that failure here instead of leaving the UI in a
            # permanent transcribing/pending state.
            try:
                with session_scope() as s:
                    job = JobQueue.get_or_create(s, meeting_id, "transcribe")
                    if job.status != "failed":
                        job.retries = (job.retries or 0) + 1
                    JobQueue.mark_failed(s, job, str(exc))
                    meeting = s.get(Meeting, meeting_id)
                    if meeting is not None:
                        meeting.status = "failed"
                    s.commit()
            except Exception:
                log.exception("manual_transcription_failure_persist_failed meeting=%s", meeting_id[:8])
            log.exception("manual_transcription_failed meeting=%s", meeting_id[:8])
        finally:
            with self._pipeline_lock:
                self._manual_transcriptions.discard(meeting_id)
