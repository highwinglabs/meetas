"""Independent-process lifecycle for the core.

The core is designed to be an OS process whose lifetime is NOT tied to the
Tauri UI. It can be:

- started in the foreground:  `meeting-core serve`
- started as a detached daemon: `meeting-core daemon` (double-fork + setsid)
  so a UI crash (and killing of the UI process group) does not take it down.
- controlled: `status` / `stop` / `restart`

A PID + lock file in the state dir prevents double-starts and enables control.
Stale locks (dead PID) are cleared automatically.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from core.config import Config
from core.logging_setup import get_logger

log = get_logger("ma.daemon")


class AlreadyRunningError(RuntimeError):
    def __init__(self, pid: int):
        self.pid = pid
        super().__init__(f"Core läuft bereits (PID {pid}).")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(config: Config) -> int | None:
    p = config.pid_file
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def write_pid(config: Config, pid: int) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    tmp = config.pid_file.with_name(config.pid_file.name + ".tmp")
    with open(tmp, "w", encoding="ascii") as handle:
        handle.write(str(int(pid)))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, config.pid_file)
    try:
        config.pid_file.chmod(0o600)
    except OSError:
        pass


def acquire_lock(config: Config) -> None:
    """Take the lock; clear it if a previous holder is dead (stale)."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lock = config.lock_file
    for _ in range(3):
        if lock.exists():
            try:
                holder = int(lock.read_text().strip() or "0")
            except (ValueError, OSError):
                holder = 0
            if holder and pid_alive(holder) and holder != os.getpid():
                raise AlreadyRunningError(holder)
            # stale or self -> remove
            try:
                lock.unlink()
            except OSError:
                pass
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, str(os.getpid()).encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            return
        except FileExistsError:
            continue
    raise AlreadyRunningError(read_pid(config) or 0)


def release_lock(config: Config) -> None:
    for f in (config.lock_file, config.pid_file):
        try:
            if f.exists():
                f.unlink()
        except OSError:
            pass


# The first parent waits this long for the grandchild to reach a live state
# (i.e. to write its PID file) before reporting success.  Long enough for the
# grandchild to import + acquire the lock + write the PID, short enough that a
# ``daemon`` call does not block for seconds on the happy path.
DAEMON_STARTUP_GRACE_S = 3.0


def _parent_verify_daemon(config: Config | None, grace_s: float) -> bool:
    """Parent-side liveness check after the double-fork.

    Returns True once the daemon has written a PID file whose process is alive
    (it does that at the start of ``run_server``, before the slow bootstrap),
    or False after ``grace_s`` if it never appears / is already dead.  A ``None``
    config (e.g. direct test calls) short-circuits to True to preserve the
    legacy "fire and forget" behavior.
    """
    if config is None:
        return True
    deadline = time.time() + max(0.0, grace_s)
    while time.time() < deadline:
        pid = read_pid(config)
        if pid and pid_alive(pid):
            return True
        time.sleep(0.1)
    pid = read_pid(config)
    return bool(pid and pid_alive(pid))


def _parent_report_failure(log_path: Path | str | None) -> None:
    """Tell the invoking user the daemon died at startup and where to look."""
    where = str(log_path) if log_path else "the daemon log"
    try:
        sys.stderr.write(
            "Daemon did not start (no live process within the grace period).\n"
            f"Check the daemon log: {where}\n")
        sys.stderr.flush()
    except Exception:
        pass


def daemonize(stderr_to: Path | None = None, *, config: Config | None = None,
              log_path: Path | str | None = None,
              grace_s: float = DAEMON_STARTUP_GRACE_S) -> None:
    """Double-fork + setsid: detach from the controlling terminal and the
    parent's process group so the process survives the UI dying.

    ``stderr_to`` (optional) receives the daemon's stderr instead of
    /dev/null so that crashes after the double-fork stay diagnosable.

    ``config`` / ``log_path`` (optional) enable the first parent to verify the
    grandchild actually came up: if the daemon does not write a live PID file
    within ``grace_s`` the parent prints the log path and exits non-zero, so a
    startup crash is not mistaken for a clean start (L7).
    """
    if os.fork() > 0:
        ok = _parent_verify_daemon(config, grace_s)
        if not ok:
            _parent_report_failure(log_path)
        os._exit(0 if ok else 1)  # first parent exits (0 = daemon is up)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)  # first child exits; grandchild is the daemon
    # redirect stdio to dev/null
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    if stderr_to is not None:
        try:
            stderr_to.parent.mkdir(parents=True, exist_ok=True)
            err = os.open(
                stderr_to,
                os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC, 0o600)
            os.dup2(err, 2)
            os.close(err)
        except OSError:
            os.dup2(devnull, 2)
    else:
        os.dup2(devnull, 2)
    os.close(devnull)
    os.umask(0o077)


def wait_healthy(config: Config, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    import urllib.request
    while time.time() < deadline:
        if not pid_alive(read_pid(config) or 0) and not config.pid_file.exists():
            time.sleep(0.1)
            continue
        try:
            host = config.host if config.host in {"127.0.0.1", "localhost"} else "127.0.0.1"
            with urllib.request.urlopen(
                f"http://{host}:{config.port}/health", timeout=1
            ) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def graceful_stop(config: Config, timeout: float = 20.0) -> bool:
    pid = read_pid(config)
    if not pid or not pid_alive(pid):
        release_lock(config)
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        release_lock(config)
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            break
        time.sleep(0.2)
    if pid_alive(pid):
        log.warning("graceful_stop_timeout pid=%s", pid)
        return False
    release_lock(config)
    return True
