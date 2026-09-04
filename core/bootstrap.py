"""Process bootstrap: logging, storage, DB (migrations), recovery, service."""
from __future__ import annotations

import os

from core.config import Config
from core.logging_setup import get_logger, setup_logging
from core.recovery.recovery import recover
from core.service import MeetingService
from core.store.db import apply_migrations, has_table, make_engine
from core.store.models import Base
from core.store.db import get_engine
from core.store.db import session_scope

log = get_logger("ma.bootstrap")


def bootstrap(config: Config, use_migrations: bool = False,
              run_recovery: bool = True, to_stderr: bool = False) -> MeetingService:
    setup_logging(config.log_level, config.log_path, to_stderr=to_stderr)
    config.ensure_dirs()
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
    # (Re)build the FTS index so search is consistent after restart/crash.
    with session_scope() as s:
        from core.store.fts import ensure_fts, reindex_all
        ensure_fts(s)
        n = reindex_all(s)
        log.info("bootstrap_fts rows=%s", n)
    service = MeetingService(config)
    # P3: resume any post-stop pipeline left unfinished by a crash/restart.
    resumed = service.resume_pending_pipelines()
    if resumed:
        log.info("bootstrap_pipeline_resumed meetings=%s", resumed)
    return service
