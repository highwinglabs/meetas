"""Live ASR engine abstraction (Phase 5).

A LiveASREngine transcribes a fixed, in-memory window of 16 kHz mono audio and
returns segments with times relative to the *start of the window* (0-based).
The pipeline applies the absolute offset. This keeps the (batch) ASR model
decoupled from the streaming concern: faster-whisper transcribes a clip, and we
simply call it on the rolling window every `period_s`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import tempfile
import wave
from pathlib import Path

import numpy as np

from core.providers.base import ASREngine, ASRSegment
from core.providers.parakeet import ParakeetEngine


class LiveASREngine(ABC):
    name: str = "base"

    @abstractmethod
    def is_ready(self) -> bool:
        """True when a model is available to transcribe windows."""

    @abstractmethod
    def transcribe_window(
        self, audio: np.ndarray, sample_rate: int, language: str | None = None
    ) -> list[ASRSegment]:
        """Transcribe the window; returned times are 0-based (window-relative)."""


class FasterWhisperLiveEngine(LiveASREngine):
    """Live engine backed by a faster-whisper model.

    Reuses a `FasterWhisperEngine` for all model management (download gate,
    loading, readiness) and transcribes in-memory windows with beam_size=1 for
    low latency (live display, not the authoritative batch transcript).
    """

    name = "faster-whisper-live"

    def __init__(self, base_engine: ASREngine, *, language: str | None = None,
                 display_name: str | None = None) -> None:
        self._base = base_engine
        self._default_lang = language
        if display_name:
            self.name = display_name

    def is_ready(self) -> bool:
        try:
            return self._base.is_model_ready()
        except Exception:
            return False

    def transcribe_window(
        self, audio: np.ndarray, sample_rate: int, language: str | None = None
    ) -> list[ASRSegment]:
        if audio is None or len(audio) == 0:
            return []
        return self._base.transcribe_array(
            audio, language or self._default_lang, beam_size=1
        )


class ParakeetLiveEngine(LiveASREngine):
    """Live adapter for an explicitly installed Parakeet command runtime.

    Parakeet runtimes do not share one stable Python API across distributions.
    The provider therefore exposes a small, documented JSON command contract.
    Windows are written to a private temporary WAV and passed to that runtime.
    This is slower than an in-process streaming API, but it is real Parakeet
    inference when the optional local adapter is installed; the service falls
    back to faster-whisper when it is not.
    """

    name = "parakeet-live"

    def __init__(self, base_engine: ParakeetEngine) -> None:
        self._base = base_engine

    def is_ready(self) -> bool:
        return self._base.is_model_ready()

    def transcribe_window(
        self, audio: np.ndarray, sample_rate: int, language: str | None = None
    ) -> list[ASRSegment]:
        if audio is None or len(audio) == 0:
            return []
        values = np.asarray(audio, dtype=np.float32)
        if values.ndim > 1:
            values = values.mean(axis=1)
        pcm = np.clip(values, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype(np.int16)
        with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
            path = Path(handle.name)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(int(sample_rate))
                wav.writeframes(pcm.tobytes())
            return self._base.transcribe(path, language=language)
