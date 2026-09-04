"""Startup crash recovery: finalize interrupted recordings, resume stuck jobs."""
from __future__ import annotations

import numpy as np

from core.audio.chunker import ChunkWriter
from core.recovery.recovery import recover
from core.store.db import get_engine, make_engine, session_scope
from core.store.models import Base, Meeting, ProcessingJob, Recording
from sqlalchemy import select


def _init(config):
    make_engine(config)
    Base.metadata.create_all(get_engine())


def _make_crashed_meeting(config, n_chunks: int) -> str:
    with session_scope() as s:
        m = Meeting(title="crashed", status="recording")
        s.add(m)
        s.flush()
        s.add(Recording(meeting_id=m.id, source="mic", status="recording", in_progress=True))
        s.commit()
        mid = m.id
    mdir = config.audio_dir / mid
    w = ChunkWriter(mdir, 16000, 1, 1.0, source="mic")
    w.begin()
    for _ in range(n_chunks):
        w.write_chunk(np.zeros(16000, dtype=np.float32).reshape(-1, 1))
    w.close()  # in_progress marker stays -> simulates a hard crash
    return mid


def test_crashed_recording_is_finalized(config):
    _init(config)
    mid = _make_crashed_meeting(config, n_chunks=3)

    with session_scope() as s:
        report = recover(config, s)

    assert report.meetings_finalized == 1
    with session_scope() as s:
        m = s.get(Meeting, mid)
        rec = s.scalar(select(Recording).where(Recording.meeting_id == mid))
        jobs = s.scalars(select(ProcessingJob).where(ProcessingJob.meeting_id == mid)).all()
        assert m.status == "ready"
        assert m.duration_s is not None and 2.9 < m.duration_s < 3.1
        assert rec.status == "assembled"
        assert rec.in_progress is False
        assert rec.original_path is not None
        assert (config.audio_dir / mid / rec.original_path.split("/")[-1]).exists()
        assert any(j.stage == "transcribe" for j in jobs)
        assert not (config.audio_dir / mid / "in_progress").exists()


def test_crashed_recording_without_chunks_fails(config):
    _init(config)
    with session_scope() as s:
        m = Meeting(title="empty", status="recording")
        s.add(m)
        s.flush()
        s.add(Recording(meeting_id=m.id, source="mic", status="recording", in_progress=True))
        s.commit()
        mid = m.id
    mdir = config.audio_dir / mid
    w = ChunkWriter(mdir, 16000, 1, 1.0)
    w.begin()  # marker written, but NO chunks
    w.close()

    with session_scope() as s:
        report = recover(config, s)
    assert report.meetings_failed == 1
    with session_scope() as s:
        m = s.get(Meeting, mid)
        assert m.status == "failed"
        assert not (mdir / "in_progress").exists()


def test_stuck_running_jobs_are_reset(config):
    _init(config)
    mid = _make_crashed_meeting(config, n_chunks=1)
    with session_scope() as s:
        m = s.get(Meeting, mid)
        job = ProcessingJob(meeting_id=mid, stage="transcribe", status="running")
        s.add(job)
        s.commit()
    with session_scope() as s:
        report = recover(config, s)
        j = s.scalar(select(ProcessingJob).where(
            ProcessingJob.meeting_id == mid, ProcessingJob.stage == "transcribe"))
        assert report.jobs_reset >= 1
        assert j.status == "pending"
        assert j.retries >= 1


def test_recovery_is_idempotent(config):
    _init(config)
    mid = _make_crashed_meeting(config, n_chunks=2)
    with session_scope() as s:
        recover(config, s)
    # second run: nothing to do (meeting no longer active)
    with session_scope() as s:
        report = recover(config, s)
        assert report.meetings_finalized == 0
        assert report.meetings_failed == 0
