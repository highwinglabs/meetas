"""Migration robustness.

Regression: a *legacy* database already carries the initial tables but its
``alembic_version`` is empty. ``apply_migrations`` must NOT re-run (and crash
on) the initial migration and must NOT lose data -- it stamps the initial
revision and applies only the remaining migrations up to head.

Also guards that a brand-new database still migrates normally.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from sqlalchemy import text

from core.store.db import (
    _INITIAL_TABLES,
    _legacy_stamp_revision,
    _quote_ident,
    apply_migrations,
    get_engine,
    make_engine,
    session_scope,
)
from core.store.models import (
    Base,
    Meeting,
    ProcessingJob,
    Recording,
    Task,
    TranscriptSegment,
    new_id,
    utcnow,
)

# Keep this in sync with the current migration head.  New migrations must
# advance the head rather than making the production code pretend that the
# previous revision is still current.
_HEAD = "e5a7c9d1f3b2"


def _tables(db_path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _alembic_row(db_path) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT version_num FROM alembic_version")
        rows = [r[0] for r in cur]
        return rows[0] if len(rows) == 1 else (",".join(rows) or None)
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def _count(db_path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_fresh_db_migrates_normally(config):
    make_engine(config)
    assert _legacy_stamp_revision(config.db_path) is None  # nothing to stamp
    apply_migrations(config)

    tables = _tables(config.db_path)
    assert {"meeting", "recording", "transcript_segment", "analysis"}.issubset(tables)
    assert _alembic_row(config.db_path) == _HEAD


def test_legacy_db_empty_version_stamps_and_preserves_data(config):
    # 1) Build a legacy DB: exactly the initial tables, NO alembic_version,
    #    NO analysis table.
    make_engine(config)
    initial = [Base.metadata.tables[n] for n in _INITIAL_TABLES]
    Base.metadata.create_all(get_engine(), tables=initial)

    mid = new_id()
    with session_scope() as s:
        s.add(Meeting(id=mid, title="Legacy Meeting", title_status="auto",
                      start_at=utcnow(), status="done"))
        s.add(Recording(id=new_id(), meeting_id=mid, source="mic",
                        status="assembled", in_progress=False, original_path="/x.wav"))
        s.add(TranscriptSegment(id=new_id(), meeting_id=mid, start_s=0.0, end_s=1.0,
                                text="Hallo", raw_text="Hallo",
                                speaker_id="Sprecher 1", status="bestaetigt"))
        s.add(TranscriptSegment(id=new_id(), meeting_id=mid, start_s=1.0, end_s=2.0,
                                text="Welt", raw_text="Welt",
                                speaker_id="Sprecher 1", status="bestaetigt"))
    get_engine().dispose()

    # sanity: the legacy state is what we claim (empty version, no analysis)
    tables_before = _tables(config.db_path)
    assert "alembic_version" not in tables_before
    assert "analysis" not in tables_before
    assert _legacy_stamp_revision(config.db_path) == "1369a9a766ec"

    # 2) Migrate: must not raise and must not re-create existing tables.
    apply_migrations(config)

    # 3) Schema reached head (analysis added), version stamped.
    tables_after = _tables(config.db_path)
    assert "analysis" in tables_after
    assert _alembic_row(config.db_path) == _HEAD

    # 4) All legacy meetings / recordings / segments are preserved.
    assert _count(config.db_path, "meeting") == 1
    assert _count(config.db_path, "recording") == 1
    assert _count(config.db_path, "transcript_segment") == 2
    conn = sqlite3.connect(str(config.db_path))
    try:
        title = conn.execute("SELECT title FROM meeting WHERE id=?", (mid,)).fetchone()
    finally:
        conn.close()
    assert title is not None and title[0] == "Legacy Meeting"


def test_legacy_db_fully_present_stamps_head(config):
    # Edge: legacy DB already has the analysis table too -> stamp straight to
    # head (nothing to run) instead of crashing on a CREATE analysis.
    make_engine(config)
    Base.metadata.create_all(get_engine())  # all tables incl. analysis, no version
    get_engine().dispose()
    assert "analysis" in _tables(config.db_path)
    assert _alembic_row(config.db_path) is None

    apply_migrations(config)  # must not raise

    assert _alembic_row(config.db_path) == _HEAD


def test_quote_ident_validates_identifiers(config):
    """The identifier guard used by PRAGMA/SELECT interpolation must quote valid
    identifiers and reject anything else (no raw SQL injection)."""
    assert _quote_ident("meeting") == '"meeting"'
    assert _quote_ident("_task2") == '"_task2"'
    for bad in ("", "1abc", "a b", "meeting-x", "meeting; DROP TABLE x",
                'a"b"', "a.b"):
        try:
            _quote_ident(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")


def _build_dup_state(config, mid: str) -> dict:
    """Create the full schema, drop the two dedup unique indexes (simulating the
    pre-``b7d4e6f8a9c1`` state), stamp the DB at the previous head, and insert
    *conflicting* duplicate jobs and tasks. Returns the ids of the rows we
    expect to survive the smart merge."""
    make_engine(config)
    Base.metadata.create_all(get_engine())
    # Simulate the schema exactly as it was before the unique keys existed.
    with get_engine().begin() as conn:
        for idx in ("uq_processing_job", "uq_task_dedup_key"):
            conn.execute(text(f"DROP INDEX IF EXISTS {idx}"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                     {"v": "a2c7e9f4b1d6"})

    now = utcnow()
    ids = {}
    with session_scope() as s:
        s.add(Meeting(id=mid, title="Dup Meeting", title_status="auto",
                      start_at=now, status="done"))
        # --- conflicting jobs: same (meeting, stage), different status/age ---
        # A newer `pending` dup must NOT beat an older `done` job.
        ids["job_done"] = new_id()
        ids["job_pending"] = new_id()
        s.add(ProcessingJob(id=ids["job_done"], meeting_id=mid, stage="transcribe",
                            status="done", progress=1.0, retries=0,
                            created_at=now, updated_at=now))
        s.add(ProcessingJob(id=ids["job_pending"], meeting_id=mid, stage="transcribe",
                            status="pending", progress=0.0, retries=0,
                            created_at=now + timedelta(seconds=5),
                            updated_at=now + timedelta(seconds=5)))
        # A `failed` job must lose to a `running` one.
        ids["job_running"] = new_id()
        ids["job_failed"] = new_id()
        s.add(ProcessingJob(id=ids["job_running"], meeting_id=mid, stage="analyze",
                            status="running", progress=0.4, retries=1,
                            created_at=now, updated_at=now + timedelta(seconds=1)))
        s.add(ProcessingJob(id=ids["job_failed"], meeting_id=mid, stage="analyze",
                            status="failed", progress=0.0, retries=2, error="boom",
                            created_at=now + timedelta(seconds=9),
                            updated_at=now + timedelta(seconds=9)))
        # --- conflicting tasks: same dedup_key, different edit time ---
        # The most recently edited task must win (manual change preserved).
        ids["task_stale"] = new_id()
        ids["task_edited"] = new_id()
        s.add(Task(id=ids["task_stale"], meeting_id=mid, dedup_key="dk-1",
                   text="Budget klären", responsible="Alt", due_date="nicht angegeben",
                   status="offen", sort_order=0,
                   created_at=now, updated_at=now))
        s.add(Task(id=ids["task_edited"], meeting_id=mid, dedup_key="dk-1",
                   text="Budget klären", responsible="Neu", due_date="2026-09-01",
                   status="laeuft", sort_order=0,
                   created_at=now + timedelta(seconds=5),
                   updated_at=now + timedelta(seconds=5)))
    get_engine().dispose()
    return ids


def test_dup_migration_smart_merges_jobs_and_tasks(config):
    """Upgrading a DB that already contains duplicate jobs/tasks must collapse
    them with a *semantic* winner: the most advanced job status and the most
    recently edited task survive -- not merely the earliest row."""
    mid = new_id()
    ids = _build_dup_state(config, mid)

    apply_migrations(config)  # runs b7d4e6f8a9c1

    assert _alembic_row(config.db_path) == _HEAD
    conn = sqlite3.connect(str(config.db_path))
    try:
        # Each (meeting, stage) now has exactly one job, and it is the winner.
        jobs = {r[0]: r[1] for r in conn.execute(
            "SELECT stage, status FROM processing_job WHERE meeting_id=?", (mid,))}
        assert set(jobs) == {"transcribe", "analyze"}
        assert jobs["transcribe"] == "done"       # done beat the newer pending
        assert jobs["analyze"] == "running"       # running beat the newer failed

        kept_task = conn.execute(
            "SELECT id, responsible, status FROM task WHERE dedup_key='dk-1'").fetchall()
        assert len(kept_task) == 1
        assert kept_task[0][0] == ids["task_edited"]
        assert kept_task[0][1] == "Neu"           # most recent manual change kept
        assert kept_task[0][2] == "laeuft"

        # The unique indexes now exist and are enforced.
        for idx in ("uq_processing_job", "uq_task_dedup_key"):
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
                (idx,)).fetchone() is not None
    finally:
        conn.close()


def test_dup_migration_idempotent_on_rerun(config):
    """Re-running the migration (or a fresh head DB) never loses the merged rows
    nor errors on the already-present unique indexes."""
    mid = new_id()
    _build_dup_state(config, mid)
    apply_migrations(config)
    apply_migrations(config)  # second run: no-op, must not raise

    conn = sqlite3.connect(str(config.db_path))
    try:
        assert _count(config.db_path, "processing_job") == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM task WHERE dedup_key='dk-1'").fetchone()[0] == 1
    finally:
        conn.close()
