"""Provider manager: builds the active local ASR engine from configuration.

MVP uses faster-whisper. The manager caches the default engine and can build
an alternative one (e.g. a different model size) on demand. Tests inject their
own engine into the service instead of using this.
"""
from __future__ import annotations

import threading

from core.config import Config
from core.providers.base import ASREngine
from core.providers.faster_whisper import FasterWhisperEngine
from core.providers.parakeet import ParakeetEngine
from core.providers import whisper_cpp
from core.providers.whisper_cpp import WhisperCppEngine


def resolve_auto_engine(config: Config) -> str:
    """Resolve the "auto" ASR engine: GPU path when available, else CPU.

    1. whisper-cpp + Vulkan – the reliable GPU path for AMD/other Vulkan
       GPUs (CTranslate2's ROCm wheels crash on beam search, upstream bug).
    2. faster-whisper – whose device "auto" then picks CUDA (NVIDIA) when
       present and the CPU otherwise.
    """
    if whisper_cpp.binding_available() and whisper_cpp.vulkan_available():
        return "whisper-cpp"
    return "faster-whisper"


class ProviderManager:
    def __init__(self, config: Config):
        self.config = config
        self._default: ASREngine | None = None
        self._parakeet_cache: dict[str, ParakeetEngine] = {}
        self._whisper_cpp_cache: dict[str, WhisperCppEngine] = {}
        self._resolved_engine: str | None = None
        self._lock = threading.Lock()

    def engine_name(self) -> str:
        """Effective ASR engine ("auto" is resolved once per manager)."""
        if self.config.asr_engine == "auto":
            if self._resolved_engine is None:
                self._resolved_engine = resolve_auto_engine(self.config)
            return self._resolved_engine
        return self.config.asr_engine

    def _parakeet(self, model_name: str) -> ParakeetEngine:
        # Cache the (thread-safe) engine per model name: building a fresh
        # ParakeetEngine on every call re-created its state and would re-load the
        # model repeatedly (L18). Double-checked under the lock.
        key = model_name.lower()
        cached = self._parakeet_cache.get(key)
        if cached is None:
            with self._lock:
                cached = self._parakeet_cache.get(key)
                if cached is None:
                    cached = ParakeetEngine(model_name=model_name, config=self.config)
                    self._parakeet_cache[key] = cached
        return cached

    def engine(self, model_name: str | None = None) -> ASREngine:
        # Parakeet models are always routed by name, even when the batch
        # engine resolved to whisper-cpp: the ggml catalog has no Parakeet
        # weights, so switching engines must never break a Parakeet setup.
        if model_name is None and self.config.asr_model.lower().startswith("parakeet"):
            return self._parakeet(self.config.asr_model)
        if model_name and model_name.lower().startswith("parakeet"):
            return self._parakeet(model_name)
        if self.engine_name() == "whisper-cpp":
            return self._whisper_cpp(model_name or self.config.asr_model)
        if model_name and model_name != self.config.asr_model:
            return FasterWhisperEngine(model_name=model_name,
                                       compute_type=self.config.asr_compute_type,
                                       device=self.config.asr_device,
                                       config=self.config)
        if self._default is None:
            with self._lock:
                if self._default is None:
                    self._default = FasterWhisperEngine(
                        model_name=self.config.asr_model,
                        compute_type=self.config.asr_compute_type,
                        device=self.config.asr_device,
                        config=self.config)
        return self._default

    def _whisper_cpp(self, model_name: str) -> WhisperCppEngine:
        # Cache per model name (model loads are expensive), same pattern as
        # the Parakeet engines.
        key = model_name.lower()
        cached = self._whisper_cpp_cache.get(key)
        if cached is None:
            with self._lock:
                cached = self._whisper_cpp_cache.get(key)
                if cached is None:
                    cached = WhisperCppEngine(model_name=model_name,
                                              device=self.config.asr_device,
                                              config=self.config)
                    self._whisper_cpp_cache[key] = cached
        return cached

    def clear_cache(self) -> None:
        """Release cached ASR engines after a settings/model change."""
        with self._lock:
            self._default = None
            self._parakeet_cache.clear()
            self._whisper_cpp_cache.clear()
            self._resolved_engine = None
