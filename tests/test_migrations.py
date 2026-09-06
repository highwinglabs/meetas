"""Migration robustness.

Regression: a *legacy* database already carries the initial tables but its
``alembic_version`` is empty. ``apply_migrations`` must NOT re-run (and crash
on) the initial migration and must NOT lose data -- it stamps the initial
revision and applies only the remaining migrations up to head.

Also guards that a brand-new database still migrates normally.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text

import core.store.db as dbmod
from core.store.db import (
    _INITIAL_TABLES,
    _PRE_TASK_FK_SET_NULL_REVISION,
    _has_index,
    _legacy_stamp_revision,
    _quote_ident,
    _run_alembic,
    _task_meeting_fk_is_set_null,
    apply_migrations,
    get_engine,
    make_engine,
    session_scope,
)
from core.store.models import (
    Analysis,
    Base,
    Meeting,
    ProcessingJob,
    Recording,
    Task,
    TaskHistory,
    TranscriptSegment,
    new_id,
    utcnow,
)

# Keep this in sync with the current migration head.  New migrations must
# advance the head rather than making the production code pretend that the
# previous revision is still current.
_HEAD = "c2d4e6f8a1b3"
# The revision carrying the old Task.meeting_id ON DELETE CASCADE FK, used to
# build a "pre-fix" database for the SET NULL rebuild test.
_PRE_TASK_FK_HEAD = "a1b2c3d4e5f6"


def _migrations_paths():
    migrations_dir = Path(dbmod.__file__).parent / "migrations"
    return migrations_dir, migrations_dir / "alembic.ini"


def _meeting_fk_on_delete(db_path) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        for row in conn.execute('PRAGMA foreign_key_list("task")'):
            if row[2] == "meeting" and row[3] == "meeting_id":
                return row[6]
        return None
    finally:
        conn.close()


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


def test_fresh_db_task_fk_is_set_null(config):
    # A brand-new database migrates all the way to head and ends with the
    # task.meeting_id FK using SET NULL (L6), plus all task indexes intact.
    make_engine(config)
    apply_migrations(config)
    get_engine().dispose()

    assert _alembic_row(config.db_path) == _HEAD
    assert _meeting_fk_on_delete(config.db_path) == "SET NULL"
    assert _task_meeting_fk_is_set_null(config.db_path) is True
    for idx in ("ix_task_meeting_id", "ix_task_project_id", "ix_task_status",
                "ix_task_dedup_key", "ix_task_archived_at",
                "ix_task_deleted_at", "uq_task_dedup_key"):
        assert _has_index(config.db_path, idx), idx


def test_task_fk_migration_preserves_rows_and_history(config):
    # Build a pre-fix DB (task.meeting_id ON DELETE CASCADE) with real data,
    # then run apply_migrations to head.  The SET NULL rebuild must preserve
    # every task and task_history row and make deleting a meeting orphan (not
    # destroy) its task.
    make_engine(config)
    os.environ["MA_DB_URL"] = config.db_url
    migrations_dir, alembic_ini = _migrations_paths()
    _run_alembic(["upgrade", _PRE_TASK_FK_HEAD], migrations_dir, alembic_ini)

    mid, tid = new_id(), new_id()
    with session_scope() as s:
        s.add(Meeting(id=mid, title="M", title_status="auto",
                      start_at=utcnow(), status="done"))
        s.add(Task(id=tid, meeting_id=mid, text="Meeting task"))
        s.add(Task(id=new_id(), meeting_id=None, text="Global task"))
        s.add(TaskHistory(id=new_id(), task_id=tid, field="status",
                          old_value="offen", new_value="erledigt"))
    get_engine().dispose()

    # sanity: pre-fix state is what we claim
    assert _meeting_fk_on_delete(config.db_path) == "CASCADE"
    assert _count(config.db_path, "task") == 2
    assert _count(config.db_path, "task_history") == 1

    apply_migrations(config)  # upgrade head -> runs the SET NULL rebuild
    get_engine().dispose()

    assert _alembic_row(config.db_path) == _HEAD
    assert _meeting_fk_on_delete(config.db_path) == "SET NULL"
    assert _count(config.db_path, "task") == 2, "rebuild dropped task rows"
    assert _count(config.db_path, "task_history") == 1, "drop cascaded history"

    # behaviour: deleting the meeting now orphans the task instead of cascading
    with session_scope() as s:
        meeting = s.get(Meeting, mid)
        s.delete(meeting)
    assert _count(config.db_path, "meeting") == 0
    assert _count(config.db_path, "task") == 2, "task was cascade-deleted"
    conn = sqlite3.connect(str(config.db_path))
    try:
        row = conn.execute("SELECT meeting_id FROM task WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] is None, "task.meeting_id was not set NULL"


def test_legacy_db_task_fk_cascade_stamps_before_head(config):
    # A create_all DB built before the SET NULL migration still carries
    # ON DELETE CASCADE on task.meeting_id; the legacy-stamp heuristic must
    # stamp one revision behind so the rebuild runs.
    make_engine(config)
    Base.metadata.create_all(get_engine())  # new model -> already SET NULL
    with get_engine().begin() as conn:
        # Flip the task FK back to CASCADE to emulate an old installed DB
        # (the table is empty here; only the FK action matters for the test).
        conn.execute(sa.text("DROP TABLE task"))
        conn.execute(sa.text(
            "CREATE TABLE task (id VARCHAR(32) NOT NULL, "
            "meeting_id VARCHAR(32), project_id VARCHAR(32), "
            "source_segment_id VARCHAR(32), source_analysis_id VARCHAR(32), "
            "text TEXT NOT NULL, responsible VARCHAR(256) NOT NULL, "
            "due_date VARCHAR(32) NOT NULL, status VARCHAR(16) NOT NULL, "
            "tags TEXT NOT NULL, dedup_key VARCHAR(64), sort_order INTEGER NOT NULL, "
            "archived_at DATETIME, deleted_at DATETIME, "
            "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "PRIMARY KEY (id), "
            "FOREIGN KEY(meeting_id) REFERENCES meeting (id) ON DELETE CASCADE, "
            "FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE SET NULL)"))
    get_engine().dispose()

    assert _meeting_fk_on_delete(config.db_path) == "CASCADE"
    assert _task_meeting_fk_is_set_null(config.db_path) is False
    assert _legacy_stamp_revision(config.db_path) == _PRE_TASK_FK_SET_NULL_REVISION


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


def test_legacy_db_missing_analysis_index_stamps_before_head(config):
    # A create_all DB built before the analysis unique-index migration lacks
    # the index; it must be stamped one revision behind so the dedup + index
    # migration still runs instead of being skipped.
    make_engine(config)
    Base.metadata.create_all(get_engine())
    with get_engine().begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_analysis_meeting_kind"))
    get_engine().dispose()

    assert _legacy_stamp_revision(config.db_path) == "f3a9c7e5b2d4"

    apply_migrations(config)  # must not raise

    assert _alembic_row(config.db_path) == _HEAD
    assert _has_index(config.db_path, "uq_analysis_meeting_kind")


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


def test_analysis_unique_meeting_kind_dedups_and_enforces(config):
    """Upgrading a DB that already holds duplicate analysis rows for the same
    (meeting, kind) must collapse them (most recent wins) and then enforce
    uniqueness so the upsert path can never create a second row."""
    mid = new_id()
    make_engine(config)
    Base.metadata.create_all(get_engine())
    # Simulate the pre-a1b2c3d4e5f6 schema: no unique index on analysis.
    with get_engine().begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_analysis_meeting_kind"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                     {"v": "f3a9c7e5b2d4"})

    now = utcnow()
    ids = {}
    with session_scope() as s:
        s.add(Meeting(id=mid, title="Dup Analysis", title_status="auto",
                      start_at=now, status="done"))
        # Two rows for the same (meeting, kind): the newer one must survive.
        ids["stale"] = new_id()
        ids["fresh"] = new_id()
        s.add(Analysis(id=ids["stale"], meeting_id=mid, kind="summary",
                       content="alt", model="m", created_at=now, updated_at=now))
        s.add(Analysis(id=ids["fresh"], meeting_id=mid, kind="summary",
                       content="neu", model="m",
                       created_at=now + timedelta(seconds=5),
                       updated_at=now + timedelta(seconds=5)))
    get_engine().dispose()

    apply_migrations(config)

    conn = sqlite3.connect(str(config.db_path))
    try:
        assert _alembic_row(config.db_path) == _HEAD
        rows = conn.execute(
            "SELECT id, content FROM analysis WHERE meeting_id=?", (mid,)).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == ids["fresh"]
        assert rows[0][1] == "neu"  # most recently modified analysis kept

        # The unique index now exists and is enforced.
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            ("uq_analysis_meeting_kind",)).fetchone() is not None
    finally:
        conn.close()


def test_analysis_unique_index_rejects_duplicate_insert(config):
    """On a migrated (head) database the unique index must reject a second row
    for the same (meeting, kind) at the database level."""
    mid = new_id()
    make_engine(config)
    Base.metadata.create_all(get_engine())
    apply_migrations(config)

    now = utcnow()
    with session_scope() as s:
        s.add(Meeting(id=mid, title="Unique", title_status="auto",
                      start_at=now, status="done"))
        s.add(Analysis(id=new_id(), meeting_id=mid, kind="summary",
                       content="erst", model="m", created_at=now, updated_at=now))
    get_engine().dispose()

    from sqlalchemy.exc import IntegrityError
    # A raw insert bypassing the upsert must be rejected at the DB level.
    eng = get_engine()
    try:
        with eng.begin() as conn:
            conn.execute(text(
                "INSERT INTO analysis (id, meeting_id, kind, content, "
                "created_at, updated_at) VALUES (:i, :m, 'summary', 'dup', :t, :t)"),
                {"i": new_id(), "m": mid, "t": now.isoformat()})
            raised = False
    except IntegrityError:
        raised = True
    finally:
        eng.dispose()
    assert raised
