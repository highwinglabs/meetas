"""Database persistence: entity CRUD, WAL mode, FK cascade, secure file perms."""
from __future__ import annotations

import os
import sqlite3

from sqlalchemy import select

from core.store.db import get_engine, has_table, make_engine, session_scope
from core.store.models import (
    Base, Meeting, ProcessingJob, ProviderConfiguration, Recording, TranscriptSegment,
)


def _init(config):
    make_engine(config)
    Base.metadata.create_all(get_engine())


def test_tables_created(config):
    _init(config)
    for t in ("meeting", "recording", "transcript_segment",
              "processing_job", "consent_event", "provider_configuration"):
        assert has_table(t), f"missing table {t}"


def test_wal_mode_enabled(config):
    _init(config)
    conn = sqlite3.connect(config.db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_entity_roundtrip_and_cascade(config):
    _init(config)
    with session_scope() as s:
        m = Meeting(title="Roundtrip", status="ready")
        s.add(m)
        s.flush()
        s.add(Recording(meeting_id=m.id, source="mic", original_path="/x/original.wav",
                        status="assembled", in_progress=False))
        s.add(TranscriptSegment(meeting_id=m.id, start_s=0.0, end_s=1.5,
                                text="Hallo", raw_text="Hallo", speaker_id="Sprecher 1"))
        s.add(ProcessingJob(meeting_id=m.id, stage="transcribe", status="done"))
        s.add(ProviderConfiguration(kind="asr", name="faster-whisper",
                                    model="small", local=True))
        s.commit()
        mid = m.id

    with session_scope() as s:
        m = s.get(Meeting, mid)
        assert m.title == "Roundtrip"
        assert len(m.recordings) == 1
        assert len(m.segments) == 1
        assert m.segments[0].text == "Hallo"
        assert len(m.jobs) == 1
        assert m.jobs[0].stage == "transcribe"
        prov = s.scalars(select(ProviderConfiguration)).all()
        assert prov[0].kind == "asr"

    # FK cascade: deleting the meeting removes children
    with session_scope() as s:
        m = s.get(Meeting, mid)
        s.delete(m)
        s.commit()
    with session_scope() as s:
        assert s.get(Meeting, mid) is None
        assert s.scalar(select(Recording).where(Recording.meeting_id == mid)) is None
        assert s.scalar(select(TranscriptSegment).where(TranscriptSegment.meeting_id == mid)) is None
        assert s.scalar(select(ProcessingJob).where(ProcessingJob.meeting_id == mid)) is None


def test_db_file_has_restrictive_perms(config):
    _init(config)
    import core.store.db as dbmod
    dbmod._secure_db_file(config)
    mode = oct(os.stat(config.db_path).st_mode & 0o777)
    assert mode == "0o600", f"expected 0600, got {mode}"
