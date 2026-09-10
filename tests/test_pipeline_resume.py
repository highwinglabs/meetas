"""Pipeline crash-resume (Point 5j).

``resume_pending_pipelines`` re-drives a meeting that a crash left mid-pipeline:
it must pick up meetings still in an active status (with a pending / retryable
stage) and run them to completion on the next start, while *not* auto-retrying a
meeting that has already finished (a later-stage failure must wait for an
explicit UI retry, not fire on every application start).
"""
from __future__ import annotations

import time

from sqlalchemy import delete, select

from core.store.db import session_scope
from core.store.models import (
    Meeting,
    ProcessingJob,
    TranscriptSegment,
    new_id,
)


def _wait_terminal(svc, mid: str, timeout: float = 20.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with session_scope() as s:
            st = s.get(Meeting, mid).status
        if st in ("done", "failed"):
            return st
        time.sleep(0.05)
    raise AssertionError(f"meeting {mid[:8]} never reached a terminal state: {st}")


def test_resume_drives_crashed_meeting_to_done(finalize_meeting, config):
    svc, mid = finalize_meeting(asr_engine=None, title="Crash")
    # Only run the transcribe stage (deterministic, offline).
    svc.config.auto_pipeline = True
    svc.config.embeddings_enabled = False

    # Simulate a crash before transcribe finished: active status + a pending
    # transcribe job and no segments. (Two sessions so the old stage rows are
    # gone before the fresh pending row is inserted -- same (meeting, stage) key.)
    with session_scope() as s:
        s.execute(delete(TranscriptSegment).where(TranscriptSegment.meeting_id == mid))
        s.execute(delete(ProcessingJob).where(ProcessingJob.meeting_id == mid))
        s.get(Meeting, mid).status = "processing"
        s.commit()
    with session_scope() as s:
        s.add(ProcessingJob(id=new_id(), meeting_id=mid, stage="transcribe",
                            status="pending"))
        s.commit()

    n = svc.resume_pending_pipelines()
    assert n == 1  # exactly this meeting is crash-resumable

    final = _wait_terminal(svc, mid)
    assert final == "done"
    with session_scope() as s:
        nseg = len(s.scalars(select(TranscriptSegment).where(
            TranscriptSegment.meeting_id == mid)).all())
        job = s.scalar(select(ProcessingJob).where(
            ProcessingJob.meeting_id == mid, ProcessingJob.stage == "transcribe"))
    assert nseg > 0                       # transcribe actually re-ran
    assert job is not None and job.status == "done"


def test_resume_skips_finished_meetings(finalize_meeting, config):
    """A finished meeting with a failed stage must NOT auto-retry on startup."""
    svc, mid = finalize_meeting(asr_engine=None, title="Done")
    svc.config.auto_pipeline = True
    svc.config.embeddings_enabled = False

    # A finished meeting whose later stage failed: it stays 'done' (usable) and
    # must wait for an explicit retry, so it is not crash-resumable.
    with session_scope() as s:
        s.get(Meeting, mid).status = "done"
        s.add(ProcessingJob(id=new_id(), meeting_id=mid, stage="analyze",
                            status="failed", error="boom", retries=0))
        s.commit()

    assert svc.resume_pending_pipelines() == 0
    # And it was not scheduled: the inflight set is empty.
    assert svc._pipeline_inflight == set()


def test_resume_recovers_analyzing_meeting(finalize_meeting, config):
    """A crash during analysis (status 'analyzing', analyze job reset to
    pending by crash recovery) must be resumed on startup, not left in a
    perpetual 'analyzing' state."""
    import json

    svc, mid = finalize_meeting(asr_engine=None, title="AnalyzeCrash")
    svc.config.auto_pipeline = True
    svc.config.embeddings_enabled = False

    # A usable transcript exists (mock ASR, deterministic).
    svc.transcribe(mid, language="de")

    # Simulate the crash state: recovery reset the running analyze job back to
    # pending (retries bumped) and the meeting is stuck in 'analyzing'.
    with session_scope() as s:
        m = s.get(Meeting, mid)
        m.status = "analyzing"
        m.settings_json = json.dumps({"analysis_enabled": True})
        job = s.scalar(select(ProcessingJob).where(
            ProcessingJob.meeting_id == mid,
            ProcessingJob.stage == "analyze"))
        if job is None:
            s.add(ProcessingJob(id=new_id(), meeting_id=mid, stage="analyze",
                                status="pending", retries=1))
        else:
            job.status = "pending"
            job.retries = 1
        s.commit()

    n = svc.resume_pending_pipelines()
    assert n == 1

    final = _wait_terminal(svc, mid)
    assert final == "done"
    with session_scope() as s:
        job = s.scalar(select(ProcessingJob).where(
            ProcessingJob.meeting_id == mid,
            ProcessingJob.stage == "analyze"))
    assert job is not None and job.status == "done"
