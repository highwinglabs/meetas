"""Phase 5 REST endpoints: feature flags, live status sub-object, diarize.

All engines are deterministic mocks (offline). `client` has Phase 5 enabled and
mock live/diar engines injected so the opt-in paths are exercised end-to-end."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.audio.stream import SyntheticSource
from core.config import get_config
from core.providers.diar import DiarSpan, MockDiarizationEngine
from core.service import MeetingService
from core.store.db import get_engine, make_engine
from core.store.models import Base
from tests.conftest import MockASREngine, MockLiveASREngine


def _build(live: bool, diar: bool, asr_engine=None,
           live_engine=None, diar_engine=None):
    config = get_config()
    config.live_transcription = live
    config.speaker_diarization = diar
    config.live_period_s = 0.1
    config.live_tail_s = 2.0
    make_engine(config)
    Base.metadata.create_all(get_engine())

    def factory(d, sr, ch):
        return SyntheticSource(4.0, sr, ch, block_seconds=0.05)

    service = MeetingService(config, source_factory=factory, asr_engine=asr_engine,
                             live_engine=live_engine, diar_engine=diar_engine)
    service.record_consent(True)
    return service


def _diar():
    return MockDiarizationEngine([
        DiarSpan(0.0, 2.0, "Sprecher 1"),
        DiarSpan(2.0, 4.0, "Sprecher 2"),
    ])


@pytest.fixture
def client():
    service = _build(live=True, diar=True, asr_engine=MockASREngine(),
                     live_engine=MockLiveASREngine(), diar_engine=_diar())
    app = create_app(service)
    with TestClient(app) as c:
        yield c, service


@pytest.fixture
def plain_client():
    service = _build(live=False, diar=False, asr_engine=MockASREngine())
    app = create_app(service)
    with TestClient(app) as c:
        yield c, service


def _start(c, svc, title="X") -> str:
    return c.post("/meetings", json={"title": title}).json()["id"]


def _stop(c, svc, mid) -> None:
    svc._sessions[mid].wait_done(timeout=10)
    c.post(f"/meetings/{mid}/stop")


def test_feature_flags_endpoint(client):
    c, _ = client
    r = c.get("/config/features")
    assert r.status_code == 200
    body = r.json()
    assert body["live_transcription"] is True
    assert body["speaker_diarization"] is True
    assert body["live_ready"] is True
    assert body["diarization_engine"] == "mock-diar"


def test_feature_flags_plain_off(plain_client):
    c, _ = plain_client
    body = c.get("/config/features").json()
    assert body["live_transcription"] is False
    assert body["speaker_diarization"] is False
    assert body["live_ready"] is False


def test_live_status_exposes_partial(client):
    c, svc = client
    mid = _start(c, svc)
    # While recording, /status carries a live sub-object.
    r = c.get(f"/meetings/{mid}/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "recording"
    assert "live" in body
    assert body["live"]["enabled"] is True
    _stop(c, svc, mid)


def test_diarize_endpoint_after_transcribe(client):
    c, svc = client
    mid = _start(c, svc)
    _stop(c, svc, mid)
    assert c.post(f"/meetings/{mid}/transcribe", json={"language": "de"}).status_code == 200

    r = c.post(f"/meetings/{mid}/diarize")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["segments"] == 2
    assert body["speakers"] == 2

    # speakers are reflected in the meeting detail
    detail = c.get(f"/meetings/{mid}").json()
    speakers = {seg.get("speaker_id") for seg in detail["segments"]}
    assert "Sprecher 1" in speakers and "Sprecher 2" in speakers


def test_diarize_requires_transcript(plain_client):
    c, svc = plain_client
    mid = _start(c, svc)
    _stop(c, svc, mid)
    # no transcript yet (live off, no transcribe) -> 400
    assert c.post(f"/meetings/{mid}/diarize").status_code == 400


def test_diarize_unknown_meeting_404(client):
    c, _ = client
    assert c.post("/meetings/nope/diarize").status_code == 404
