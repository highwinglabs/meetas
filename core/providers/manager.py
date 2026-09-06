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


class ProviderManager:
    def __init__(self, config: Config):
        self.config = config
        self._default: ASREngine | None = None
        self._parakeet_cache: dict[str, ParakeetEngine] = {}
        self._lock = threading.Lock()

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
        if model_name is None and self.config.asr_model.lower().startswith("parakeet"):
            return self._parakeet(self.config.asr_model)
        if model_name and model_name.lower().startswith("parakeet"):
            return self._parakeet(model_name)
        if model_name and model_name != self.config.asr_model:
            return FasterWhisperEngine(model_name=model_name,
                                       compute_type=self.config.asr_compute_type,
                                       config=self.config)
        if self._default is None:
            with self._lock:
                if self._default is None:
                    self._default = FasterWhisperEngine(
                        model_name=self.config.asr_model,
                        compute_type=self.config.asr_compute_type,
                        config=self.config)
        return self._default

    def clear_cache(self) -> None:
        """Release cached ASR engines after a settings/model change."""
        with self._lock:
            self._default = None
            self._parakeet_cache.clear()
