"""ASR provider abstraction.

An ASREngine turns a local audio file into timestamped text segments. The
concrete engine (faster-whisper) is optional to import so the rest of the
system and the tests run without it. Model availability is explicit and gated:
nothing is downloaded unless the network is enabled AND the user confirmed.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class ASRError(RuntimeError):
    """Base error for ASR operations."""


class ModelNotReadyError(ASRError):
    """The model is not downloaded and no (confirmed) download was requested."""


@dataclass
class ASRSegment:
    """One timestamped transcription result (provider-agnostic)."""
    start_s: float
    end_s: float
    text: str
    language: str | None = None
    confidence: float | None = None
    raw_text: str = ""


class ASREngine(ABC):
    """A local speech-to-text engine."""

    model_name: str = "abstract"

    @abstractmethod
    def is_model_ready(self) -> bool:
        """True if the model files are present locally (no download needed)."""

    @abstractmethod
    def prepare_model(self, allow_download: bool = False) -> None:
        """Ensure the model is available.

        - If present -> load it.
        - If missing and allow_download is False -> raise ModelNotReadyError.
        - If missing and allow_download is True -> pass the network gate and
          download.
        """

    @abstractmethod
    def transcribe(self, audio_path: Path | str,
                   language: str | None = None) -> list[ASRSegment]:
        """Transcribe a local audio file into timestamped segments."""

    def transcribe_with_progress(self, audio_path: Path | str,
                                 language: str | None = None,
                                 on_segment: Callable[[ASRSegment], None] | None = None
                                 ) -> list[ASRSegment]:
        """Transcribe and optionally report accepted segments as they arrive.

        Providers that only expose a batch API inherit this compatibility
        implementation; streaming-capable providers override it.
        """
        segments = self.transcribe(audio_path, language=language)
        if on_segment:
            for segment in segments:
                on_segment(segment)
        return segments
