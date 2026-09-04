"""Phase 3 REST endpoints: /models/llm and /meetings/{id}/analyze.

All engines are mocks or the OpenAI-compatible client backed by
``httpx.MockTransport`` -- NO real LLM request and NO real network call.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.audio.stream import SyntheticSource
from core.config import get_config
from core.llm import MockLLM, OpenAICompatibleLLM
from core.service import MeetingService
from core.store.db import get_engine, make_engine
from core.store.models import Base
from tests.conftest import MockASREngine


def _build(llm_engine=None):
    # Safe by default: never construct the real client in tests.
    llm_engine = llm_engine if llm_engine is not None else MockLLM()
    config = get_config()
    make_engine(config)
    Base.metadata.create_all(get_engine())

    def factory(d, sr, ch):
        return SyntheticSource(1.5, sr, ch, block_seconds=0.05)

    service = MeetingService(config, source_factory=factory,
                             asr_engine=MockASREngine(), llm_engine=llm_engine)
    service.record_consent(True)
    return service


@pytest.fixture
def client():
    service = _build(llm_engine=MockLLM())
    app = create_app(service)
    with TestClient(app) as c:
        yield c, service


def _finalize(c, svc, title="X") -> str:
    mid = c.post("/meetings", json={"title": title}).json()["id"]
    svc._sessions[mid].wait_done(timeout=10)
    c.post(f"/meetings/{mid}/stop")
    return mid


def _transcribe(c, mid) -> dict:
    r = c.post(f"/meetings/{mid}/transcribe", json={})
    assert r.status_code == 200
    return r.json()


def _real_client(handler):
    config = get_config()
    config.llm_max_busy_retries = 2
    config.llm_busy_wait_s = 0.01
    config.llm_timeout_s = 5.0
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return OpenAICompatibleLLM(
        base_url="http://127.0.0.1:8081/v1", model="qwen3.8-27b-q4kxl",
        config=config, http_client=http,
    )


def test_llm_model_status(client):
    c, _ = client
    r = c.get("/models/llm")
    assert r.status_code == 200
    b = r.json()
    assert b["mock"] is True
    assert b["local"] is True
    assert b["network_used"] is False
    assert b["model"] == "qwen3.8-27b-q4kxl"
    assert b["base_url"] == "http://127.0.0.1:8081/v1"


def test_analyze_endpoint_then_detail(client):
    import json

    c, svc = client
    mid = _finalize(c, svc)
    _transcribe(c, mid)
    r = c.post(f"/meetings/{mid}/analyze", json={"kind": "summary"})
    assert r.status_code == 200
    assert r.json()["model"] == "mock-llm"
    # the analysis payload is the validated JSON + a rendered markdown
    content = r.json().get("content")
    if content is not None:
        data = json.loads(content)
        assert "kurzfassung" in data
    detail = c.get(f"/meetings/{mid}").json()
    assert detail["analyses"][0]["model"] == "mock-llm"
    a0 = detail["analyses"][0]
    assert json.loads(a0["content"])  # stored content is valid JSON
    assert "Kurzfassung" in a0["markdown"]  # rendered markdown is present


def test_analyze_unknown_meeting_404(client):
    c, _ = client
    assert c.post("/meetings/nope/analyze", json={}).status_code == 404


def test_analyze_no_transcript_400(client):
    c, svc = client
    mid = _finalize(c, svc)  # captured but not transcribed
    r = c.post(f"/meetings/{mid}/analyze", json={})
    assert r.status_code == 400
    assert "Transkript" in r.json()["detail"]


def test_analyze_busy_mock_503():
    service = _build(llm_engine=MockLLM(busy=True))
    app = create_app(service)
    with TestClient(app) as c:
        mid = _finalize(c, service)
        _transcribe(c, mid)
        r = c.post(f"/meetings/{mid}/analyze", json={})
        assert r.status_code == 503
        assert "belegt" in r.json()["detail"]


def test_analyze_real_client_busy_503():
    engine = _real_client(lambda r: httpx.Response(503, text="slot is busy"))
    service = _build(llm_engine=engine)
    app = create_app(service)
    with TestClient(app) as c:
        mid = _finalize(c, service)
        _transcribe(c, mid)
        r = c.post(f"/meetings/{mid}/analyze", json={})
        assert r.status_code == 503
        assert "belegt" in r.json()["detail"]


def test_analyze_real_client_down_503():
    def handler(r):
        raise httpx.ConnectError("connection refused")

    service = _build(llm_engine=_real_client(handler))
    app = create_app(service)
    with TestClient(app) as c:
        mid = _finalize(c, service)
        _transcribe(c, mid)
        r = c.post(f"/meetings/{mid}/analyze", json={})
        assert r.status_code == 503
        assert "nicht erreichbar" in r.json()["detail"]
