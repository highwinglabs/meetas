"""Lightweight, offline energy-based voice activity (Phase 1).

silero-VAD is an optional enhancement (later phase). Energy-based VAD is
fully offline and deterministic, which fits the no-network MVP constraint.
"""
from __future__ import annotations

import math

import numpy as np


def energy_rms_dbfs(samples: np.ndarray) -> float:
    """Return RMS level of audio (in [-1, 1]) in dBFS, -inf for silence."""
    if samples.size == 0:
        return float("-inf")
    flat = samples.reshape(-1)
    rms = float(np.sqrt(np.mean(np.square(flat))))
    if rms <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(rms)


def is_speaking(samples: np.ndarray, threshold_dbfs: float = -45.0) -> bool:
    """Energy gate. -45 dBFS is a conservative speech floor."""
    if samples.size == 0:
        return False
    return energy_rms_dbfs(samples) >= threshold_dbfs
