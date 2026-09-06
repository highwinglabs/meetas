"""Small, dependency-free microphone cleanup primitives.

The live and archival capture paths must stay usable on a fresh, offline
installation. This module removes low-frequency rumble and prevents a hot
microphone from clipping. System audio is never passed through these filters.
"""
from __future__ import annotations

import math

import numpy as np


class MicEnhancer:
    """Stateful high-pass filter with a conservative peak limiter."""

    def __init__(self, sample_rate: int, high_pass_hz: float = 80.0,
                 limiter_peak: float = 0.97):
        if sample_rate <= 0:
            raise ValueError("sample_rate muss positiv sein.")
        self.sample_rate = int(sample_rate)
        self.high_pass_hz = max(0.0, float(high_pass_hz))
        self.limiter_peak = min(1.0, max(0.5, float(limiter_peak)))
        self._previous_input: np.ndarray | None = None
        self._previous_output: np.ndarray | None = None
        if self.high_pass_hz:
            rc = 1.0 / (2.0 * math.pi * self.high_pass_hz)
            dt = 1.0 / self.sample_rate
            self._hp_alpha = rc / (rc + dt)
        else:
            self._hp_alpha = 1.0

    def process(self, samples: np.ndarray, mic_channels: int | None = None) -> np.ndarray:
        """Return enhanced float32 audio, preserving shape and channel count."""
        if samples is None or np.asarray(samples).size == 0:
            return np.asarray(samples, dtype=np.float32)
        was_mono = np.asarray(samples).ndim == 1
        out = np.ascontiguousarray(samples, dtype=np.float32).copy()
        if out.ndim == 1:
            out = out.reshape(-1, 1)
        count = out.shape[1] if mic_channels is None else min(
            out.shape[1], max(0, mic_channels))
        if count == 0:
            return out
        mic = out[:, :count]
        if self.high_pass_hz:
            previous_input = (np.zeros(count, dtype=np.float32)
                              if self._previous_input is None
                              else self._previous_input[:count])
            previous_output = (np.zeros(count, dtype=np.float32)
                               if self._previous_output is None
                               else self._previous_output[:count])
            filtered = np.empty_like(mic)
            alpha = self._hp_alpha
            for index in range(len(mic)):
                current = mic[index]
                previous_output = alpha * (previous_output + current - previous_input)
                filtered[index] = previous_output
                previous_input = current
            self._previous_input = previous_input.copy()
            self._previous_output = previous_output.copy()
            mic = filtered
        peak = float(np.max(np.abs(mic))) if mic.size else 0.0
        if peak > self.limiter_peak:
            mic = mic * (self.limiter_peak / peak)
        out[:, :count] = np.clip(mic, -1.0, 1.0)
        return out[:, 0] if was_mono else out
