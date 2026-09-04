"""Startup crash recovery.

Runs whenever the core process (re)starts. It is idempotent and makes the
system consistent after a hard crash:

1. Jobs stuck in `running` are reset to `pending` (pipeline resumes, not lost).
2. Meetings still marked `recording`/`paused` (interrupted) are finalized from
   their chunk index: the original is assembled, the recording marked
   `assembled`, the meeting moved to `ready`, and a `transcribe` job is queued.
   If no complete chunks exist, the meeting is marked `failed` (no silent loss
   of state, no phantom active meeting).

The invariant maintained: at most one *active* (recording/paused) meeting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.audio.assembly import AssemblyError, assemble_original
from core.audio.chunker import ChunkWriter
from core.config import Config
from core.jobs.queue import JobQueue
from core.logging_setup import get_logger
from core.store.models import Meeting, ProcessingJob, Recording, utcnow

log = get_logger("ma.recovery")

ACTIVE_STATUSES = ("recording", "paused")


@dataclass
class RecoveryReport:
    jobs_reset: int = 0
    meetings_finalized: int = 0
    meetings_failed: int = 0
    notes: list[str] = field(default_factory=list)


def active_meeting(session: Session) -> Meeting | None:
    return session.scalars(
        select(Meeting).where(Meeting.status.in_(ACTIVE_STATUSES),
                                Meeting.deleted_at.is_(None)).order_by(Meeting.start_at.desc())
    ).first()


def recover(config: Config, session: Session) -> RecoveryReport:
    report = RecoveryReport()

    # 1) resume jobs that were mid-flight when the process died
    report.jobs_reset = JobQueue.recover_crashed(session)
    if report.jobs_reset:
        report.notes.append(f"{report.jobs_reset} job(s) reset to pending")

    # 2) finalize interrupted recordings
    interrupted = session.scalars(
        select(Meeting).where(Meeting.status.in_(ACTIVE_STATUSES),
                                Meeting.deleted_at.is_(None))
    ).all()

    for meeting in interrupted:
        meeting_dir = config.audio_dir / meeting.id
        marker = meeting_dir / "in_progress"
        recording: Recording | None = None
        for rec in meeting.recordings:
            if rec.in_progress:
                recording = rec
                break
        if recording is None:
            recording = meeting.recordings[0] if meeting.recordings else None

        try:
            if marker.exists():
                original = assemble_original(meeting_dir, config)
            else:
                # no marker: finalize path already ran before the crash.
                candidate = meeting_dir / "original.wav"
                if not candidate.is_file():
                    raise AssemblyError("keine Aufnahmedaten gefunden")
                original = candidate

            if recording is not None:
                recording.status = "assembled"
                recording.in_progress = False
                recording.original_path = str(original)
            writer = ChunkWriter(meeting_dir, 0, 0)
            duration = writer.total_duration_s()
            meeting.status = "ready"
            meeting.end_at = meeting.start_at + _timedelta(duration) if duration else utcnow()
            if duration:
                meeting.duration_s = round(duration, 3)
            JobQueue.get_or_create(session, meeting.id, "transcribe")
            if marker.exists():
                marker.unlink()
            report.meetings_finalized += 1
            report.notes.append(f"meeting {meeting.id[:8]} finalized ({duration:.1f}s)")
        except (AssemblyError, OSError, KeyError, TypeError, ValueError) as exc:
            if recording is not None:
                recording.status = "failed"
                recording.in_progress = False
            meeting.status = "failed"
            if marker.exists():
                marker.unlink()
            report.meetings_failed += 1
            report.notes.append(f"meeting {meeting.id[:8]} failed: {exc}")
            log.warning("recovery_failed meeting=%s err=%s", meeting.id[:8], exc)

    # A manual transcription has no in-memory worker after a process restart
    # (and its selected model is request-scoped).  When automatic pipelines are
    # disabled, expose that interrupted request as a retryable failure instead
    # of leaving the meeting forever in ``transcribing``.  Automatic pipelines
    # remain pending and are resumed by the service with their persisted
    # meeting settings.
    if not config.auto_pipeline:
        # Upload completion commits the assembled meeting before the final
        # ``ready`` status update.  A crash in that tiny window must not leave
        # a manual-only meeting looking as if a hidden pipeline were running.
        imported = session.scalars(select(Meeting).where(
            Meeting.status == "processing", Meeting.deleted_at.is_(None))).all()
        for meeting in imported:
            job = session.scalar(select(ProcessingJob).where(
                ProcessingJob.meeting_id == meeting.id,
                ProcessingJob.stage == "transcribe"))
            if job is not None and job.status in {"pending", "failed", "done"}:
                meeting.status = "ready"

        stuck = session.scalars(select(Meeting).where(
            Meeting.status == "transcribing", Meeting.deleted_at.is_(None))).all()
        for meeting in stuck:
            job = session.scalar(select(ProcessingJob).where(
                ProcessingJob.meeting_id == meeting.id,
                ProcessingJob.stage == "transcribe"))
            if job is None or job.status in {"pending", "running"}:
                if job is not None:
                    job.status = "failed"
                    job.error = "Der Core wurde während der Transkription beendet. Bitte erneut starten."
                meeting.status = "failed"
                report.notes.append(
                    f"manual transcription {meeting.id[:8]} interrupted; marked failed")

    session.commit()
    log.info("recovery_done %s", report.__dict__)
    return report


def _timedelta(seconds: float):
    from datetime import timedelta
    return timedelta(seconds=seconds)
