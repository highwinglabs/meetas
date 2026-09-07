"""FastAPI app factory and server runner.

`run_server` is what makes the core an independent, controllable local
process: it acquires the single-instance lock, writes the PID, boots the
service (DB + recovery) and serves on loopback. A SIGTERM finalizes the active
recording and releases the lock.
"""
from __future__ import annotations

import os
import signal
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core import __version__, i18n
from core.api import rest
from core.bootstrap import bootstrap
from core.config import Config, get_config
from core.daemon import acquire_lock, release_lock, write_pid
from core.logging_setup import get_logger
from core.service import MeetingService

log = get_logger("ma.api")

# Default location of the built frontend: <project_root>/ui/dist. This module is
# core/api/app.py, so the project root is two levels up.
_DEFAULT_UI_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"


def _resolve_ui_dist(explicit: str | os.PathLike | None = None) -> Path | None:
    """Locate the built frontend dir. Returns None when no usable build exists,
    in which case the app behaves exactly as the pure REST API (Phase 1-3).

    An *explicit* path is authoritative: if it is given but unusable, we do NOT
    silently fall back to a different location (predictable for tests/embedders).
    Only when no explicit path is given do we consult $MA_UI_DIST, then the
    default <project_root>/ui/dist.
    """
    if explicit is not None:
        p = Path(explicit)
        return p if (p.is_dir() and (p / "index.html").is_file()) else None
    env = os.environ.get("MA_UI_DIST")
    if env:
        p = Path(env)
        if p.is_dir() and (p / "index.html").is_file():
            return p
    d = _DEFAULT_UI_DIST
    return d if (d.is_dir() and (d / "index.html").is_file()) else None


def _mount_ui(app: FastAPI, dist: Path) -> None:
    """Serve the built React SPA on loopback. Only `/`, `/index.html` and the
    `/assets/*` mount are added -- all REST routes are registered first and are
    therefore never shadowed. The UI is just a same-origin client of the API."""
    index = dist / "index.html"

    @app.get("/", include_in_schema=False)
    def _root():
        return FileResponse(index)

    @app.get("/index.html", include_in_schema=False)
    def _index():
        return FileResponse(index)

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="ui-assets")
    log.info("ui_serving dist=%s", dist)


class _LanguageMiddleware:
    """Pure ASGI middleware that resolves the request language from the
    ``Accept-Language`` header and stores it in a ContextVar for the duration of
    the request. Pure ASGI (not BaseHTTPMiddleware) so the ContextVar set here
    is visible to the endpoint handlers in the same task. German is the default
    when the header is absent, keeping existing clients unchanged."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        raw = headers.get(b"accept-language")
        accept = raw.decode("latin-1") if raw is not None else None
        token = i18n.current_language.set(i18n.parse_language(accept))
        try:
            await self.app(scope, receive, send)
        finally:
            i18n.current_language.reset(token)


def create_app(service: MeetingService,
               ui_dist: str | os.PathLike | None = None) -> FastAPI:
    rest.set_service(service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield  # startup: service already bootstrapped by caller
        service.close()  # shutdown: finalize active sessions

    app = FastAPI(title="meetas core", version=__version__, lifespan=lifespan)
    app.add_middleware(_LanguageMiddleware)
    app.include_router(rest.router)
    dist = _resolve_ui_dist(ui_dist)
    if dist is not None:
        _mount_ui(app, dist)
    return app


def _finalize_on_signal(service: MeetingService, config: Config) -> None:
    def _handler(signum, frame):
        log.info("signal_received signum=%s -> finalizing", signum)
        try:
            service.close()
        finally:
            release_lock(config)
            raise SystemExit(0)
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def run_server(config: Config | None = None, use_migrations: bool = False) -> None:
    import uvicorn

    cfg = config or get_config()
    # The API has no authentication layer and therefore must never be exposed
    # beyond the local machine. Ignore a dangerous environment/config override
    # instead of silently binding to 0.0.0.0 or a LAN address.
    if cfg.host not in {"127.0.0.1", "localhost"}:
        log.warning("unsafe_bind_host=%s; forcing loopback", cfg.host)
        cfg.host = "127.0.0.1"
    acquire_lock(cfg)
    write_pid(cfg, os.getpid())
    service = bootstrap(cfg, use_migrations=use_migrations, to_stderr=True)
    _finalize_on_signal(service, cfg)
    app = create_app(service)
    try:
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")
    finally:
        release_lock(cfg)
