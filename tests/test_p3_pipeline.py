"""P3 regression tests: the automatic post-stop pipeline.

Contract under test:
- After ``stop`` the pipeline runs ``transcribe -> diarize? -> embed? ->
  analyze?`` automatically, non-blocking, and marks the meeting ``done``.
- Stages are gated by config: diarization / embeddings / analysis only run when
  their flags are on; otherwise the stage is reported ``skipped``.
- The pipeline is crash-resumable: already-``done`` stages are NOT re-run on
  resume, unfinished (``pending``/``failed``) stages are re-run.
- A failed ``transcribe`` (e.g. model missing) fails the meeting and records a
  clear error; a failed later stage keeps the usable transcript (meeting
  ``done``) and flags only that stage.
- A repeatedly failing stage is abandoned after ``pipeline_max_retries``.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import func, select

from core.config import get_config
from core.llm import MockLLM
from core.providers.base import ASREngine, ASRError, ASRSegment
from core.store.db import session_scope
from core.store.models import Meeting, ProcessingJob, TranscriptSegment


class CountingASREngine(ASREngine):
    """Offline ASR mock that counts calls and can sleep/fail on demand."""

    model_name = "mock-count"

    def __init__(self, fail: bool = False, delay: float = 0.0):
        self._segments = [
            ASRSegment(0.0, 1.2, "Hallo, alle zusammen.", "de", 0.90),
            ASRSegment(1.5, 3.0, "Lasst uns über das Q2-Budget sprechen.", "de", 0.85),
        ]
        self.fail = fail
        self.delay = delay
        self.transcribe_calls = 0

    def is_model_ready(self) -> bool:
        return not self.fail

    def prepare_model(self, allow_download: bool = False) -> None:
        return None

    def transcribe(self, audio_path, language: str | None = None) -> list[ASRSegment]:
        self.transcribe_calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise ASRError("simulated ASR failure")
        return self._segments


def _cfg(diarize=False, embed=True, analyze=True, max_retries=None):
    c = get_config()
    c.auto_pipeline = True
    c.speaker_diarization = diarize
    c.embeddings_enabled = embed
    c.auto_analyze = analyze
    if max_retries is not None:
        c.pipeline_max_retries = max_retries
    return c


def _capture(make_service, duration_s: float = 1.5, fail_asr: bool = False,
             fail_llm: bool = False, asr_delay: float = 0.0):
    """Capture a meeting to a finished (stopped) state and return the handles.

    The meeting is started and fully captured but NOT stopped, so the caller
    controls (and can time) the ``stop`` call that triggers the pipeline."""
    engine = CountingASREngine(fail=fail_asr, delay=asr_delay)
    llm = MockLLM(fail=fail_llm)
    svc = make_service(duration_s=duration_s, asr_engine=engine, llm_engine=llm)
    mid = svc.start_meeting(title="P3", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    return svc, mid, engine, llm


def _wait_pipeline(svc, mid, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    st = svc.pipeline_status(mid)
    # Terminal = a final status AND the pipeline no longer in flight (the
    # transcribe stage transiently flips status to 'done' mid-pipeline).
    while time.time() < deadline and not (
            st["status"] in ("done", "failed") and not st["running"]):
        time.sleep(0.05)
        st = svc.pipeline_status(mid)
    return st


def _stages(svc, mid) -> dict:
    return {s["stage"]: s["state"] for s in svc.pipeline_status(mid)["stages"]}


def _job_error(svc, mid, stage) -> str | None:
    with session_scope() as s:
        j = s.scalar(select(ProcessingJob).where(
            ProcessingJob.meeting_id == mid, ProcessingJob.stage == stage))
        return j.error if j else None


def _job_retries(svc, mid, stage) -> int:
    with session_scope() as s:
        j = s.scalar(select(ProcessingJob).where(
            ProcessingJob.meeting_id == mid, ProcessingJob.stage == stage))
        return (j.retries or 0) if j else -1


def _n_segments(svc, mid) -> int:
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(TranscriptSegment).where(
            TranscriptSegment.meeting_id == mid)) or 0


# --- ordering + gating ---
def test_pipeline_runs_all_stages_in_order(make_service):
    _cfg(diarize=True, embed=True, analyze=True)
    svc, mid, engine, llm = _capture(make_service)
    svc.stop(mid)
    st = _wait_pipeline(svc, mid)
    assert st["status"] == "done"
    states = _stages(svc, mid)
    assert states == {"transcribe": "done", "diarize": "done",
                      "embed": "done", "analyze": "done"}
    assert engine.transcribe_calls == 1
    assert llm.calls >= 1          # analysis actually ran
    assert _n_segments(svc, mid)   # transcript persisted
    assert svc.get_meeting(mid)["analyses"], "analysis should be stored"


def test_pipeline_gates_stages_by_config(make_service):
    _cfg(diarize=False, embed=False, analyze=False)
    svc, mid, engine, llm = _capture(make_service)
    svc.stop(mid)
    st = _wait_pipeline(svc, mid)
    assert st["status"] == "done"
    states = _stages(svc, mid)
    assert states == {"transcribe": "done", "diarize": "skipped",
                      "embed": "skipped", "analyze": "skipped"}
    assert engine.transcribe_calls == 1
    assert llm.calls == 0, "analysis must not run when gated off"


def test_pipeline_non_blocking_after_stop(make_service):
    _cfg(diarize=False, embed=False, analyze=False)
    # A 1.5 s ASR sleep: if the pipeline ran on the request path, stop() would
    # block for >= 1.5 s. It must return well before that.
    svc, mid, engine, llm = _capture(make_service, asr_delay=1.5)
    t0 = time.time()
    svc.stop(mid)
    elapsed = time.time() - t0
    assert elapsed < 1.2, f"stop() blocked {elapsed:.2f}s on the pipeline"
    st = svc.pipeline_status(mid)
    assert st["running"] or st["status"] in ("processing", "transcribing"), \
        "pipeline should be in flight right after stop"
    final = _wait_pipeline(svc, mid)
    assert final["status"] == "done"
    assert engine.transcribe_calls == 1


# --- failure handling ---
def test_pipeline_transcribe_failure_fails_meeting_with_clear_error(make_service):
    _cfg(diarize=False, embed=True, analyze=True)
    svc, mid, engine, llm = _capture(make_service, fail_asr=True)
    svc.stop(mid)
    st = _wait_pipeline(svc, mid)
    assert st["status"] == "failed"
    assert _stages(svc, mid)["transcribe"] == "failed"
    assert "simulated ASR failure" in (_job_error(svc, mid, "transcribe") or "")
    # a failed core stage must not have proceeded to the LLM
    assert llm.calls == 0


def test_pipeline_later_stage_failure_keeps_transcript(make_service):
    _cfg(diarize=False, embed=True, analyze=True)
    svc, mid, engine, llm = _capture(make_service, fail_llm=True)
    svc.stop(mid)
    st = _wait_pipeline(svc, mid)
    # analyze (not transcribe) failed -> the transcript stays usable
    assert st["status"] == "done"
    assert _stages(svc, mid)["transcribe"] == "done"
    assert _stages(svc, mid)["embed"] == "done"
    assert _stages(svc, mid)["analyze"] == "failed"
    assert "Analyse-Fehler" in (_job_error(svc, mid, "analyze") or "")
    assert _n_segments(svc, mid), "transcript must survive a failed analysis"


# --- crash resume ---
def test_resume_skips_done_and_reruns_pending(make_service):
    _cfg(diarize=False, embed=True, analyze=True)
    svc, mid, engine, llm = _capture(make_service)
    svc.stop(mid)
    _wait_pipeline(svc, mid)  # full run: everything done, transcribe ran once
    assert engine.transcribe_calls == 1

    # Simulate a crash that left embed + analyze unfinished: reset them to
    # pending (as recovery does for a mid-flight job) and mark the meeting
    # in-flight again.
    with session_scope() as s:
        for stage in ("embed", "analyze"):
            j = s.scalar(select(ProcessingJob).where(
                ProcessingJob.meeting_id == mid, ProcessingJob.stage == stage))
            j.status = "pending"
        m = s.get(Meeting, mid)
        m.status = "processing"
        s.commit()

    resumed = svc.resume_pending_pipelines()
    assert resumed >= 1
    _wait_pipeline(svc, mid)
    # transcribe was already done -> NOT re-run; embed/analyze re-ran
    assert engine.transcribe_calls == 1
    states = _stages(svc, mid)
    assert states == {"transcribe": "done", "diarize": "skipped",
                      "embed": "done", "analyze": "done"}


def test_resume_does_not_touch_finished_or_unrelated(make_service):
    _cfg(diarize=False, embed=False, analyze=False)
    svc, mid, engine, llm = _capture(make_service)
    svc.stop(mid)
    _wait_pipeline(svc, mid)
    # All done -> nothing to resume.
    assert svc.resume_pending_pipelines() == 0
    assert engine.transcribe_calls == 1


# --- retry cap ---
def test_repeated_failure_is_abandoned_after_max_retries(make_service):
    _cfg(diarize=False, embed=False, analyze=True, max_retries=2)
    svc, mid, engine, llm = _capture(make_service, fail_llm=True)
    svc.stop(mid)                      # analyze fails -> retries=1
    _wait_pipeline(svc, mid)
    assert _job_retries(svc, mid, "analyze") == 1
    svc.run_pipeline(mid, wait=True)   # retry 2 -> fails, retries=2
    assert _job_retries(svc, mid, "analyze") == 2
    svc.run_pipeline(mid, wait=True)   # capped -> skipped, retries stays 2
    assert _job_retries(svc, mid, "analyze") == 2
    assert _stages(svc, mid)["analyze"] == "failed"
