"""Offline tests for the optional whisper-cpp ASR backend (pywhispercpp).

The real binding needs a native (often Vulkan) build, so these tests inject a
fake ``pywhispercpp`` module into ``sys.modules`` and exercise the engine's
own logic: device resolution, model handling, segment normalization and the
ProviderManager dispatch.
"""
import sys
import types

import pytest


class _FakeSegment:
    """Mimics pywhispercpp Segment objects (t0/t1 in centiseconds)."""

    def __init__(self, t0: int, t1: int, text: str):
        self.t0 = t0
        self.t1 = t1
        self.text = text


class _FakeModel:
    instances: list["_FakeModel"] = []

    def __init__(self, model: str, context_params: dict | None = None,
                 **params):
        self.model = model
        self.context_params = context_params or {}
        self.params = params
        _FakeModel.instances.append(self)

    def transcribe(self, path: str, **kwargs) -> list[_FakeSegment]:
        self.transcribe_kwargs = kwargs
        return [
            _FakeSegment(0, 2000, " Hallo? "),
            _FakeSegment(2000, 2000, ""),      # empty -> skipped
            _FakeSegment(61000, 63000, "Ja, guck mal!"),
        ]


@pytest.fixture
def fake_pywhispercpp(monkeypatch):
    model_mod = types.ModuleType("pywhispercpp.model")
    model_mod.Model = _FakeModel
    pkg = types.ModuleType("pywhispercpp")
    pkg.model = model_mod
    monkeypatch.setitem(sys.modules, "pywhispercpp", pkg)
    monkeypatch.setitem(sys.modules, "pywhispercpp.model", model_mod)
    _FakeModel.instances.clear()
    return pkg


def _make_model_file(config, model_name: str = "small") -> None:
    config.models_dir.mkdir(parents=True, exist_ok=True)
    (config.models_dir / f"ggml-{model_name}.bin").write_bytes(b"fake")


def test_device_resolution_and_gpu_layers(fake_pywhispercpp):
    from core.providers.whisper_cpp import WhisperCppEngine
    cpu = WhisperCppEngine(model_name="small", device="cpu")
    assert cpu.resolved_device() == "cpu"
    assert cpu._gpu_layers() == 0
    vulkan = WhisperCppEngine(model_name="small", device="vulkan")
    assert vulkan.resolved_device() == "vulkan"
    assert vulkan._gpu_layers() == -1
    # 'cuda' means GPU intent for this backend (normalized to vulkan).
    cuda = WhisperCppEngine(model_name="small", device="cuda")
    assert cuda.resolved_device() == "vulkan"


def test_model_readiness_and_file_path(fake_pywhispercpp, config):
    from core.providers.whisper_cpp import WhisperCppEngine
    _make_model_file(config, "small")
    eng = WhisperCppEngine(model_name="small", config=config)
    assert eng._model_file().name == "ggml-small.bin"
    assert eng.is_model_ready() is True
    assert eng.model_size_note() != ""
    missing = WhisperCppEngine(model_name="medium", config=config)
    assert missing.is_model_ready() is False


def test_transcribe_normalizes_segments(fake_pywhispercpp, config):
    from core.providers.whisper_cpp import WhisperCppEngine
    _make_model_file(config, "small")
    eng = WhisperCppEngine(model_name="small", config=config)
    audio = config.base_dir / "audio.wav"
    audio.write_bytes(b"RIFF...")
    segs = eng.transcribe(audio, language="de")
    assert [s.text for s in segs] == ["Hallo?", "Ja, guck mal!"]
    # centiseconds -> seconds
    assert (segs[0].start_s, segs[0].end_s) == (0.0, 20.0)
    assert (segs[1].start_s, segs[1].end_s) == (610.0, 630.0)
    # quality settings passed through (beam_size 5 via nested dict)
    model = _FakeModel.instances[-1]
    assert model.transcribe_kwargs["beam_search"]["beam_size"] == 5
    assert model.transcribe_kwargs["language"] == "de"


def test_transcribe_uses_gpu_flag_for_vulkan(fake_pywhispercpp, config):
    from core.providers.whisper_cpp import WhisperCppEngine
    _make_model_file(config, "small")
    eng = WhisperCppEngine(model_name="small", device="vulkan", config=config)
    audio = config.base_dir / "audio.wav"
    audio.write_bytes(b"RIFF...")
    eng.transcribe(audio, language="de")
    assert _FakeModel.instances[-1].context_params == {"use_gpu": True}


