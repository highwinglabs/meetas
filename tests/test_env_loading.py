"""Environment and ``.env`` loading. Offline and deterministic.

``load_dotenv`` writes to the process environment directly (not through
monkeypatch), so every test uses ``monkeypatch.setenv``/``delenv`` for the keys it
touches: monkeypatch records the original value and restores it at test end, which
also removes any value ``.env`` loading introduced. This keeps the suite isolated.
"""
from __future__ import annotations

import pytest

from core.config import Config


def test_env_file_values_are_applied(tmp_path, monkeypatch):
    monkeypatch.delenv("MA_PORT", raising=False)
    monkeypatch.delenv("MA_LOG_LEVEL", raising=False)
    (tmp_path / ".env").write_text("MA_PORT=9911\nMA_LOG_LEVEL=DEBUG\n")
    monkeypatch.setenv("MA_BASE_DIR", str(tmp_path))

    cfg = Config.load()

    assert cfg.port == 9911
    assert cfg.log_level == "DEBUG"


def test_real_environment_wins_over_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("MA_PORT=9911\n")
    # A real environment variable must take precedence over the .env value.
    monkeypatch.setenv("MA_PORT", "8123")
    monkeypatch.setenv("MA_BASE_DIR", str(tmp_path))

    cfg = Config.load()

    assert cfg.port == 8123


def test_missing_env_file_keeps_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("MA_PORT", raising=False)
    # No .env in the base dir and none in the (repo) cwd.
    monkeypatch.setenv("MA_BASE_DIR", str(tmp_path))

    cfg = Config.load()

    assert cfg.port == 8765
    assert cfg.network_allowed is False


def test_env_file_in_cwd_is_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("MA_PORT", raising=False)
    base = tmp_path / "elsewhere"
    base.mkdir()
    monkeypatch.chdir(tmp_path)                      # a .env exists in the cwd...
    (tmp_path / ".env").write_text("MA_PORT=9444\n")
    monkeypatch.setenv("MA_BASE_DIR", str(base))     # ...and the base dir has none

    cfg = Config.load()

    assert cfg.port == 9444


def test_env_file_can_set_base_dir(tmp_path, monkeypatch):
    """Regression: MA_BASE_DIR in the project-local .env must take effect.

    The base dir is resolved from the environment, so the cwd .env has to be
    loaded before ``default_base_dir()`` runs.
    """
    monkeypatch.delenv("MA_BASE_DIR", raising=False)
    monkeypatch.delenv("MA_PORT", raising=False)
    target = tmp_path / "target_base"
    target.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    (proj / ".env").write_text(f"MA_BASE_DIR={target}\nMA_PORT=9701\n")

    cfg = Config.load()

    assert cfg.base_dir == target
    assert cfg.port == 9701
