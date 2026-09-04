"""P7: backup/restore REST endpoints are wired and reachable (UI parity).

Confirms the exact HTTP surface the new "Sicherung" tab depends on:
POST /backup, GET /backup, POST /backup/restore (dry-run + confirm)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.audio.stream import SyntheticSource
from core.config import get_config
from core.service import MeetingService
from core.store.db import get_engine, make_engine
from core.store.models import Base, Meeting
from tests.conftest import MockASREngine


@pytest.fixture
def client():
    config = get_config()
    make_engine(config)
    Base.metadata.create_all(get_engine())

    def factory(d, sr, ch):
        return SyntheticSource(2.0, sr, ch, block_seconds=0.05)

    service = MeetingService(config, source_factory=factory, asr_engine=MockASREngine())
    service.record_consent(True)
    # seed one finished meeting so a db backup has content
    mid = service.start_meeting(title="Seed", source="mic")
    service._sessions[mid].wait_done(timeout=10)
    service.stop(mid)

    app = create_app(service)
    with TestClient(app) as c:
        yield c, service


def test_backup_endpoints_roundtrip(client):
    c, svc = client

    # create a db backup via REST
    r = c.post("/backup", json={"kind": "db", "note": "rest-test"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "db"
    assert body["integrity"] == "ok"
    assert body["size"] > 0

    # it is listed
    r = c.get("/backup")
    assert r.status_code == 200
    listed = r.json()["backups"]
    assert any(b["id"] == body["id"] and b["exists"] for b in listed)

    # dry-run restore changes nothing
    r = c.post("/backup/restore", json={"backup_id": body["id"], "confirm": False})
    assert r.status_code == 200
    assert r.json()["applied"] is False

    # confirm restore applies and records a safety snapshot
    r = c.post("/backup/restore", json={"backup_id": body["id"], "confirm": True})
    assert r.status_code == 200
    out = r.json()
    assert out["applied"] is True
    assert out["safety_backup"]


def test_backup_kind_validation(client):
    c, svc = client
    r = c.post("/backup", json={"kind": "bogus"})
    assert r.status_code in (400, 422, 500)  # rejected, not silently created
    r2 = c.post("/backup", json={"kind": "full"})
    assert r2.status_code == 200
    assert r2.json()["kind"] == "full"
