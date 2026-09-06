"""REST API end-to-end via FastAPI TestClient (synthetic, long-lived source)."""
from __future__ import annotations

import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.audio.stream import AudioSource
from core.config import get_config
from core.service import MeetingService
from core.store.db import get_engine, make_engine
from core.store.models import Base


class SlowSource(AudioSource):
    """Yields near-silence blocks and paces itself so a meeting stays active
    for the duration of the test, yet terminates quickly on stop()."""

    def __init__(self, sr: int = 16000, ch: int = 1):
        self.sample_rate = sr
        self.channels = ch

    def frames(self):
        n = int(0.05 * self.sample_rate)
        for _ in range(200000):  # far more than a test will ever consume
            yield np.zeros(n, dtype=np.float32).reshape(-1, 1)
            time.sleep(0.01)

    def close(self):
        return None


@pytest.fixture
def client():
    config = get_config()
    make_engine(config)
    Base.metadata.create_all(get_engine())
    service = MeetingService(config, source_factory=lambda d, sr, ch: SlowSource(sr, ch))
    app = create_app(service)
    with TestClient(app) as c:
        yield c, service


def test_health(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["ffmpeg"] is True
    assert body["network_allowed"] is False


def test_devices(client):
    c, _ = client
    r = c.get("/devices")
    assert r.status_code == 200
    assert "devices" in r.json()


def test_consent_flow_blocks_start_until_ack(client):
    c, _ = client
    # no consent yet
    assert c.get("/consent").json()["acknowledged"] is False
    r = c.post("/meetings", json={"title": "X"})
    assert r.status_code == 409
    # acknowledge consent
    assert c.post("/consent/ack", json={"acknowledged": True}).status_code == 200
    assert c.get("/consent").json()["acknowledged"] is True
    # now start works
    r = c.post("/meetings", json={"title": "X"})
    assert r.status_code == 201
    mid = r.json()["id"]
    c.post(f"/meetings/{mid}/stop")


def test_single_active_meeting_enforced(client):
    c, _ = client
    c.post("/consent/ack", json={"acknowledged": True})
    mid1 = c.post("/meetings", json={"title": "A"}).json()["id"]
    r2 = c.post("/meetings", json={"title": "B"})
    assert r2.status_code == 409
    c.post(f"/meetings/{mid1}/stop")
    # after stop a new meeting may start
    r3 = c.post("/meetings", json={"title": "C"})
    assert r3.status_code == 201
    c.post(f"/meetings/{r3.json()['id']}/stop")


def test_analysis_cancel_endpoint(client):
    c, _ = client
    c.post("/consent/ack", json={"acknowledged": True})
    mid = c.post("/meetings", json={"title": "C"}).json()["id"]
    c.post(f"/meetings/{mid}/stop")
    # Stopping a (never started) analysis is a no-op that reports cancelled.
    r = c.post(f"/meetings/{mid}/analysis/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    # Unknown meeting -> 404.
    assert c.post("/meetings/does-not-exist/analysis/cancel").status_code == 404


def test_meeting_lifecycle_pause_resume_stop(client):
    c, _ = client
    c.post("/consent/ack", json={"acknowledged": True})
    mid = c.post("/meetings", json={"title": "Life"}).json()["id"]

    assert c.get(f"/meetings/{mid}/status").json()["status"] == "recording"

    assert c.post(f"/meetings/{mid}/pause").status_code == 200
    assert c.get(f"/meetings/{mid}/status").json()["status"] == "paused"

    assert c.post(f"/meetings/{mid}/resume").status_code == 200
    assert c.get(f"/meetings/{mid}/status").json()["status"] == "recording"

    r = c.post(f"/meetings/{mid}/stop")
    assert r.status_code == 200
    st = r.json()
    assert st["status"] == "ready"
    assert st["original_path"] is not None

    # reflected in list + detail
    listing = c.get("/meetings").json()["meetings"]
    assert any(m["id"] == mid and m["status"] == "ready" for m in listing)
    detail = c.get(f"/meetings/{mid}").json()
    assert detail["id"] == mid
    assert detail["status"] == "ready"
    assert any(j["stage"] == "transcribe" for j in detail["jobs"])


def test_audio_preview_roundtrip(finalize_meeting):
    # Regression: POST /meetings/{id}/audio/preview answered 500 (NameError:
    # uuid used without import in create_audio_preview) for finished meetings
    # with a mic or both source.
    svc, mid = finalize_meeting(duration_s=2.0)
    app = create_app(svc)
    with TestClient(app) as c:
        r = c.post(f"/meetings/{mid}/audio/preview",
                   json={"start_s": 0, "duration_s": 2, "profile": {},
                         "noise_profile_mode": "disabled"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["duration_s"] == 2.0
        g = c.get(f"/meetings/{mid}/audio/preview/{body['token']}")
        assert g.status_code == 200
        assert g.headers["content-type"].startswith("audio/wav")
        assert g.content[:4] == b"RIFF"


def test_workspace_trash_tasks_and_device_preference(client):
    c, _ = client
    c.post("/consent/ack", json={"acknowledged": True})
    project = c.post("/projects", json={"name": "API-Projekt"}).json()
    mid = c.post("/meetings", json={"title": "API-Meeting", "project_id": project["id"]}).json()["id"]
    assert c.post(f"/meetings/{mid}/stop").status_code == 200

    task = c.post("/tasks", json={"text": "Manuell nachfassen", "project_id": project["id"]})
    assert task.status_code == 201
    assert task.json()["source"] == "manuell"
    assert task.json()["project_id"] == project["id"]

    assert c.get("/devices/preference").status_code == 200
    assert c.put("/devices/preference", json={"name": None, "index": None}).status_code == 200

    assert c.delete(f"/meetings/{mid}").status_code == 200
    assert mid not in {m["id"] for m in c.get("/meetings").json()["meetings"]}
    trash = c.get("/trash").json()
    assert any(m["id"] == mid for m in trash["meetings"])
    assert c.post(f"/meetings/{mid}/restore").status_code == 200
    assert c.delete(f"/meetings/{mid}").status_code == 200
    assert c.delete(f"/meetings/{mid}/permanent").status_code == 200
    assert not any(m["id"] == mid for m in c.get("/trash").json()["meetings"])


def test_meeting_metadata_can_be_edited_and_moved(client):
    c, _ = client
    c.post("/consent/ack", json={"acknowledged": True})
    project = c.post("/projects", json={"name": "Prüfung"}).json()
    mid = c.post("/meetings", json={"title": "Alt"}).json()["id"]
    c.post(f"/meetings/{mid}/stop")

    response = c.patch(f"/meetings/{mid}", json={
        "title": "Interview IT", "project_id": project["id"]})
    assert response.status_code == 200
    assert response.json()["title"] == "Interview IT"
    assert response.json()["project_id"] == project["id"]
    assert c.get(f"/meetings/{mid}").json()["project_id"] == project["id"]

    # Explicitly clearing the project is supported without deleting the meeting.
    response = c.patch(f"/meetings/{mid}", json={"project_id": None})
    assert response.status_code == 200
    assert response.json()["project_id"] is None


def test_live_window_settings_are_validated_before_update(client):
    c, _ = client
    original = c.get("/config").json()
    response = c.put("/config", json={"values": {
        "live_window_s": 8, "live_tail_s": 8,
    }})
    assert response.status_code == 400
    current = c.get("/config").json()
    assert current["live_window_s"] == original["live_window_s"]
    assert current["live_tail_s"] == original["live_tail_s"]

    response = c.put("/config", json={"values": {
        "live_window_s": 8, "live_period_s": 2, "live_tail_s": 3,
    }})
    assert response.status_code == 200
    assert response.json()["live_window_s"] == 8.0
    assert response.json()["live_period_s"] == 2.0
    assert response.json()["live_tail_s"] == 3.0


def test_live_period_bounds_are_shared_between_config_and_settings(client, monkeypatch):
    # Regression (L14): the settings API clamp and the config/env validation
    # must agree on the live_period_s bounds, so a value that is legal in one
    # path is clamped identically in the other (no silent drift).
    from core.config import Config, LIVE_PERIOD_S_MIN, LIVE_PERIOD_S_MAX

    c, _ = client

    # Settings API path: below the shared floor -> clamped to the floor.
    response = c.put("/config", json={"values": {"live_period_s": 0.5}})
    assert response.status_code == 200
    assert response.json()["live_period_s"] == LIVE_PERIOD_S_MIN

    # Config/env path: the same value loaded from the environment is clamped
    # to the same floor by Config.load()'s normalisation.
    monkeypatch.setenv("MA_LIVE_PERIOD_S", "0.5")
    assert Config.load().live_period_s == LIVE_PERIOD_S_MIN

    # The shared ceiling is applied by both paths as well.
    response = c.put("/config", json={"values": {"live_period_s": 99.0}})
    assert response.json()["live_period_s"] == LIVE_PERIOD_S_MAX
    monkeypatch.setenv("MA_LIVE_PERIOD_S", "99")
    assert Config.load().live_period_s == LIVE_PERIOD_S_MAX


def test_unknown_meeting_404(client):
    c, _ = client
    assert c.get("/meetings/does-not-exist").status_code == 404
    assert c.post("/meetings/does-not-exist/pause").status_code == 404


def test_enable_network_requires_confirmation(client):
    # Regression (#6): enabling external providers is a security-posture change
    # and must require an explicit confirmation flag.
    c, svc = client
    r = c.put("/config", json={"values": {"network_allowed": True}})
    assert r.status_code == 409
    assert svc.config.network_allowed is False
    r = c.put("/config", json={"values": {"network_allowed": True},
                               "confirm_network_allowed": True})
    assert r.status_code == 200
    assert svc.config.network_allowed is True
    # Disabling again needs no confirmation.
    r = c.put("/config", json={"values": {"network_allowed": False}})
    assert r.status_code == 200
    assert svc.config.network_allowed is False


def test_upload_body_size_capped(client, config):
    # Regression (#7): an oversized upload body is rejected (413) before it is
    # read into memory / processed.
    c, svc = client
    config.max_upload_bytes = 16
    r = c.post("/uploads?filename=big.bin", content=b"A" * 32,
               headers={"content-type": "application/octet-stream"})
    assert r.status_code == 413
    # A body within the cap is not rejected for size.
    r = c.post("/uploads?filename=small.bin", content=b"A" * 4,
               headers={"content-type": "application/octet-stream"})
    assert r.status_code != 413


def test_resumable_upload_total_size_capped(client, config):
    # Regression (#7): the declared total size of a resumable upload is capped.
    c, svc = client
    config.max_upload_bytes = 100
    r = c.post("/uploads/start", json={"filename": "huge.bin", "total_size": 10000})
    assert r.status_code == 400
    assert "zu groß" in r.json().get("detail", "")


def test_resumable_upload_chunk_size_capped(client, config):
    # Regression: a single resumable-upload chunk request is capped per-request
    # and rejected with 413 *before* its body is buffered unbounded in memory
    # (previously `await request.body()` read the whole request first).
    c, svc = client
    config.max_upload_chunk_bytes = 16
    up = c.post("/uploads/start",
                json={"filename": "chunky.bin", "total_size": 100}).json()
    assert up["status"] == "uploading"
    upload_id = up["id"]
    # A 32-byte chunk exceeds the 16-byte per-chunk cap -> 413, nothing accepted.
    r = c.patch(f"/uploads/{upload_id}/chunk", content=b"A" * 32,
                headers={"x-upload-offset": "0"})
    assert r.status_code == 413
    assert c.get(f"/uploads/{upload_id}").json()["received_size"] == 0
    # A chunk within the cap is accepted (200) and advances the offset.
    r = c.patch(f"/uploads/{upload_id}/chunk", content=b"A" * 8,
                headers={"x-upload-offset": "0"})
    assert r.status_code == 200
    assert r.json()["received_size"] == 8


def test_release_storage_skips_remote_endpoint(client, config):
    # Regression (#5): release_storage must not issue requests to a
    # non-loopback, config-controlled endpoint while external network is off.
    c, svc = client
    config.ollama_base_url = "http://198.51.100.7/v1"  # TEST-NET, never dialed
    config.network_allowed = False
    out = svc.release_storage()
    assert out["ollama"]["requested"] is False
    assert "lokal" in out["ollama"]["note"]


def test_consent_text_german_default_and_english(client):
    # German is the default (no Accept-Language header); English on request.
    c, _ = client
    de = c.get("/consent").json()["text"]
    assert "Tonaufnahmen" in de  # German default preserved
    en = c.get("/consent", headers={"Accept-Language": "en"}).json()["text"]
    assert "records audio" in en
    assert "Tonaufnahmen" not in en


def test_error_detail_localised_by_accept_language(client):
    # The same failure is German by default and English for Accept-Language: en.
    c, _ = client
    r = c.get("/meetings/does-not-exist")
    assert r.status_code == 404
    assert r.json()["detail"] == "Meeting nicht gefunden: does-not-exist"
    r = c.get("/meetings/does-not-exist", headers={"Accept-Language": "en"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Meeting not found: does-not-exist"


def test_validation_error_localised_by_accept_language(client):
    # A validation error (consent not yet given) is localised per request.
    c, _ = client
    r = c.post("/meetings", json={"title": "X"})
    assert r.status_code == 409
    assert "Einwilligungs" in r.json()["detail"]
    r = c.post("/meetings", json={"title": "X"}, headers={"Accept-Language": "en"})
    assert r.status_code == 409
    assert r.json()["detail"] == "Please acknowledge the consent and data-protection notice first."
