"""First-run setup wizard backend (offline, deterministic).

Covers: /setup/check aggregation, /setup/complete gating + persistence, the
existing-installation migration (setup_maybe_auto_complete / bootstrap), and
the background DownloadManager (ASR via mock engine, Ollama via a local
loopback pull server with real progress). No external network is used.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.bootstrap import bootstrap
from core.config import get_config
from core.logging_setup import setup_logging
from core.providers.base import ASREngine, ASRSegment
from core.security.secrets import require_network_action
from core.service import MeetingService


def _model_file(config, name: str = "small"):
    return config.models_dir / f"Systran--faster-whisper-{name}" / "model.bin"


def _wait_download(client, timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get("/setup/download/status").json()
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("Download lief nicht innerhalb der Frist zu Ende")


@pytest.fixture
def client():
    config = get_config()
    # Deterministic Ollama probe: port 1 never serves anything.
    config.ollama_base_url = "http://127.0.0.1:1/v1"
    config.network_allowed = False
    from core.store.db import get_engine, make_engine
    from core.store.models import Base
    make_engine(config)
    Base.metadata.create_all(get_engine())
    service = MeetingService(config)
    app = create_app(service)
    with TestClient(app) as c:
        yield c, service, config


class _ReadyAfterPrepareASR(ASREngine):
    """Offline stand-in: 'downloads' instantly and then reports ready."""

    model_name = "mock"

    def __init__(self, gated: bool = False, delay_s: float = 0.0):
        self._ready = False
        self._gated = gated
        self._delay = delay_s

    def is_model_ready(self) -> bool:
        return self._ready

    def transcribe(self, audio_path, language: str | None = None) -> list[ASRSegment]:
        return []

    def prepare_model(self, allow_download: bool = False) -> None:
        if not allow_download:
            raise RuntimeError("allow_download fehlt")
        if self._gated:
            require_network_action("download ASR model 'mock'", confirmed=True)
        if self._delay:
            time.sleep(self._delay)
        self._ready = True


def test_setup_check_shape(client):
    c, _, config = client
    r = c.get("/setup/check")
    assert r.status_code == 200
    body = r.json()
    assert body["setup_completed"] is False
    assert body["setup_version"] == 1
    assert body["ffmpeg"] is True
    assert body["network_allowed"] is False, "fresh install keeps the network gate closed"
    assert isinstance(body["disk_free_mb"], int)
    assert body["asr"]["model"] == "small"
    assert body["asr"]["installed"] is False
    assert body["asr"]["size_mb"] == 485
    assert body["ollama"]["reachable"] is False
    assert body["ollama"]["has_model"] is False
    assert body["ollama"]["selected_model"] == "qwen3.5:4b"
    # Hint is None only when the ollama binary already exists on this machine.
    hint = body["ollama"]["install_hint"]
    assert hint is None or isinstance(hint, str)
    assert body["download"]["state"] == "idle"


def test_setup_complete_requires_asr_model(client):
    c, _, _ = client
    r = c.post("/setup/complete")
    assert r.status_code == 409


def test_setup_complete_ok_and_persisted(client):
    c, _, config = client
    _model_file(config).parent.mkdir(parents=True, exist_ok=True)
    _model_file(config).write_bytes(b"0")
    r = c.post("/setup/complete")
    assert r.status_code == 200
    assert r.json() == {"setup_completed": True, "setup_version": 1}
    assert c.get("/setup/check").json()["setup_completed"] is True
    raw = (config.base_dir / "config.json").read_text(encoding="utf-8")
    assert json.loads(raw)["setup_completed"] is True


def test_setup_auto_complete_existing_installation(client):
    # Existing install: model already on disk -> silently marked as set up.
    c, service, config = client
    _model_file(config).parent.mkdir(parents=True, exist_ok=True)
    _model_file(config).write_bytes(b"0")
    assert service.setup_maybe_auto_complete() is True
    assert config.setup_completed is True
    assert json.loads((config.base_dir / "config.json").read_text())["setup_completed"] is True


def test_setup_auto_complete_fresh_install_unchanged(client):
    c, service, config = client
    assert service.setup_maybe_auto_complete() is False
    assert config.setup_completed is False


def test_bootstrap_migration_marks_existing_installs(client, tmp_path):
    # The real startup path: bootstrap() must set the flag silently.
    from core.config import Config, set_config
    _model_file(get_config()).parent.mkdir(parents=True, exist_ok=True)
    _model_file(get_config()).write_bytes(b"0")
    cfg = Config(base_dir=tmp_path)
    set_config(cfg)
    cfg.ollama_base_url = "http://127.0.0.1:1/v1"
    cfg.ensure_dirs()
    setup_logging("WARNING", cfg.log_path, to_stderr=False)
    _model_file(cfg).parent.mkdir(parents=True, exist_ok=True)
    _model_file(cfg).write_bytes(b"0")
    service = bootstrap(cfg, use_migrations=False, run_recovery=False)
    assert cfg.setup_completed is True
    assert service.setup_check()["setup_completed"] is True


def test_download_asr_via_engine_and_gate(client):
    c, service, config = client
    # 1) happy path through the injected engine (no network needed)
    service._asr_override = _ReadyAfterPrepareASR()
    r = c.post("/setup/download", json={"kind": "asr", "model": "mock",
                                        "confirm": True})
    assert r.status_code == 202
    final = _wait_download(c)
    assert final["state"] == "done"
    assert final["kind"] == "asr"
    assert final["model"] == "mock"
    # 2) the network gate is NOT bypassed: blocked engine -> error state
    service._asr_override = _ReadyAfterPrepareASR(gated=True)
    r = c.post("/setup/download", json={"kind": "asr", "model": "mock",
                                        "confirm": True})
    assert r.status_code == 202
    final = _wait_download(c)
    assert final["state"] == "error"
    assert "Netzwerk" in final["error"]


def test_download_only_one_at_a_time(client):
    c, service, _ = client
    service._asr_override = _ReadyAfterPrepareASR(delay_s=0.4)
    r = c.post("/setup/download", json={"kind": "asr", "model": "mock",
                                        "confirm": True})
    assert r.status_code == 202
    r2 = c.post("/setup/download", json={"kind": "ollama", "model": "x:1",
                                         "confirm": True})
    assert r2.status_code == 400
    final = _wait_download(c)
    assert final["state"] == "done"


class _PullHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        lines = (
            {"status": "pulling", "completed": 100, "total": 300},
            {"status": "pulling", "completed": 200, "total": 300},
            {"status": "success"},
        )
        body = b"".join(json.dumps(x).encode() + b"\n" for x in lines)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


def test_download_ollama_pull_reports_progress(client):
    c, _, config = client
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PullHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        config.ollama_base_url = f"http://127.0.0.1:{port}/v1"
        config.network_allowed = True  # gate semantics: pull needs opt-in
        r = c.post("/setup/download",
                   json={"kind": "ollama", "model": "test-model:1",
                         "confirm": True})
        assert r.status_code == 202
        assert r.json()["state"] == "running"
        final = _wait_download(c)
        assert final["state"] == "done"
        assert final["kind"] == "ollama"
        # The pull stream's completed/total values reached the status view.
        assert final["total_bytes"] == 300
        assert final["downloaded_bytes"] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_download_status_stays_idle_without_download(client):
    c, _, _ = client
    body = c.get("/setup/download/status").json()
    assert body["state"] == "idle"
    assert body["error"] is None
    # Unknown kinds are rejected by request validation before anything starts.
    r = c.post("/setup/download", json={"kind": "nope", "confirm": True})
    assert r.status_code == 422


def test_progress_tqdm_factory_binds_callback():
    """huggingface_hub instantiates tqdm_class(total=..., ...) per file; the
    factory must bind the callback so real HF downloads report progress."""
    from core.providers.faster_whisper import _progress_tqdm_factory

    seen: list[tuple[int, int]] = []
    factory = _progress_tqdm_factory(lambda n, t: seen.append((n, t)))
    assert callable(factory)
    # The network bar ("Downloading bytes") must stay silent...
    with factory(desc="Downloading bytes", total=100, unit="B") as transfer:
        transfer.update(50)
    assert seen == []
    # ...while the reconstruction bar reports monotonic bytes.
    with factory(desc="Reconstructing (incomplete total...)", total=100, unit="B") as bar:
        bar.update(40)
        bar.update(60)
        bar.set_description("model.bin")
        bar.set_postfix_str("1.0MiB/s", refresh=False)
        bar.refresh()
    assert seen == [(40, 100), (100, 100)]
