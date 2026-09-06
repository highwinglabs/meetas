"""ORM models (Phase 1, forward-compatible).

Phase 1 populates Meeting, Recording, ConsentEvent, ProviderConfiguration and
ProcessingJob. TranscriptSegment is defined now so Phase 2 (ASR) needs no new
migration for the core transcript entity. All relationships are referentially
consistent (FK + ON DELETE CASCADE).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, foreign, relationship


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    # naive UTC for stable SQLite storage
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Project(Base):
    """A user-managed folder for related meetings and future project files."""
    __tablename__ = "project"

    id = Column(String(32), primary_key=True, default=new_id)
    name = Column(String(512), nullable=False)
    description = Column(Text, default="", nullable=False)
    status = Column(String(16), default="active", nullable=False)  # active | archived
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    # The physical FK is added by the additive migration.  Keeping the ORM
    # join explicit also lets the legacy schema be inspected safely before the
    # migration has added the new project table.
    meetings = relationship(
        "Meeting", back_populates="project",
        primaryjoin=lambda: foreign(Meeting.project_id) == Project.id,
    )
    files = relationship("ProjectFile", back_populates="project",
                         cascade="all, delete-orphan")


class Meeting(Base):
    __tablename__ = "meeting"

    id = Column(String(32), primary_key=True, default=new_id)
    title = Column(String(512), default="Neues Meeting", nullable=False)
    title_status = Column(String(16), default="auto", nullable=False)  # auto | manual
    start_at = Column(DateTime, default=utcnow, nullable=False)
    end_at = Column(DateTime, nullable=True)
    duration_s = Column(Float, nullable=True)
    lang = Column(String(8), nullable=True)
    project_id = Column(String(32), nullable=True, index=True)
    # Per-meeting overrides for ASR/live/diarization/analysis/search settings.
    # JSON keeps this additive and forward-compatible without duplicating the
    # global Config schema in the relational model.
    settings_json = Column(Text, default="{}", nullable=False)
    # recording | paused | ready | transcribing | analyzing | done | failed | archived
    status = Column(String(16), default="recording", nullable=False, index=True)
    deleted_at = Column(DateTime, nullable=True, index=True)
    archived_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    recordings = relationship("Recording", back_populates="meeting",
                              cascade="all, delete-orphan")
    segments = relationship("TranscriptSegment", back_populates="meeting",
                            cascade="all, delete-orphan")
    jobs = relationship("ProcessingJob", back_populates="meeting",
                        cascade="all, delete-orphan")
    analyses = relationship("Analysis", back_populates="meeting",
                            cascade="all, delete-orphan")
    embeddings = relationship("SegmentEmbedding", back_populates="meeting",
                              cascade="all, delete-orphan")
    tags = relationship("MeetingTag", back_populates="meeting",
                        cascade="all, delete-orphan")
    speakers = relationship("SpeakerLabel", back_populates="meeting",
                            cascade="all, delete-orphan")
    markers = relationship("Marker", back_populates="meeting",
                           cascade="all, delete-orphan")
    revisions = relationship("Revision", back_populates="meeting",
                             cascade="all, delete-orphan")
    transcript_versions = relationship("TranscriptVersion", back_populates="meeting",
                                       cascade="all, delete-orphan")
    project = relationship(
        "Project", back_populates="meetings",
        primaryjoin=lambda: foreign(Meeting.project_id) == Project.id,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Meeting {self.id} {self.status} {self.title!r}>"


class Recording(Base):
    __tablename__ = "recording"

    id = Column(String(32), primary_key=True, default=new_id)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    source = Column(String(16), default="mic", nullable=False)  # mic | system | both
    device = Column(String(256), nullable=True)
    sample_rate = Column(Integer, nullable=True)
    channels = Column(Integer, nullable=True)
    original_path = Column(String(1024), nullable=True)
    # recording | paused | assembled | failed
    status = Column(String(16), default="recording", nullable=False)
    in_progress = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    meeting = relationship("Meeting", back_populates="recordings")


class TranscriptSegment(Base):
    __tablename__ = "transcript_segment"

    id = Column(String(32), primary_key=True, default=new_id)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    start_s = Column(Float, nullable=False)
    end_s = Column(Float, nullable=False)
    text = Column(Text, default="", nullable=False)
    raw_text = Column(Text, default="", nullable=False)
    language = Column(String(8), nullable=True)
    confidence = Column(Float, nullable=True)
    audio_ref = Column(String(1024), nullable=True)
    # Phase 1: free label ("Sprecher 1"). Later: FK to Speaker/SpeakerProfile.
    speaker_id = Column(String(64), nullable=True)
    # vorlaeufig | bestaetigt | manuell
    status = Column(String(16), default="vorlaeufig", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    meeting = relationship("Meeting", back_populates="segments")


class TranscriptVersion(Base):
    """Immutable snapshot metadata for each completed transcription run."""
    __tablename__ = "transcript_version"

    id = Column(String(32), primary_key=True, default=new_id)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    version_no = Column(Integer, nullable=False)
    model = Column(String(128), nullable=False)
    language = Column(String(8), nullable=True)
    segments_json = Column(Text, default="[]", nullable=False)
    is_current = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    meeting = relationship("Meeting", back_populates="transcript_versions")


class ProcessingJob(Base):
    __tablename__ = "processing_job"

    id = Column(String(32), primary_key=True, default=new_id)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    # capture | transcribe | diarize | embed | analyze
    stage = Column(String(16), nullable=False)
    # pending | running | done | failed | cancelled
    status = Column(String(16), default="pending", nullable=False, index=True)
    progress = Column(Float, default=0.0, nullable=False)
    error = Column(Text, nullable=True)
    retries = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        # One logical job per (meeting, stage); guards get_or_create against a
        # duplicate insert under concurrency.
        Index("uq_processing_job", "meeting_id", "stage", unique=True),
        {},
    )

    meeting = relationship("Meeting", back_populates="jobs")


class ConsentEvent(Base):
    __tablename__ = "consent_event"

    id = Column(String(32), primary_key=True, default=new_id)
    ts = Column(DateTime, default=utcnow, nullable=False)
    acknowledged = Column(Boolean, default=False, nullable=False)
    text = Column(Text, default="", nullable=False)


class ProviderConfiguration(Base):
    __tablename__ = "provider_configuration"

    id = Column(String(32), primary_key=True, default=new_id)
    kind = Column(String(16), nullable=False)  # asr | llm | embedding | diar
    name = Column(String(128), nullable=False)
    endpoint = Column(String(512), nullable=True)
    model = Column(String(128), nullable=True)
    params_json = Column(Text, default="{}", nullable=False)
    local = Column(Boolean, default=True, nullable=False)
    # reference into the secret store, NEVER the key itself
    api_key_ref = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class Analysis(Base):
    """A stored LLM analysis of a meeting (Phase 3). One row per (meeting, kind)."""
    __tablename__ = "analysis"
    __table_args__ = (
        # Enforced in the database too (migration a1b2c3d4e5f6): the write
        # path is a select-then-insert upsert, so the unique index guards
        # against concurrent/duplicate rows the same way as the job/task keys.
        Index("uq_analysis_meeting_kind", "meeting_id", "kind", unique=True),
    )

    id = Column(String(32), primary_key=True, default=new_id)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    # summary | action_items | ... (forward-compatible; only "summary" in MVP)
    kind = Column(String(16), default="summary", nullable=False)
    model = Column(String(128), nullable=True)  # which model produced it
    # Language the analysis *content* was written in (resolved at analysis time).
    # NULL for legacy rows -> renderers fall back to German (historical default).
    output_lang = Column(String(8), nullable=True)
    content = Column(Text, default="", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    meeting = relationship("Meeting", back_populates="analyses")


class SegmentEmbedding(Base):
    """A dense vector for a transcript segment (Phase 5 semantic search).

    ``vector`` is the packed little-endian float32 payload; ``dim`` records its
    length. One row per (meeting, segment, model) so the index can be rebuilt
    when the embedding model changes without touching the transcript.
    """
    __tablename__ = "segment_embedding"

    id = Column(String(32), primary_key=True, default=new_id)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    seg_id = Column(String(32), nullable=False)
    model = Column(String(128), nullable=False, default="hashing")
    dim = Column(Integer, nullable=False, default=0)
    vector = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        # Re-embedding is idempotent per (meeting, segment, model).
        # (a plain UniqueConstraint is added in the migration too.)
        Index("uq_segment_embedding", "meeting_id", "seg_id", "model", unique=True),
        {},
    )
    meeting = relationship("Meeting", back_populates="embeddings")


class MeetingTag(Base):
    """A free-form tag on a meeting (Phase 7 auto/manual tagging)."""
    __tablename__ = "meeting_tag"

    id = Column(String(32), primary_key=True, default=new_id)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    tag = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    meeting = relationship("Meeting", back_populates="tags")


class SpeakerLabel(Base):
    """A user-assigned display name for a diarized speaker in one meeting (Phase 4).

    ``speaker_id`` is the label the diarizer produced (e.g. "Sprecher 2");
    ``label`` is the human name (e.g. "Dr. Berger"). Stored per meeting so the
    same speaker id in two meetings can have different names.
    """
    __tablename__ = "speaker_label"

    id = Column(String(32), primary_key=True, default=new_id)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    speaker_id = Column(String(64), nullable=False)
    label = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    meeting = relationship("Meeting", back_populates="speakers")


class Marker(Base):
    """A user-placed marker on the recording timeline (Phase 7)."""
    __tablename__ = "marker"

    id = Column(String(32), primary_key=True, default=new_id)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    at_s = Column(Float, nullable=False)
    text = Column(Text, default="", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    meeting = relationship("Meeting", back_populates="markers")


class Revision(Base):
    """An auditable edit to a transcript segment (Phase 7, "revisions").

    Editing a segment keeps the original text and appends a revision row, so the
    transcript history is never silently lost.
    """
    __tablename__ = "revision"

    id = Column(String(32), primary_key=True, default=new_id)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    segment_id = Column(String(32), nullable=False)
    field = Column(String(32), default="text", nullable=False)
    original = Column(Text, default="", nullable=False)
    updated = Column(Text, default="", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    meeting = relationship("Meeting", back_populates="revisions")


class Task(Base):
    """A central, actionable task extracted from a meeting (Phase 6).

    ``meeting_id`` is nullable so a task can be promoted to the global overview
    and edited independently; ``source_segment_id`` / ``source_analysis_id`` keep
    the traceable anchor to where it came from (never invented).
    """
    __tablename__ = "task"

    id = Column(String(32), primary_key=True, default=new_id)
    # meeting_id is nullable so a task can be promoted to the global overview
    # and edited independently; orphaning (SET NULL) on meeting deletion keeps
    # the task (matching project_id) instead of cascading it away (L6).
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="SET NULL"),
                        nullable=True, index=True)
    project_id = Column(String(32), ForeignKey("project.id", ondelete="SET NULL"),
                        nullable=True, index=True)
    source_segment_id = Column(String(32), nullable=True)
    source_analysis_id = Column(String(32), nullable=True)
    text = Column(Text, default="", nullable=False)
    responsible = Column(String(256), default="nicht angegeben", nullable=False)
    due_date = Column(String(32), default="nicht angegeben", nullable=False)
    # offen | laeuft | erledigt
    status = Column(String(16), default="offen", nullable=False, index=True)
    tags = Column(Text, default="", nullable=False)
    # idempotency key: stable hash of (meeting, text) so re-analysis doesn't dup.
    dedup_key = Column(String(64), nullable=True, index=True)
    sort_order = Column(Integer, default=0, nullable=False)
    # Soft lifecycle state: archived tasks remain recoverable, while deleted
    # tasks are shown in the task paper bin until explicitly purged.
    archived_at = Column(DateTime, nullable=True, index=True)
    deleted_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        # Idempotency guard: one row per dedup_key. NULL keys (manual tasks) are
        # distinct in SQLite, so only extracted tasks are constrained.
        Index("uq_task_dedup_key", "dedup_key", unique=True),
        {},
    )


class TaskHistory(Base):
    """Immutable audit trail for task status/owner changes."""
    __tablename__ = "task_history"

    id = Column(String(32), primary_key=True, default=new_id)
    task_id = Column(String(32), ForeignKey("task.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="SET NULL"),
                        nullable=True, index=True)
    field = Column(String(32), nullable=False)
    old_value = Column(Text, nullable=False)
    new_value = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class BackupRecord(Base):
    """A bookkeeping row for a local backup (Phase 8: backup/restore)."""
    __tablename__ = "backup_record"

    id = Column(String(32), primary_key=True, default=new_id)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    kind = Column(String(16), default="full", nullable=False)  # full | db
    path = Column(String(1024), nullable=False)
    size = Column(Integer, default=0, nullable=False)
    manifest_json = Column(Text, default="{}", nullable=False)
    note = Column(String(512), default="", nullable=False)


class ProjectFile(Base):
    """A local file belonging to a project or an imported meeting.

    Project documents are extracted locally into bounded searchable chunks;
    audio/video files remain linked to their imported meeting.
    """
    __tablename__ = "project_file"

    id = Column(String(32), primary_key=True, default=new_id)
    project_id = Column(String(32), ForeignKey("project.id", ondelete="CASCADE"),
                        nullable=True, index=True)
    meeting_id = Column(String(32), ForeignKey("meeting.id", ondelete="SET NULL"),
                        nullable=True, index=True)
    original_name = Column(String(512), nullable=False)
    path = Column(String(1024), nullable=False)
    mime_type = Column(String(128), nullable=True)
    kind = Column(String(32), default="document", nullable=False)
    size = Column(Integer, default=0, nullable=False)
    extraction_status = Column(String(16), default="pending", nullable=False, index=True)
    extraction_error = Column(Text, nullable=True)
    extracted_chars = Column(Integer, default=0, nullable=False)
    indexed_at = Column(DateTime, nullable=True)
    deleted_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    project = relationship("Project", back_populates="files")
    chunks = relationship("ProjectDocumentChunk", back_populates="file",
                          cascade="all, delete-orphan")


class ProjectDocumentChunk(Base):
    """Locally extracted, searchable text belonging to one project file."""
    __tablename__ = "project_document_chunk"

    id = Column(String(64), primary_key=True)
    project_file_id = Column(String(32), ForeignKey("project_file.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    project_id = Column(String(32), ForeignKey("project.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    locator = Column(String(256), default="", nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    file = relationship("ProjectFile", back_populates="chunks")


class UploadSession(Base):
    """Crash-resumable local upload before it becomes a project file/meeting."""
    __tablename__ = "upload_session"

    id = Column(String(32), primary_key=True, default=new_id)
    filename = Column(String(512), nullable=False)
    temp_path = Column(String(1024), nullable=False)
    total_size = Column(Integer, nullable=False)
    received_size = Column(Integer, default=0, nullable=False)
    title = Column(String(512), nullable=True)
    project_id = Column(String(32), nullable=True, index=True)
    start_at = Column(DateTime, nullable=True)
    settings_json = Column(Text, default="{}", nullable=False)
    content_type = Column(String(128), nullable=True)
    # uploading | paused | completed | cancelled | failed
    status = Column(String(16), default="uploading", nullable=False, index=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
