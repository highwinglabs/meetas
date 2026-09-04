"""Model providers (Phase 2: ASR).

Local-first by default. Any network action (e.g. model download) is gated:
network is OFF by default and downloads always require explicit confirmation.
"""
from core.providers.base import (
    ASREngine, ASRError, ASRSegment, ModelNotReadyError,
)
from core.providers.manager import ProviderManager
from core.providers.parakeet import ParakeetEngine

__all__ = [
    "ASREngine", "ASRError", "ASRSegment", "ModelNotReadyError", "ProviderManager",
    "ParakeetEngine",
]
