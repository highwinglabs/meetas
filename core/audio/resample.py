"""Minimal, dependency-free linear audio resampler (Phase 5, live path).

The capture runs at the device's native rate (often 44.1 kHz) while faster-whisper
wants 16 kHz. We resample the streamed chunks on the fly. A linear resampler is
intentionally used (no scipy/librosa) because for ASR purposes it is more than
sufficient and keeps the live path offline and dependency-light. The batch path
still uses ffmpeg for the authoritative 16 kHz copy.
"""
from __future__ import annotations

import numpy as np


class StreamingLinearResampler:
    """Resample blocks continuously without resetting at every block."""

    def __init__(self, src_rate: int, dst_rate: int):
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("Sampleraten müssen positiv sein.")
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self._step = self.src_rate / float(self.dst_rate)
        self._position = 0.0
        self._buffer = np.zeros(0, dtype=np.float32)

    def process(self, samples: np.ndarray) -> np.ndarray:
        """Resample one mono block and return float32 output."""
        values = np.asarray(samples, dtype=np.float32).reshape(-1)
        if values.size == 0:
            return np.zeros(0, dtype=np.float32)
        if self.src_rate == self.dst_rate:
            return values.copy()
        self._buffer = (values.copy() if self._buffer.size == 0 else
                        np.concatenate((self._buffer, values)))
        available = len(self._buffer) - 1 - self._position
        count = max(0, int(np.ceil(available / self._step - 1e-12)))
        if count == 0:
            return np.zeros(0, dtype=np.float32)
        positions = self._position + np.arange(count, dtype=np.float64) * self._step
        indexes = np.floor(positions).astype(np.int64)
        fraction = (positions - indexes).astype(np.float32)
        output = self._buffer[indexes] + (
            self._buffer[indexes + 1] - self._buffer[indexes]) * fraction
        self._position += count * self._step
        consumed = min(len(self._buffer) - 1, int(self._position))
        if consumed:
            self._buffer = self._buffer[consumed:]
            self._position -= consumed
        return output.astype(np.float32, copy=False)


def resample_linear(x: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linearly resample a 1-D or (n, ch) float audio array from src_rate to dst_rate.

    Returns a float32 array of the same shape (last dim preserved) at dst_rate.
    No-op (returns a copy) when the rates are equal or the input is empty.
    """
    if x is None or x.size == 0:
        return np.asarray(x, dtype=np.float32)
    x = np.ascontiguousarray(x, dtype=np.float32)
    if src_rate == dst_rate:
        return x.copy()

    n = x.shape[0]
    ratio = dst_rate / float(src_rate)
    out_len = int(round(n * ratio))
    if out_len <= 0:
        return np.zeros((0,) + x.shape[1:], dtype=np.float32)

    # fractional output sample positions in the input domain
    pos = np.arange(out_len) / ratio
    idx = np.floor(pos).astype(np.int64)
    frac = (pos - idx).astype(np.float32)
    idx0 = np.clip(idx, 0, n - 1)
    idx1 = np.clip(idx + 1, 0, n - 1)

    a = x[idx0]
    b = x[idx1]
    out = a + (b - a) * frac.reshape(-1, *([1] * (a.ndim - 1)))
    return out.astype(np.float32)
