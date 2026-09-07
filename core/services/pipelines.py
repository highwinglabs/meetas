"""LLM analysis runs and the automatic post-stop pipeline. (part of MeetingService)."""
from __future__ import annotations

import re
import threading
from typing import Callable
from sqlalchemy import select
from core.analysis import schema
from core.jobs.queue import JobQueue
from core.tasks import extract_tasks
from core.store.db import session_scope
from core.store.models import Meeting, ProcessingJob
from core.logging_setup import get_logger
from core.services._common import UnknownMeetingError, friendly_job_error

log = get_logger("ma.service")


class PipelineMixin:

    def analyze(self, meeting_id: str, kind: str = "summary",
                override_system: str | None = None,
                model_name: str | None = None,
                template: str | None = None) -> dict:
        """Run the LLM analysis for a transcribed meeting (local server only).

        The real LLM is only invoked here, when the user actually asks for an
        analysis. In tests/dev the injected ``llm_engine`` (MockLLM) is used.
        """
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            meeting_lang = meeting.lang
            # The per-meeting analysis-language choice decides whether the LLM
            # must write in the transcript language ("wie_transkript") or in a
            # fixed one (de/en). Resolved here so the directive goes into the
            # system prompt that is actually sent.
            output_lang = schema.resolve_output_lang(
                self._decode_settings(meeting.settings_json).get("analysis_language"),
                meeting_lang)
        engine = (self._llm_providers.engine_for_model(model_name)
                  if model_name else self._llm_engine())
        system = override_system or schema.system_prompt_for_template(
            template, lang=meeting_lang, output_lang=output_lang)
        with self._analysis_lock:
            token = self._analysis_tokens.get(meeting_id, 0) + 1
            self._analysis_tokens[meeting_id] = token
            cancel_event = threading.Event()
            self._cancel_events[token] = cancel_event
            self._analysis_engines[token] = engine
        try:
            out = self._analysis_processor.process(
                meeting_id, engine, kind=kind, override_system=system,
                output_lang=output_lang,
                cancel_event=cancel_event,
                still_current=lambda: self._analysis_tokens.get(meeting_id) == token,
            )
        finally:
            with self._analysis_lock:
                if self._analysis_tokens.get(meeting_id) == token:
                    self._analysis_tokens.pop(meeting_id, None)
                self._cancel_events.pop(token, None)
                self._analysis_engines.pop(token, None)
        if out.get("status") == "cancelled":
            return out
        # Phase 6: pull action items from the summary into the central task
        # board (idempotent -- re-analysis never duplicates tasks).
        if kind == "summary" and self.config.tasks_enabled:
            with session_scope() as s:
                res = extract_tasks(s, meeting_id)
                s.commit()
            out["tasks"] = res
        # Phase 7: derive a local auto title + tags from the analysis.
        if kind == "summary":
            out["auto_meta"] = self.apply_auto_title_tags(meeting_id)
        return out

    def cancel_analysis(self, meeting_id: str) -> dict:
        """Stop a running (or stuck) LLM analysis for a meeting.

        The job is marked ``cancelled`` (a neutral state, not an error) and the
        meeting returns to ``ready``. Nothing is stored from the stopped run.
        If the LLM request is still in flight it finishes in the background and
        its result is discarded; closing the engine's HTTP client usually ends
        it immediately.
        """
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
        engine = None
        shared = False
        with self._analysis_lock:
            token = self._analysis_tokens.get(meeting_id)
            if token is not None:
                self._cancel_events[token].set()
                engine = self._analysis_engines.get(token)
                # Only close the shared cached engine when no other active
                # analysis is using the same instance.
                shared = any(self._analysis_engines.get(t) is engine
                             for t in self._analysis_engines if t != token)
        if engine is not None and not shared:
            close = getattr(engine, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best-effort interruption
                    pass
        with session_scope() as s:
            job = s.scalar(
                select(ProcessingJob).where(
                    ProcessingJob.meeting_id == meeting_id,
                    ProcessingJob.stage == "analyze",
                )
            )
            if job is not None and job.status in ("pending", "running"):
                JobQueue.mark_cancelled(s, job)
            m = s.get(Meeting, meeting_id)
            if m is not None and m.status == "analyzing":
                m.status = "ready"
            s.commit()
            return {
                "meeting_id": meeting_id,
                "status": job.status if job is not None else "cancelled",
            }

    # --- P3: automatic post-stop pipeline (transcribe -> diarize -> embed -> analyze) ---
    def pipeline_stages(self, meeting_id: str) -> list[tuple[str, Callable]]:
        """The applicable pipeline stages for a meeting, in order."""
        settings = self._meeting_settings(meeting_id)
        final_model = settings.get("quality_asr_model") or settings.get("asr_model")
        language = settings.get("language")
        if language in ("", "auto"):
            language = None
        with self._config_lock:
            default_speaker_mode = self.config.default_speaker_mode
            diarization_enabled = bool(self.config.speaker_diarization)
            embeddings_default = bool(self.config.embeddings_enabled)
            analysis_default = bool(self.config.auto_analyze)
        speaker_mode = settings.get("speaker_mode")
        if speaker_mode is None:
            speaker_mode = default_speaker_mode
            # The legacy boolean remains a compatibility switch: enabling it
            # must still opt the default pipeline into post-run diarization
            # when no per-meeting mode was stored.
            if speaker_mode == "off" and diarization_enabled:
                speaker_mode = "after"
        embeddings = settings.get("embeddings_enabled", embeddings_default)
        analysis_enabled = settings.get("analysis_enabled", analysis_default)
        analysis_model = settings.get("analysis_model")
        stages: list[tuple[str, Callable]] = [
            ("transcribe", lambda: self.transcribe(meeting_id, language=language,
                                                     model_name=final_model)),
        ]
        if speaker_mode in ("after", "live"):
            stages.append(("diarize", lambda: self.diarize(meeting_id)))
        if embeddings:
            stages.append(("embed", lambda: self.embed_meeting(meeting_id)))
        if analysis_enabled:
            stages.append(("analyze", lambda: self.analyze(
                meeting_id, model_name=analysis_model,
                template=settings.get("analysis_template"))))
        return stages

    def _gated(self, fn: Callable):
        """Wrap a worker fn so it only runs while holding a concurrency slot.

        The gate reflects the *current* ``pipeline_max_workers`` value, so a
        runtime setting change immediately bounds (or lifts) parallel pipelines
        without recreating the executor."""
        def wrapper(*args, **kwargs):
            with self._pipeline_gate:
                return fn(*args, **kwargs)
        return wrapper

    def _submit_pipeline(self, meeting_id: str):
        with self._pipeline_lock:
            if meeting_id in self._pipeline_inflight:
                return None
            self._pipeline_inflight.add(meeting_id)
            try:
                return self._pipeline_executor.submit(
                    self._gated(self._run_pipeline), meeting_id)
            except Exception:
                # Executor shutdown can race a request arriving during daemon
                # shutdown. Never leave a meeting permanently marked in-flight
                # when submission itself failed.
                self._pipeline_inflight.discard(meeting_id)
                raise

    def schedule_pipeline(self, meeting_id: str) -> bool:
        """Fire-and-forget: schedule the pipeline if not already running."""
        if self._submit_pipeline(meeting_id) is None:
            return False
        log.info("pipeline_scheduled meeting=%s", meeting_id[:8])
        return True

    def run_pipeline(self, meeting_id: str, wait: bool = False) -> dict:
        """(Re)start or join the pipeline; block only when ``wait`` is set."""
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
        fut = self._submit_pipeline(meeting_id)
        if fut is not None and wait:
            fut.result()
        return self.pipeline_status(meeting_id)

    def _run_pipeline(self, meeting_id: str) -> None:
        try:
            self._set_meeting_status(meeting_id, "processing")
            stages = self.pipeline_stages(meeting_id)
            last = len(stages) - 1
            for i, (stage, fn) in enumerate(stages):
                res = self._run_stage(meeting_id, stage, fn)
                if res in {"failed", "exhausted"}:
                    # A failed transcribe makes the meeting unusable; a later
                    # failure keeps the (usable) transcript but flags the stage.
                    status = "failed" if stage == "transcribe" else "done"
                    self._set_meeting_status(meeting_id, status)
                    log.warning("pipeline_stopped meeting=%s stage=%s status=%s",
                                meeting_id[:8], stage, status)
                    return
                if res == "cancelled":
                    # User-stopped stage: terminal, but the meeting stays usable.
                    self._set_meeting_status(meeting_id, "ready")
                    log.info("pipeline_stopped meeting=%s stage=%s status=cancelled",
                             meeting_id[:8], stage)
                    return
                if i < last:
                    # transcribe marks the meeting 'done'; re-assert while we go on
                    self._set_meeting_status(meeting_id, "processing")
            self._set_meeting_status(meeting_id, "done")
        except Exception:  # noqa: BLE001 - a worker must never take the daemon down
            log.exception("pipeline_error meeting=%s", meeting_id[:8])
            self._set_meeting_status(meeting_id, "failed")
        finally:
            with self._pipeline_lock:
                self._pipeline_inflight.discard(meeting_id)

    def _run_stage(self, meeting_id: str, stage: str, fn: Callable) -> str:
        with session_scope() as s:
            job = JobQueue.get_or_create(s, meeting_id, stage)
            if job.status == "done":
                s.commit()
                return "skipped"
            if job.status == "cancelled":
                # A user-stopped stage is terminal; an explicit re-trigger from
                # the UI starts it again (analyze resets it to running).
                s.commit()
                return "cancelled"
            retries = job.retries or 0
            if job.status == "failed" and retries >= self.config.pipeline_max_retries:
                s.commit()
                return "exhausted"  # gave up: too many attempts
            # Crash recovery changes running jobs back to pending and counts
            # the interrupted attempt.  Honour the same cap for those states;
            # a brand-new pending job (retries == 0) must still run once.
            if job.status in {"pending", "running", "cancelled"} and retries > 0 \
                    and retries >= self.config.pipeline_max_retries:
                s.commit()
                return "exhausted"
            JobQueue.mark_running(s, job)
            s.commit()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - record, attribute, stop the chain
            with session_scope() as s:
                job = JobQueue.get_or_create(s, meeting_id, stage)
                job.retries = (job.retries or 0) + 1
                JobQueue.mark_failed(s, job, friendly_job_error(exc))
                if stage == "transcribe":
                    m = s.get(Meeting, meeting_id)
                    if m is not None:
                        m.status = "failed"
                s.commit()
            log.error("pipeline_stage_failed meeting=%s stage=%s error=%s",
                      meeting_id[:8], stage, exc)
            return "failed"
        if isinstance(result, dict) and result.get("status") == "cancelled":
            # User stopped it mid-run; the analysis already marked the job
            # cancelled. Never flip that back to done.
            with session_scope() as s:
                m = s.get(Meeting, meeting_id)
                if m is not None and m.status in ("analyzing", "processing"):
                    m.status = "ready"
                s.commit()
            return "cancelled"
        with session_scope() as s:
            job = JobQueue.get_or_create(s, meeting_id, stage)
            if job.status != "done":
                JobQueue.mark_done(s, job)
            s.commit()
        return "ok"

    def resume_pending_pipelines(self) -> int:
        """Re-run pipelines left unfinished (crash) after startup."""
        if not self.config.auto_pipeline:
            return 0
        with session_scope() as s:
            # Only unfinished jobs belonging to a meeting that is itself still
            # in a processing state are crash-resumable.  A later-stage failure
            # intentionally leaves a usable meeting as ``done``; it must wait
            # for an explicit retry from the UI instead of being retried on
            # every application start.  Retry caps are honoured here too.
            active_ids = set(s.scalars(select(Meeting.id).where(
                Meeting.status.in_(["processing", "transcribing", "ready"]),
                Meeting.deleted_at.is_(None))).all())
            jobs_by_meeting: dict[str, dict[str, ProcessingJob]] = {}
            for job in s.scalars(select(ProcessingJob).where(
                    ProcessingJob.meeting_id.in_(active_ids))).all():
                jobs_by_meeting.setdefault(job.meeting_id, {})[job.stage] = job

        # Decide per stage, in pipeline order.  A meeting is resumable only when
        # its first unfinished stage is pending/retryable (or missing).  This
        # avoids the old ``resumable |= active_ids`` behaviour, which repeatedly
        # started meetings whose failed stage had already exhausted its retry
        # budget and could incorrectly mark them done without that stage.
        resumable: set[str] = set()
        for mid in active_ids:
            jobs = jobs_by_meeting.get(mid, {})
            blocked = False
            needs_run = False
            for stage, _ in self.pipeline_stages(mid):
                job = jobs.get(stage)
                if job is None:
                    needs_run = True
                    break
                if job.status == "done":
                    continue
                if job.status == "cancelled":
                    # User-stopped stage is terminal: never auto-resume it.
                    blocked = True
                    break
                if job.status == "failed" and (job.retries or 0) >= self.config.pipeline_max_retries:
                    blocked = True
                    break
                if job.status in {"pending", "running"} \
                        and (job.retries or 0) > 0 \
                        and (job.retries or 0) >= self.config.pipeline_max_retries:
                    blocked = True
                    break
                # pending/failed (below the cap), and crash-recovered running
                # rows, are the first unfinished stage to drive again.
                if job.status in {"pending", "failed", "running"}:
                    needs_run = True
                    break
            if needs_run and not blocked:
                resumable.add(mid)
        n = 0
        for mid in sorted(resumable):
            if self.schedule_pipeline(mid):
                n += 1
        if n:
            log.info("pipeline_resumed meetings=%s", n)
        return n

    def pipeline_status(self, meeting_id: str) -> dict:
        with session_scope() as s:
            m = s.get(Meeting, meeting_id)
            if m is None or m.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            status = m.status
            jobs = {j.stage: j for j in s.scalars(
                select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)).all()}
        applicable = {st for st, _ in self.pipeline_stages(meeting_id)}
        stages = []
        for stage in ("transcribe", "diarize", "embed", "analyze"):
            if stage not in applicable:
                state, err = "skipped", None
            elif stage not in jobs:
                state, err = "pending", None
            else:
                state, err = jobs[stage].status, jobs[stage].error
            stages.append({"stage": stage, "state": state, "error": err})
        with self._pipeline_lock:
            in_flight = meeting_id in self._pipeline_inflight
        applicable_states = [st["state"] for st in stages if st["state"] != "skipped"]
        return {
            "meeting_id": meeting_id,
            "status": status,
            "stages": stages,
            # in_flight is the authoritative "still active" signal: the transcribe
            # stage briefly flips the meeting to 'done' before the next stage runs,
            # so status alone is not a reliable terminal indicator.
            "running": in_flight or status in ("processing", "transcribing")
                        or "running" in applicable_states,
            "done": (not in_flight) and status == "done"
                    and all(x == "done" for x in applicable_states),
        }

    def _set_meeting_status(self, meeting_id: str, status: str) -> None:
        with session_scope() as s:
            m = s.get(Meeting, meeting_id)
            if m is not None:
                m.status = status
                s.commit()
