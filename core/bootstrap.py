"""Process bootstrap: logging, storage, DB (migrations), recovery, service."""
from __future__ import annotations

import os
import time

from core.config import Config
from core.logging_setup import get_logger, setup_logging
from core.recovery.recovery import recover
from core.service import MeetingService
from core.store.db import apply_migrations, has_table, make_engine
from core.store.models import Base
from core.store.db import get_engine
from core.store.db import session_scope

log = get_logger("ma.bootstrap")

# L22: a preview-*.wav is at most ~10 min old in a live process (in-memory
# expiry). After a restart the in-memory map is gone, so any such file older
# than one hour is an orphan and can never be served again -> delete it.
_STALE_PREVIEW_S = 3600.0


def _cleanup_stale_previews(config: Config) -> int:
    """Remove orphaned A/B preview WAVs left behind by a previous process (L22).

    Previews live next to each meeting's original audio as ``preview-<token>.wav``
    and are meant to be short-lived. A process restart drops the in-memory
    expiry map, so previews older than :data:`_STALE_PREVIEW_S` are definitely
    stale. Only regular, non-symlink files under the audio store matching the
    preview pattern are removed. Returns the number of files deleted.
    """
    audio_root = config.audio_dir
    if not audio_root.exists():
        return 0
    root = audio_root.resolve()
    cutoff = time.time() - _STALE_PREVIEW_S
    removed = 0
    try:
        candidates = root.glob("**/preview-*.wav")
    except (OSError, RuntimeError):
        return 0
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        if root not in path.resolve().parents:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def bootstrap(config: Config, use_migrations: bool = False,
              run_recovery: bool = True, to_stderr: bool = False) -> MeetingService:
    setup_logging(config.log_level, config.log_path, to_stderr=to_stderr)
    config.ensure_dirs()
    # L22: drop preview files orphaned by a previous process before anything
    # else runs. This is pure file hygiene (no DB access needed yet).
    removed_previews = _cleanup_stale_previews(config)
    if removed_previews:
        log.info("bootstrap_cleanup stale_previews=%s", removed_previews)
    make_engine(config)
    if use_migrations:
        apply_migrations(config)
    else:
        if not has_table("meeting"):
            Base.metadata.create_all(get_engine())
            from core.store.db import _secure_db_file
            _secure_db_file(config)
    if run_recovery:
        with session_scope() as s:
            report = recover(config, s)
            log.info("bootstrap_recovery %s", report.__dict__)
    # (Re)build the FTS index so search is consistent after restart/crash.  A
    # crash leaves the index row count out of step with transcript_segment;
    # when the counts already match the index is current and the full rebuild
    # is skipped (L2: reindex_all used to run on every single startup).
    with session_scope() as s:
        from core.store.fts import ensure_fts, fts_needs_rebuild, reindex_all
        ensure_fts(s)
        if fts_needs_rebuild(s):
            n = reindex_all(s)
            log.info("bootstrap_fts rows=%s", n)
        else:
            log.info("bootstrap_fts rows=up-to-date (reindex skipped)")
    service = MeetingService(config)
    # P3: resume any post-stop pipeline left unfinished by a crash/restart.
    resumed = service.resume_pending_pipelines()
    if resumed:
        log.info("bootstrap_pipeline_resumed meetings=%s", resumed)
    return service
