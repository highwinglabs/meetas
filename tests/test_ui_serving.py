"""Phase 4: the core serves the built React frontend on loopback WITHOUT
shadowing the REST API. No browser and no network -- only static-file serving
of a (fake) build dir is exercised here. The real UI build is produced by
`npm run build` in ``ui/`` and served from ``ui/dist``.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from core.api.app import create_app


def _make_dist(base) -> "object":
    from pathlib import Path

    dist = Path(base) / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div>'
        '<script src="/assets/app.js"></script></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("console.log('ui');", encoding="utf-8")
    return dist


def test_ui_is_served_and_api_is_not_shadowed(make_service, tmp_path):
    dist = _make_dist(tmp_path)
    service = make_service()
    app = create_app(service, ui_dist=dist)
    with TestClient(app) as c:
        # the SPA index is served at the root
        r = c.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert 'id="root"' in r.text
        # the JS asset is served
        r2 = c.get("/assets/app.js")
        assert r2.status_code == 200
        assert "console.log" in r2.text
        # REST routes registered before the UI are untouched
        assert c.get("/health").status_code == 200
        assert c.get("/meetings").status_code == 200
        assert c.get("/consent").status_code == 200
        assert c.get("/models/llm").status_code == 200


def test_favicons_are_served_from_dist_root(make_service, tmp_path):
    # Browsers request the tab icon at the dist root; the fixed icon paths
    # are registered explicitly (no catch-all), with long caching.
    dist = _make_dist(tmp_path)
    (dist / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
    (dist / "favicon-32.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    service = make_service()
    app = create_app(service, ui_dist=dist)
    with TestClient(app) as c:
        r = c.get("/favicon.svg")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/svg+xml"
        assert "max-age=31536000" in r.headers["cache-control"]
        assert c.get("/favicon-32.png").status_code == 200
        # A variant that is not part of the build must be a plain 404.
        assert c.get("/favicon-16.png").status_code == 404
        # REST routes stay reachable (no catch-all shadowing).
        assert c.get("/meetings").status_code == 200


def test_no_dist_means_pure_api(make_service, tmp_path):
    service = make_service()
    app = create_app(service, ui_dist=tmp_path / "does-not-exist")
    with TestClient(app) as c:
        # without a build there is no UI route: root is a plain 404
        assert c.get("/").status_code == 404
        assert c.get("/assets/app.js").status_code == 404
        # the REST API still fully works
        assert c.get("/health").status_code == 200
        assert c.get("/meetings").status_code == 200


def test_explicit_dist_wins_over_env(make_service, tmp_path, monkeypatch):
    dist = _make_dist(tmp_path)
    other = tmp_path / "other-dist"
    other.mkdir()
    (other / "index.html").write_text("<html>OTHER</html>", encoding="utf-8")
    monkeypatch.setenv("MA_UI_DIST", str(other))
    service = make_service()
    app = create_app(service, ui_dist=dist)  # explicit beats the env default
    with TestClient(app) as c:
        assert 'id="root"' in c.get("/").text