def test_transcribe_requires_ready_model(fake_pywhispercpp, config):
    from core.providers.base import ModelNotReadyError
    from core.providers.whisper_cpp import WhisperCppEngine
    eng = WhisperCppEngine(model_name="small", config=config)
    audio = config.base_dir / "audio.wav"
    audio.write_bytes(b"RIFF...")
    with pytest.raises(ModelNotReadyError):
        eng.transcribe(audio)


def test_missing_binding_raises_helpful_error(config, monkeypatch):
    # Simulate "never installed": drop any cached binding modules so the lazy
    # import really fails (an already-imported binding is, by definition, present).
    monkeypatch.setitem(sys.modules, "pywhispercpp", None)
    monkeypatch.delitem(sys.modules, "pywhispercpp.model", raising=False)
    from core.providers.base import ASRError
    from core.providers.whisper_cpp import WhisperCppEngine
    _make_model_file(config, "small")
    eng = WhisperCppEngine(model_name="small", config=config)
    audio = config.base_dir / "audio.wav"
    audio.write_bytes(b"RIFF...")
    with pytest.raises(ASRError, match="whisper-cpp"):
        eng.transcribe(audio)


def test_provider_manager_dispatches_whisper_cpp(config, fake_pywhispercpp):
    _make_model_file(config, "small")
    config.asr_engine = "whisper-cpp"
    from core.providers.manager import ProviderManager
    from core.providers.whisper_cpp import WhisperCppEngine
    mgr = ProviderManager(config)
    assert isinstance(mgr.engine("small"), WhisperCppEngine)
    assert isinstance(mgr.engine(), WhisperCppEngine)
    mgr.clear_cache()
    assert mgr._whisper_cpp_cache == {}


def test_auto_engine_prefers_whisper_cpp_with_vulkan(config, monkeypatch):
    from core.providers import whisper_cpp
    from core.providers.manager import ProviderManager
    from core.providers.whisper_cpp import WhisperCppEngine
    config.asr_engine = "auto"
    monkeypatch.setattr(whisper_cpp, "binding_available", lambda: True)
    monkeypatch.setattr(whisper_cpp, "vulkan_available", lambda: True)
    mgr = ProviderManager(config)
    assert mgr.engine_name() == "whisper-cpp"
    assert isinstance(mgr.engine("small"), WhisperCppEngine)
    assert mgr.engine() is mgr.engine("small"), "same model must share the cache"


def test_auto_engine_falls_back_to_faster_whisper(config, monkeypatch):
    from core.providers import whisper_cpp
    from core.providers.faster_whisper import FasterWhisperEngine
    from core.providers.manager import ProviderManager
    config.asr_engine = "auto"
    # no binding -> CPU default
    monkeypatch.setattr(whisper_cpp, "binding_available", lambda: False)
    monkeypatch.setattr(whisper_cpp, "vulkan_available", lambda: True)
    mgr = ProviderManager(config)
    assert mgr.engine_name() == "faster-whisper"
    assert isinstance(mgr.engine("small"), FasterWhisperEngine)
    # binding present but no Vulkan -> still CPU default
    mgr._resolved_engine = None
    monkeypatch.setattr(whisper_cpp, "binding_available", lambda: True)
    monkeypatch.setattr(whisper_cpp, "vulkan_available", lambda: False)
    assert mgr.engine_name() == "faster-whisper"


def test_explicit_engine_selection_overrides_auto(config, fake_pywhispercpp):
    from core.providers.faster_whisper import FasterWhisperEngine
    from core.providers.manager import ProviderManager
    from core.providers.whisper_cpp import WhisperCppEngine
    config.asr_engine = "faster-whisper"
    assert isinstance(ProviderManager(config).engine("small"), FasterWhisperEngine)
    config.asr_engine = "whisper-cpp"
    assert isinstance(ProviderManager(config).engine("small"), WhisperCppEngine)


def test_clear_cache_re_resolves_auto_engine(config, monkeypatch):
    from core.providers import whisper_cpp
    from core.providers.manager import ProviderManager
    config.asr_engine = "auto"
    monkeypatch.setattr(whisper_cpp, "binding_available", lambda: True)
    monkeypatch.setattr(whisper_cpp, "vulkan_available", lambda: True)
    mgr = ProviderManager(config)
    assert mgr.engine_name() == "whisper-cpp"
    config.asr_engine = "faster-whisper"
    mgr.clear_cache()
    assert mgr.engine_name() == "faster-whisper"
