"""Single-instance lock, PID file and stale-lock handling."""
from __future__ import annotations

import os
import subprocess

import pytest

from core.daemon import (
    AlreadyRunningError, acquire_lock, read_pid, release_lock, write_pid,
)


def test_acquire_holds_our_pid(config):
    acquire_lock(config)
    assert config.lock_file.exists()
    assert int(config.lock_file.read_text().strip()) == os.getpid()
    release_lock(config)
    assert not config.lock_file.exists()


def test_conflicting_live_holder_raises(config):
    # pid 1 (systemd/init) is alive and is not us
    config.lock_file.parent.mkdir(parents=True, exist_ok=True)
    config.lock_file.write_text("1")
    try:
        with pytest.raises(AlreadyRunningError):
            acquire_lock(config)
    finally:
        config.lock_file.unlink(missing_ok=True)


def test_stale_lock_is_cleared_and_reacquired(config):
    p = subprocess.Popen(["true"])
    p.wait()  # pid is now dead
    config.lock_file.parent.mkdir(parents=True, exist_ok=True)
    config.lock_file.write_text(str(p.pid))
    acquire_lock(config)  # must clear the stale lock
    assert int(config.lock_file.read_text().strip()) == os.getpid()
    release_lock(config)


def test_pid_write_read_roundtrip(config):
    write_pid(config, 4242)
    assert read_pid(config) == 4242
    release_lock(config)
    assert read_pid(config) is None


def test_restart_subcommand_has_no_migrations_flag():
    # Regression: `restart` parses an args namespace without
    # `no_migrations`; cmd_daemon used to crash *after* the double-fork
    # (AttributeError, stderr on /dev/null), so the new daemon never started.
    from core.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["restart"])
    assert getattr(args, "no_migrations", False) is False
    args = parser.parse_args(["restart", "--no-migrations"])
    assert args.no_migrations is True
    # daemon keeps its own flag as well
    assert parser.parse_args(["daemon", "--no-migrations"]).no_migrations is True


def test_daemon_and_restart_share_no_migrations_flag():
    # Regression: `restart` used to parse an args namespace without
    # `no_migrations`; cmd_daemon then crashed *after* the double-fork
    # (silent, stderr on /dev/null) and the new daemon never started.
    from core.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["daemon", "--no-migrations"]).no_migrations is True
    assert parser.parse_args(["restart", "--no-migrations"]).no_migrations is True
    assert getattr(parser.parse_args(["restart"]), "no_migrations", False) is False
