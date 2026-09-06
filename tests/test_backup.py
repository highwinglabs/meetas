"""Phase 7c: local backup / restore + retention. All offline, file-based,
safety-first (a restore always takes a pre-restore safety snapshot)."""
from __future__ import annotations

import os
from pathlib import Path

from core.store.db import session_scope
from core.store.models import Meeting


def test_create_db_backup(make_service, config):
    svc = make_service()
    mid = svc.start_meeting(title="M1", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    out = svc.create_backup(kind="db")
    assert out["kind"] == "db"
    assert Path(out["path"]).exists()
    assert out["integrity"] == "ok"
    assert out["size"] > 0
    # recorded + file secured 0600
    mode = os.stat(out["path"]).st_mode & 0o777
    assert mode == 0o600
    backups = svc.list_backups()
    assert any(b["id"] == out["id"] and b["exists"] for b in backups)


def test_create_full_backup_archives_data(make_service, config):
    svc = make_service()
    # drop a file into the audio dir so "full" has something to archive
    cfg_audio = config.audio_dir
    cfg_audio.mkdir(parents=True, exist_ok=True)
    (cfg_audio / "clip.wav").write_bytes(b"0" * 64)
    out = svc.create_backup(kind="full")
    assert out["kind"] == "full"
    assert out["data_zip"] and Path(out["data_zip"]).exists()
    import zipfile
    with zipfile.ZipFile(out["data_zip"]) as zf:
        assert any("clip.wav" in n for n in zf.namelist())


def test_restore_dry_run_changes_nothing(make_service, config):
    svc = make_service()
    m1 = svc.start_meeting(title="A", source="mic")
    svc._sessions[m1].wait_done(timeout=10)
    svc.stop(m1)
    b = svc.create_backup(kind="db")
    # add a second meeting after the backup
    m2 = svc.start_meeting(title="B", source="mic")
    svc._sessions[m2].wait_done(timeout=10)
    svc.stop(m2)
    with session_scope() as s:
        assert s.query(Meeting).count() == 2
    dry = svc.restore_backup(b["id"], confirm=False)
    assert dry["applied"] is False
    with session_scope() as s:
        assert s.query(Meeting).count() == 2  # unchanged


def test_restore_confirm_swaps_db_and_makes_safety(make_service, config):
    svc = make_service()
    m1 = svc.start_meeting(title="A", source="mic")
    svc._sessions[m1].wait_done(timeout=10)
    svc.stop(m1)
    b = svc.create_backup(kind="db")
    m2 = svc.start_meeting(title="B", source="mic")
    svc._sessions[m2].wait_done(timeout=10)
    svc.stop(m2)
    with session_scope() as s:
        assert s.query(Meeting).count() == 2
    out = svc.restore_backup(b["id"], confirm=True)
    assert out["applied"] is True
    assert out["safety_backup"] and Path(out["safety_backup"]).exists()
    # live DB rolled back to the snapshot (only meeting A)
    with session_scope() as s:
        assert s.query(Meeting).count() == 1


def test_restore_reruns_session_guard_before_swap(make_service, config):
    # L16: the "no active capture session" precondition is re-validated
    # immediately before the live DB is swapped, not only at the start of the
    # restore (the old check-then-act released its lock before the swap).
    from core import backup as backup_mod
    from core.services._common import ActiveMeetingError
    svc = make_service()
    m1 = svc.start_meeting(title="A", source="mic")
    svc._sessions[m1].wait_done(timeout=10)
    svc.stop(m1)
    b = svc.create_backup(kind="db")
    m2 = svc.start_meeting(title="B", source="mic")
    svc._sessions[m2].wait_done(timeout=10)
    svc.stop(m2)
    with session_scope() as s:
        assert s.query(Meeting).count() == 2

    # A guard that fires (a session started after the initial check) must abort
    # the restore before any file is swapped or a temp file is created.
    def _fire() -> None:
        raise ActiveMeetingError("session started during restore")
    try:
        backup_mod.restore_backup(config, b["id"], confirm=True, pre_write_guard=_fire)
        assert False, "expected ActiveMeetingError"
    except ActiveMeetingError:
        pass
    with session_scope() as s:
        assert s.query(Meeting).count() == 2  # no swap happened
    assert list(config.db_path.parent.glob(".restore_src_*.tmp")) == []

    # A passing guard is invoked exactly once and lets the restore proceed.
    calls = {"n": 0}
    def _pass() -> None:
        calls["n"] += 1
    out = backup_mod.restore_backup(config, b["id"], confirm=True, pre_write_guard=_pass)
    assert out["applied"] is True and calls["n"] == 1
    with session_scope() as s:
        assert s.query(Meeting).count() == 1  # rolled back to A's snapshot


def test_restore_missing_backup_raises(make_service):
    svc = make_service()
    try:
        svc.restore_backup("does-not-exist", confirm=False)
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_restore_corrupt_backup_rejected(make_service, config):
    svc = make_service()
    b = svc.create_backup(kind="db")
    # corrupt the backup file
    Path(b["path"]).write_bytes(b"not a real sqlite db")
    try:
        svc.restore_backup(b["id"], confirm=True)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_retention_prunes_oldest(make_service, config):
    svc = make_service()
    config.backup_retention = 2
    paths = [svc.create_backup(kind="db")["path"] for _ in range(4)]
    remaining = [p for p in paths if Path(p).exists()]
    assert len(remaining) == 2
    # the newest survive, oldest pruned
    assert paths[0] not in remaining


def test_restore_oldest_survives_prune(make_service, config):
    # Regression (#1): the pre-restore safety snapshot used to trigger retention
    # pruning that could delete the very backup being restored, losing the only
    # copy (FileNotFoundError mid-restore). The swap must succeed from a temp
    # copy even when the source file is pruned away.
    import os
    import time
    # Isolate: no auto-backup at service start, so the only backups are the ones
    # created below and retention counts are deterministic.
    config.backup_enabled = False
    config.backup_retention = 2
    svc = make_service()
    m1 = svc.start_meeting(title="A", source="mic")
    svc._sessions[m1].wait_done(timeout=10)
    svc.stop(m1)
    a = svc.create_backup(kind="db")  # the backup we will restore from
    # force it to be unambiguously the oldest (mtime), so pruning targets it
    os.utime(a["path"], (time.time() - 3600, time.time() - 3600))
    m2 = svc.start_meeting(title="B", source="mic")
    svc._sessions[m2].wait_done(timeout=10)
    svc.stop(m2)
    svc.create_backup(kind="db")  # pushes the count to the retention cap
    out = svc.restore_backup(a["id"], confirm=True)
    assert out["applied"] is True
    # live DB rolled back to A's snapshot (only meeting A)
    with session_scope() as s:
        assert s.query(Meeting).count() == 1
