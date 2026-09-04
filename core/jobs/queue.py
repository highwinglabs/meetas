"""Processing jobs: persistent, resumable pipeline stages.

Phase 1 only *records* jobs (the `transcribe` stage is enqueued after capture
and picked up by Phase 2). The important Phase 1 behavior is crash-safety:
any job left in `running` after a crash is reset to `pending` on startup so
the pipeline can resume instead of being lost.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.logging_setup import get_logger
from core.store.models import ProcessingJob

log = get_logger("ma.jobs")

STAGES = ("capture", "transcribe", "diarize", "embed", "analyze")


class JobQueue:
    """Thin wrapper over ProcessingJob rows. One logical job per (meeting, stage)."""

    @staticmethod
    def get_or_create(session: Session, meeting_id: str, stage: str) -> ProcessingJob:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        job = session.scalar(
            select(ProcessingJob).where(
                ProcessingJob.meeting_id == meeting_id,
                ProcessingJob.stage == stage,
            )
        )
        if job is None:
            job = ProcessingJob(meeting_id=meeting_id, stage=stage, status="pending")
            session.add(job)
            session.flush()
        return job

    @staticmethod
    def mark_running(session: Session, job: ProcessingJob) -> None:
        job.status = "running"
        session.flush()

    @staticmethod
    def mark_done(session: Session, job: ProcessingJob) -> None:
        job.status = "done"
        job.progress = 1.0
        session.flush()

    @staticmethod
    def mark_failed(session: Session, job: ProcessingJob, error: str) -> None:
        job.status = "failed"
        job.error = error
        session.flush()

    @staticmethod
    def mark_cancelled(session: Session, job: ProcessingJob) -> None:
        """User-initiated stop: a neutral terminal state, not an error."""
        job.status = "cancelled"
        job.error = None
        session.flush()

    @staticmethod
    def recover_crashed(session: Session) -> int:
        """Reset jobs stuck in 'running' (crash) back to 'pending' for resume."""
        stuck = session.scalars(
            select(ProcessingJob).where(ProcessingJob.status == "running")
        ).all()
        for job in stuck:
            job.status = "pending"
            job.retries += 1
            log.warning("job_reset_to_pending stage=%s meeting=%s", job.stage, job.meeting_id)
        if stuck:
            session.flush()
        return len(stuck)
