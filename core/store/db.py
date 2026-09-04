"""Database engine, session factory and bootstrap.

- SQLite in WAL mode for crash resistance and concurrent reads.
- Migrations via Alembic in production; create_all as a dev/test fallback.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import Config
from core.logging_setup import get_logger
from .models import Base

log = get_logger("ma.store")

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(name: str) -> str:
    """Validate and double-quote a SQL identifier for safe interpolation.

    Some statements (e.g. ``PRAGMA table_info(...)``) cannot bind the
    table name, so the value is validated against a strict identifier
    pattern and quoted instead of interpolated raw.
    """
    if not _IDENT_RE.match(name):
        raise ValueError(f"unzulässiger SQL-Identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def _set_pragmas(dbapi_conn, _record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def make_engine(config: Config) -> Engine:
    global _engine, _session_factory
    config.data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        config.db_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
    event.listen(engine, "connect", _set_pragmas)
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database engine not initialized (call make_engine first)")
    return _engine


def get_session_factory() -> sessionmaker:
    if _session_factory is None:
        raise RuntimeError("Session factory not initialized (call make_engine first)")
    return _session_factory


@contextmanager
def session_scope():
    """Short-lived unit-of-work. Commits on success, rolls back on error."""
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Tables created by the *initial* migration (revision 1369a9a766ec). Used to
# recognise a legacy database that already has the base schema but no recorded
# alembic version.
_INITIAL_REVISION = "1369a9a766ec"
_ANALYSIS_REVISION = "8f2c4a91b7d3"
_PHASE8_REVISION = "b7c9d2e4f6a8"
_PROJECTS_REVISION = "c4e8f2a1d6b9"
_VERSIONS_REVISION = "d7f3a1c9e2b4"
_TASK_HISTORY_REVISION = "f1a6c9d3e8b2"
_UPLOADS_REVISION = "e8f4b2c6d1a0"
_TASK_LIFECYCLE_REVISION = "c9e8f1a2b3d4"
_DOCS_REVISION = "d4f7a2b9c6e1"
# Keep this in sync with the actual migration graph.  The previous value was
# an intermediate revision, causing a fully materialized legacy DB to replay
# later migrations unnecessarily (and potentially collide with indexes).
_HEAD_REVISION = "e5a7c9d1f3b2"
_INITIAL_TABLES = frozenset({
    "consent_event", "meeting", "provider_configuration",
    "processing_job", "recording", "transcript_segment",
})


def _db_state(db_path: Path) -> tuple[int, set[str]]:
    """Return (alembic_version_row_count, set_of_table_names) for a DB file.

    A missing file (brand-new database) yields (0, set()). Reads via a raw
    read-only-style connection and never modifies the schema.
    """
    if not db_path.exists():
        return 0, set()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        tables = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        rows = 0
        if "alembic_version" in tables:
            rows = cur.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0]
        return rows, tables
    finally:
        conn.close()


def _has_column(db_path: Path, table: str, column: str) -> bool:
    if not db_path.exists():
        return False
    ident = _quote_ident(table)
    conn = sqlite3.connect(str(db_path))
    try:
        return column in {row[1] for row in conn.execute(f"PRAGMA table_info({ident})")}
    finally:
        conn.close()


def _legacy_stamp_revision(db_path: Path) -> str | None:
    """Revision to `alembic stamp` for a legacy (unversioned) DB, else None.

    A legacy DB has the initial tables but an *empty* `alembic_version`, so a
    plain `upgrade head` would re-run the initial migration and fail with
    "table ... already exists". We stamp instead of deleting anything:
      - all initial tables + `analysis` present  -> stamp "head" (nothing to run)
      - all initial tables present, `analysis`   -> stamp the initial revision
        so `upgrade head` then only adds the remaining migrations.
    """
    rows, tables = _db_state(db_path)
    if rows > 0:
        return None  # alembic already knows its position -> normal upgrade
    if not _INITIAL_TABLES.issubset(tables):
        return None  # brand-new or partial schema -> normal upgrade path
    # ``Base.metadata.create_all()`` was used by older development builds and
    # can leave every current table present without an alembic version.  In
    # that case there is nothing safe for ``upgrade`` to create; stamping the
    # known head avoids replaying CREATE TABLE statements over intact data.
    if "upload_session" in tables:
        # Work forward from the newest schema that is actually visible.  A
        # partially materialized create_all database may contain the newer
        # tables while still missing columns from the final migrations; blindly
        # stamping head would make the next startup fail with missing-column
        # errors.
        if "task_history" not in tables:
            return _UPLOADS_REVISION
        if not (_has_column(db_path, "meeting", "deleted_at")
                and _has_column(db_path, "project", "deleted_at")
                and _has_column(db_path, "task", "project_id")):
            return _TASK_HISTORY_REVISION
        if not (_has_column(db_path, "task", "archived_at")
                and _has_column(db_path, "task", "deleted_at")):
            return "a2c7e9f4b1d6"
        if "project_file" not in tables:
            return _TASK_LIFECYCLE_REVISION
        if ("project_document_chunk" not in tables
                or not _has_column(db_path, "project_file", "extraction_status")):
            return _TASK_LIFECYCLE_REVISION
        if not _has_column(db_path, "project_file", "deleted_at"):
            return _DOCS_REVISION
        return _HEAD_REVISION
    # Older unversioned installations can contain some later tables already
    # (for example after a create_all-based development run). Stamp only the
    # newest schema that is visibly present; upgrade then applies the rest.
    if "transcript_version" in tables:
        return _VERSIONS_REVISION
    if "project" in tables:
        return _PROJECTS_REVISION
    if "segment_embedding" in tables or "task" in tables:
        return _PHASE8_REVISION
    if "analysis" in tables:
        return _ANALYSIS_REVISION
    return _INITIAL_REVISION


def _run_alembic(alembic_args: list[str], migrations_dir: Path,
                 alembic_ini: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(alembic_ini), *alembic_args],
        capture_output=True, text=True, cwd=str(migrations_dir),
    )


def apply_migrations(config: Config) -> None:
    """Run `alembic upgrade head` against the configured DB.

    The alembic env resolves the URL from MA_DB_URL or the config file. A legacy
    database that already carries the base schema but has an empty
    `alembic_version` is first stamped (see `_legacy_stamp_revision`) so the
    initial migration is not re-run and no data is lost.
    """
    os.environ["MA_DB_URL"] = config.db_url
    migrations_dir = Path(__file__).parent / "migrations"
    alembic_ini = migrations_dir / "alembic.ini"

    stamp_rev = _legacy_stamp_revision(config.db_path)
    if stamp_rev is not None:
        stamp = _run_alembic(["stamp", stamp_rev], migrations_dir, alembic_ini)
        if stamp.returncode != 0:
            log.error("alembic_stamp_failed rev=%s\nstdout=%s\nstderr=%s",
                      stamp_rev, stamp.stdout[-2000:], stamp.stderr[-2000:])
            raise RuntimeError(f"Alembic stamp failed ({stamp_rev}): {stamp.stderr[-500:]}")
        log.info("legacy_db_stamped rev=%s", stamp_rev)

    result = _run_alembic(["upgrade", "head"], migrations_dir, alembic_ini)
    if result.returncode != 0:
        log.error("alembic_failed\nstdout=%s\nstderr=%s",
                  result.stdout[-2000:], result.stderr[-2000:])
        raise RuntimeError(f"Alembic migration failed: {result.stderr[-500:]}")
    log.info("migrations_applied head")
    _secure_db_file(config)


def _secure_db_file(config: Config) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = config.db_path.parent / (config.db_path.name + suffix)
        if p.exists():
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass


def has_table(name: str) -> bool:
    engine = get_engine()
    from sqlalchemy import inspect
    return name in inspect(engine).get_table_names()


def table_count(name: str) -> int:
    ident = _quote_ident(name)
    with session_scope() as s:
        return s.execute(text(f"SELECT COUNT(*) FROM {ident}")).scalar()  # pragma: no cover
