"""Phase 2 REST endpoints: ASR model status/download, transcribe, search, export.

`client` injects the mock ASR engine (offline transcription works). `raw_client`
uses the real provider manager (model not downloaded) to test the network gate.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.audio.stream import SyntheticSource
from core.config import get_config
from core.service import MeetingService
from core.store.db import get_engine, make_engine
from core.store.models import Base
from tests.conftest import MockASREngine


def _build(asr_engine=None):
    config = get_config()
    make_engine(config)
    Base.metadata.create_all(get_engine())

    def factory(d, sr, ch):
        return SyntheticSource(1.5, sr, ch, block_seconds=0.05)

    service = MeetingService(config, source_factory=factory, asr_engine=asr_engine)
    service.record_consent(True)
    return service


@pytest.fixture
def client():
    service = _build(asr_engine=MockASREngine())
    app = create_app(service)
    with TestClient(app) as c:
        yield c, service


@pytest.fixture
def raw_client():
    service = _build()  # real provider manager, model not downloaded
    app = create_app(service)
    with TestClient(app) as c:
        yield c, service


def _finalize(c, svc, title="X") -> str:
    mid = c.post("/meetings", json={"title": title}).json()["id"]
    # fast/finite synthetic source: capture fully before stopping (deterministic)
    svc._sessions[mid].wait_done(timeout=10)
    c.post(f"/meetings/{mid}/stop")
    return mid


def test_asr_model_status(client):
    c, _ = client
    r = c.get("/models/asr")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "mock"
    assert body["ready"] is True


def test_transcribe_endpoint_then_detail(client):
    c, svc = client
    mid = _finalize(c, svc)
    r = c.post(f"/meetings/{mid}/transcribe", json={"language": "de"})
    assert r.status_code == 200
    assert r.json()["segments"] == 2

    detail = c.get(f"/meetings/{mid}").json()
    assert detail["status"] == "done"
    assert len(detail["segments"]) == 2


def test_transcribe_unknown_meeting_404(client):
    c, _ = client
    assert c.post("/meetings/nope/transcribe", json={}).status_code == 404


def test_search_endpoint(client):
    c, svc = client
    mid = _finalize(c, svc)
    c.post(f"/meetings/{mid}/transcribe", json={})
    r = c.get("/search", params={"q": "Budget"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    res = body["results"][0]
    assert res["segment_id"]
    assert res["meeting_id"] == mid
    assert "snippet" in res


def test_export_endpoint_markdown_and_json(client):
    c, svc = client
    mid = _finalize(c, svc)
    c.post(f"/meetings/{mid}/transcribe", json={})

    r = c.get(f"/meetings/{mid}/export", params={"format": "markdown"})
    assert r.status_code == 200
    assert "Quellenverweise" in r.json()["content"]

    r = c.get(f"/meetings/{mid}/export", params={"format": "json"})
    assert r.status_code == 200
    assert json.loads(r.json()["content"])["meeting"]["id"] == mid


def test_download_model_gated_network_off(raw_client):
    c, _ = raw_client
    # model not downloaded, network disabled by default
    assert c.get("/models/asr").json()["ready"] is False
    r = c.post("/models/asr/download", json={"confirm": True})
    assert r.status_code == 409  # NetworkBlockedError


def test_transcribe_gated_model_not_ready(raw_client):
    c, svc = raw_client
    mid = _finalize(c, svc)
    r = c.post(f"/meetings/{mid}/transcribe", json={})
    assert r.status_code == 409  # ModelNotReadyError
    assert "nicht heruntergeladen" in r.json()["detail"]
