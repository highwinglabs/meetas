"""Phase 8: local provider selection + security defaults (audit).

Covers the per-role local provider resolution, the provider-status endpoint,
and the hard security invariants (loopback bind, no network by default).
Network-gating itself is covered in test_asr_provider.py / test_api_phase2.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api.app import create_app
from core.config import Config


@pytest.fixture
def client(make_service):
    service = make_service()
    app = create_app(service)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        service.close()


def test_config_security_defaults(config: Config) -> None:
    # Loopback-only bind and no network by default are the security contract.
    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.network_allowed is False
    assert config.downloads_require_confirmation is True


def test_provider_status_reports_every_role(make_service) -> None:
    svc = make_service()
    try:
        out = svc.provider_status()
        assert out["local"] is True
        assert out["network_allowed"] is False
        roles = out["roles"]
        for role in Config.MODEL_ROLES:
            assert role in roles
            st = roles[role]
            assert st["role"] == role
            assert st["local"] is True
            assert st["network_used"] is False
            assert st["model"] and st["base_url"]
        assert "diarization" in out
        assert "embeddings" in out
        assert "select_how" in out
    finally:
        svc.close()


def test_provider_status_reflects_profile_override(make_service, config: Config) -> None:
    config.model_profiles = {"analyse": {"model": "custom-local-1"}}
    svc = make_service()
    try:
        out = svc.provider_status()
        assert out["roles"]["analyse"]["model"] == "custom-local-1"
        # a role without an override inherits the main model
        assert out["roles"]["offline"]["model"] == config.llm_model
    finally:
        svc.close()


def test_provider_status_endpoint(client) -> None:
    r = client.get("/models/providers")
    assert r.status_code == 200
    data = r.json()
    assert data["local"] is True
    assert data["network_allowed"] is False
    assert set(Config.MODEL_ROLES).issubset(data["roles"].keys())


def test_collection_routes_not_shadowed_by_id_route(client) -> None:
    # Regression: GET /meetings/recurring-topics and /meetings/timeline are
    # collection routes and must NOT be swallowed by GET /meetings/{meeting_id}.
    r1 = client.get("/meetings/recurring-topics")
    assert r1.status_code == 200, r1.text
    assert "topics" in r1.json() or "count" in r1.json()

    r2 = client.get("/meetings/timeline", params={"granularity": "day"})
    assert r2.status_code == 200, r2.text
    assert "buckets" in r2.json()
