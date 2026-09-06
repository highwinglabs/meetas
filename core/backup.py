"""Local backup + restore (Phase 7c).

Everything is offline and file-based:
* A backup is a *consistent online snapshot* of the SQLite DB (taken via the
  SQLite backup API, so a live DB is not corrupted by a mid-write copy). The
  FTS index lives in the same DB and is therefore backed up too.
* ``kind="db"`` snapshots the DB only; ``kind="full"`` additionally archives the
  user-content dirs (audio + exports) into a ``.zip`` next to the snapshot.
* Restore is safety-first: it always takes a *pre-restore* safety snapshot of
  the current DB, verifies the backup's integrity, and performs an atomic file
  swap. Without ``confirm=True`` it is a dry-run that changes nothing.

No user data is ever deleted and no network is used.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from core.config import Config, get_config
from core.logging_setup import get_logger
from core.store.db import get_engine, session_scope
from core.store.models import BackupRecord, new_id, utcnow

log = get_logger("ma.backup")


def _backups_dir(cfg: Config) -> Path:
    d = cfg.base_dir / "backups"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _safe_backup_path(cfg: Config, raw: str | Path) -> Path:
    """Resolve a recorded backup path inside the application backup store."""
    root = _backups_dir(cfg).resolve()
    try:
        path = Path(raw).resolve()
    except (TypeError, ValueError, OSError, RuntimeError) as exc:
        raise ValueError("Backuppfad ist ungültig.") from exc
    if root not in path.parents or path == root or not path.is_file():
        raise ValueError("Backup liegt außerhalb des lokalen Backup-Speichers.")
    return path


def _snapshot_db(src: Path, dst: Path) -> None:
    """Write a consistent online snapshot of the live SQLite DB to ``dst``."""
    if not src.exists():
        dst.write_bytes(b"")
        return
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dst))
    try:
        with src_conn:
            src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def _integrity(path: Path) -> str:
    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return row[0] if row else "unknown"
        finally:
            conn.close()
    except sqlite3.Error:
        return "unreadable"


def _schema_is_compatible(path: Path) -> bool:
    """Ensure a physically valid snapshot is also a meeting-assistant DB."""
    try:
        with sqlite3.connect(str(path)) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            return {"meeting", "recording", "transcript_segment"}.issubset(names)
    except sqlite3.Error:
        return False


def _archive_data_dirs(cfg: Config, base_name: str) -> Path | None:
    """Zip the user-content dirs (audio + exports) if any contain files."""
    dirs = [cfg.audio_dir, cfg.exports_dir]
    have = [d for d in dirs if d.exists() and any(d.iterdir())]
    if not have:
        return None
    out = _backups_dir(cfg) / f"{base_name}_data.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in have:
            for f in d.rglob("*"):
                if not f.is_file() or f.is_symlink():
                    continue
                resolved = f.resolve()
                if d.resolve() not in resolved.parents:
                    continue
                zf.write(resolved, arcname=f.relative_to(cfg.base_dir))
    try:
        out.chmod(0o600)
    except OSError:
        pass
    return out


def create_backup(cfg: Config | None = None, kind: str = "db",
                  note: str = "") -> dict:
    """Create a local backup. Returns the backup descriptor dict."""
    cfg = cfg or get_config()
    if kind not in ("db", "full"):
        raise ValueError("backup kind muss 'db' oder 'full' sein")
    cfg.ensure_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # A short unique suffix keeps rapid consecutive backups from colliding on
    # the same file (they could land in the same second/minute).
    base_name = f"meeting_assistant_{ts}_{kind}_{new_id()[:6]}"
    db_path = _backups_dir(cfg) / f"{base_name}.db"
    _snapshot_db(cfg.db_path, db_path)
    size = db_path.stat().st_size
    integrity = _integrity(db_path)
    # Reject empty or invalid snapshots up front: a 0-byte / unreadable snapshot
    # is not a usable backup and must never be recorded as one, otherwise it
    # would look restorable but fail (or wipe data) at restore time.
    if size == 0 or integrity != "ok":
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(
            f"Backup-Snapshot ist ungültig (size={size}, integrity={integrity}); "
            f"es wurde kein Backup erstellt.")
    try:
        db_path.chmod(0o600)
    except OSError:
        pass

    data_zip = None
    if kind == "full":
        try:
            data_zip = _archive_data_dirs(cfg, base_name)
        except Exception:
            db_path.unlink(missing_ok=True)
            raise

    manifest = {
        "kind": kind,
        "db_size": db_path.stat().st_size,
        "integrity": integrity,
        "data_zip": str(data_zip) if data_zip else None,
        "db_tables": _table_count(db_path),
    }
    rec_id = new_id()
    try:
        with session_scope() as s:
            s.add(BackupRecord(
                id=rec_id, kind=kind, path=str(db_path),
                size=manifest["db_size"],
                manifest_json=__import__("json").dumps(manifest),
                note=note, created_at=utcnow()))
            s.commit()
    except Exception:
        db_path.unlink(missing_ok=True)
        if data_zip:
            data_zip.unlink(missing_ok=True)
        raise
    _prune(cfg, keep=cfg.backup_retention)
    log.info("backup_done id=%s kind=%s path=%s size=%s", rec_id[:8], kind,
             db_path, manifest["db_size"])
    return {
        "id": rec_id, "kind": kind, "path": str(db_path),
        "size": manifest["db_size"], "integrity": integrity,
        "data_zip": str(data_zip) if data_zip else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "note": note,
    }


def _table_count(db_path: Path) -> int:
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return len(rows)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _prune(cfg: Config, keep: int) -> None:
    """Delete only the *oldest backup files* beyond ``keep``. Never user data."""
    if keep <= 0:
        return
    bdir = _backups_dir(cfg)
    # Tie-break on the filename (which embeds the creation timestamp) so the
    # "oldest" is deterministic even when two backups share an mtime.
    files = sorted(bdir.glob("meeting_assistant_*.db"),
                   key=lambda p: (p.stat().st_mtime, p.name))
    excess = len(files) - keep
    removed: list[Path] = []
    for p in files[:max(0, excess)]:
        try:
            p.unlink()
            removed.append(p)
        except OSError:
            continue
        # A kind="full" backup stores user content in a sibling
        # ``{base_name}_data.zip``; remove it together with its snapshot so it
        # is not orphaned and accumulates forever.  If the DB unlink failed,
        # keep the archive too: it is still part of a usable backup.
        zip_path = p.with_name(p.stem + "_data.zip")
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
    # Keep bookkeeping and the filesystem in agreement.  A failed unlink is
    # deliberately retained so the integrity report can still show it.
    if removed:
        with session_scope() as s:
            for path in removed:
                s.query(BackupRecord).filter(BackupRecord.path == str(path)).delete(
                    synchronize_session=False)
            s.commit()
    # Clean orphaned data archives left by older versions or interrupted
    # pruning.  They are never user data: only the backup sibling is removed.
    db_names = {p.name for p in bdir.glob("meeting_assistant_*.db")}
    for archive in bdir.glob("meeting_assistant_*_data.zip"):
        db_name = archive.name[:-len("_data.zip")] + ".db"
        if db_name not in db_names:
            try:
                archive.unlink()
            except OSError:
                pass


def list_backups(cfg: Config | None = None) -> list:
    cfg = cfg or get_config()
    with session_scope() as s:
        rows = s.scalars(select(BackupRecord).order_by(
            BackupRecord.created_at.desc())).all()
        out = []
        for r in rows:
            try:
                exists = _safe_backup_path(cfg, r.path).is_file()
            except (OSError, TypeError, ValueError, RuntimeError):
                exists = False
            created_at = None
            if r.created_at:
                created = r.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                created_at = created.astimezone(timezone.utc).isoformat()
            out.append(
            {"id": r.id, "kind": r.kind, "path": r.path, "size": r.size,
             "created_at": created_at,
             "note": r.note,
             "exists": exists})
        return out


def restore_backup(cfg: Config, backup_id: str, confirm: bool = False,
                   *, pre_write_guard: "Callable[[], None] | None" = None) -> dict:
    """Restore the live DB from a backup.

    Without ``confirm=True`` this is a dry-run (no changes). With it, it first
    takes a pre-restore safety snapshot, verifies the backup, then atomically
    swaps the DB file. The FTS index (same DB) is restored with it.

    ``pre_write_guard`` (optional, L16) is a no-arg callable invoked immediately
    before any file is written or swapped. A caller that checked a precondition
    (e.g. "no active capture session") under a lock released earlier can pass a
    guard that re-validates that precondition right before the live DB is
    overwritten, closing the check-then-act gap. If it raises, no swap happens.
    """
    cfg = cfg or get_config()
    cfg.ensure_dirs()
    with session_scope() as s:
        rec = s.get(BackupRecord, backup_id)
        if rec is None:
            raise KeyError(backup_id)
    path = _safe_backup_path(cfg, rec.path)

    integrity = _integrity(path)
    if integrity != "ok" or not _schema_is_compatible(path):
        raise ValueError(f"Backup ist besch&auml;digt (integrity_check={integrity})")

    plan = {
        "backup_id": backup_id, "path": str(path), "kind": rec.kind,
        "integrity": integrity, "size": rec.size,
        "live_db": str(cfg.db_path),
        "live_size": cfg.db_path.stat().st_size if cfg.db_path.exists() else 0,
    }
    if not confirm:
        return {**plan, "applied": False,
                "note": "Dry-Run: nichts ge&auml;ndert. Mit confirm=true ausf&uuml;hren."}

    # L16: re-validate the caller precondition immediately before any file is
    # written or the live DB is swapped. The service-level check that opened
    # this path may have released its lock, so a session could have started
    # since; raising here (before step 1) means no files are touched at all.
    if pre_write_guard is not None:
        pre_write_guard()

    # 1) Copy the restore source to a private temp file FIRST, outside the
    #    pruned backups dir. The pre-restore safety snapshot (step 2) triggers
    #    retention pruning which could otherwise delete this very source file
    #    before the swap -- so the swap must not depend on the source surviving.
    src_tmp = cfg.db_path.parent / f".restore_src_{new_id()[:8]}.tmp"
    try:
        shutil.copyfile(str(path), src_tmp)
        # 2) Pre-restore safety snapshot (never lose current data).
        safety = create_backup(cfg, kind="db",
                               note=f"pre-restore safety for {backup_id[:8]}")
        # 3) Release pooled connections, then swap the temp copy in atomically.
        try:
            get_engine().dispose()
        except RuntimeError:
            pass
        os.replace(src_tmp, cfg.db_path)
        try:
            cfg.db_path.chmod(0o600)
        except OSError:
            pass
    finally:
        try:
            src_tmp.unlink(missing_ok=True)
        except OSError:
            pass
    log.warning("restore_applied id=%s from=%s safety=%s", backup_id[:8],
                path, safety["path"])
    return {**plan, "applied": True, "safety_backup": safety["path"],
            "note": "Restore abgeschlossen. Ein vorheriger Sicherheits-Snapshot wurde erstellt."}
