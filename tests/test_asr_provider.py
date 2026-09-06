"""faster-whisper engine: model-readiness and the network/confirmation gate.

No real model is downloaded in unit tests; the download hook is stubbed to
verify the gate logic (network OFF by default, downloads need confirmation).
"""
from __future__ import annotations

import pytest

from core.providers.base import ASRError, ModelNotReadyError
from core.providers.faster_whisper import FasterWhisperEngine
from core.security.secrets import NetworkBlockedError


def test_model_not_ready_when_absent(config):
    eng = FasterWhisperEngine(model_name="tiny", config=config)
    assert eng.is_model_ready() is False


def test_prepare_without_download_raises_model_not_ready(config):
    eng = FasterWhisperEngine(model_name="tiny", config=config)
    config.network_allowed = True  # network on, but no confirmation given
    with pytest.raises(ModelNotReadyError):
        eng.prepare_model(allow_download=False)


def test_download_blocked_when_network_off(config):
    eng = FasterWhisperEngine(model_name="tiny", config=config)
    assert config.network_allowed is False
    with pytest.raises(NetworkBlockedError):
        eng.prepare_model(allow_download=True)  # confirmed, but network disabled


def test_download_proceeds_when_confirmed_and_network_on(config, monkeypatch):
    config.network_allowed = True
    eng = FasterWhisperEngine(model_name="tiny", config=config)
    called = {}

    def fake_load(**_kw):
        mf = eng._model_file()
        mf.parent.mkdir(parents=True, exist_ok=True)
        mf.write_bytes(b"stub")
        called["yes"] = True

    monkeypatch.setattr(eng, "_load_model", fake_load)
    eng.prepare_model(allow_download=True)
    assert called.get("yes")
    assert eng.is_model_ready() is True


def test_ready_model_loads_without_network(config, monkeypatch):
    eng = FasterWhisperEngine(model_name="tiny", config=config)
    # simulate an already-downloaded model
    mf = eng._model_file()
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_bytes(b"stub")
    loaded = {}
    monkeypatch.setattr(eng, "_load_model", lambda **_kw: loaded.setdefault("yes", True) or eng._model)
    assert eng.is_model_ready() is True
    eng.prepare_model(allow_download=False)  # must not raise, no network needed
    assert loaded.get("yes")


def test_ready_model_recognises_current_turbo_publisher_cache(config):
    eng = FasterWhisperEngine(model_name="large-v3-turbo", config=config)
    model = config.models_dir / "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo" / "snapshots" / "revision"
    model.mkdir(parents=True, exist_ok=True)
    (model / "model.bin").write_bytes(b"stub")
    assert eng.is_model_ready() is True


def test_transcribe_without_model_raises(config):
    eng = FasterWhisperEngine(model_name="tiny", config=config)
    with pytest.raises(ModelNotReadyError):
        eng.transcribe(config.audio_dir / "nope.wav")


def test_transcribe_missing_audio_raises(config):
    eng = FasterWhisperEngine(model_name="tiny", config=config)
    mf = eng._model_file()
    mf.parent.mkdir(parents=True, exist_ok=True)
    mf.write_bytes(b"stub")
    with pytest.raises(ASRError):
        eng.transcribe(config.audio_dir / "does_not_exist.wav")


def test_provider_manager_caches_parakeet_engine(config, monkeypatch):
    # L18: repeated engine() calls must reuse one ParakeetEngine instead of
    # building a fresh instance every time; clear_cache invalidates.
    from core.providers.manager import ProviderManager
    monkeypatch.setattr(config, "asr_model", "parakeet-tdt-0.6b-v3-int8")
    manager = ProviderManager(config)
    first = manager.engine()
    second = manager.engine()
    assert first is second, "parakeet engine was rebuilt instead of cached"
    manager.clear_cache()
    third = manager.engine()
    assert third is not first, "clear_cache did not invalidate the parakeet cache"


def test_provider_manager_caches_default_whisper_engine(config, monkeypatch):
    # The default faster-whisper engine is cached too (lock-guarded).
    from core.providers.manager import ProviderManager
    monkeypatch.setattr(config, "asr_model", "small")
    manager = ProviderManager(config)
    first = manager.engine()
    assert first is manager.engine()
    manager.clear_cache()
    assert manager._default is None
    assert manager.engine() is not first
