"""Resampler: length correctness + frequency preservation (no scipy)."""
from __future__ import annotations

import numpy as np

from core.audio.resample import StreamingLinearResampler, resample_linear


def test_equal_rate_is_noop():
    x = np.arange(100, dtype=np.float32)
    out = resample_linear(x, 16000, 16000)
    assert out.shape == x.shape
    assert np.array_equal(out, x)


def test_downsample_length_is_correct():
    n = 44100  # 1s at 44.1k
    x = np.ones(n, dtype=np.float32)
    out = resample_linear(x, 44100, 16000)
    assert abs(len(out) - 16000) <= 1


def test_upsample_length_is_correct():
    n = 8000  # 0.5s at 16k
    x = np.ones(n, dtype=np.float32)
    out = resample_linear(x, 16000, 44100)
    assert abs(len(out) - int(0.5 * 44100)) <= 2


def test_sine_frequency_preserved():
    # A 500 Hz tone resampled 44.1k -> 16k must still be ~500 Hz.
    sr = 44100
    t = np.arange(sr, dtype=np.float32) / sr
    tone = np.sin(2 * np.pi * 500.0 * t).astype(np.float32)
    out = resample_linear(tone, sr, 16000)
    # dominant frequency via a coarse DFT over the 16k signal
    spec = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    freqs = np.fft.rfftfreq(len(out), d=1.0 / 16000)
    peak = freqs[int(np.argmax(spec))]
    assert abs(peak - 500.0) < 8.0, f"expected ~500 Hz, got {peak:.1f} Hz"


def test_channels_preserved():
    n = 44100
    x = np.random.default_rng(0).standard_normal((n, 2)).astype(np.float32)
    out = resample_linear(x, 44100, 16000)
    assert out.ndim == 2 and out.shape[1] == 2
    assert abs(out.shape[0] - 16000) <= 1


def test_empty_returns_empty():
    out = resample_linear(np.zeros(0, dtype=np.float32), 44100, 16000)
    assert out.size == 0


def test_streaming_resampler_keeps_block_boundaries_continuous():
    sr = 44100
    x = np.sin(2 * np.pi * 500.0 * np.arange(sr, dtype=np.float32) / sr)
    streaming = StreamingLinearResampler(sr, 16000)
    out = np.concatenate([
        streaming.process(x[start:start + 4410])
        for start in range(0, len(x), 4410)
    ])
    expected = resample_linear(x, sr, 16000)
    assert abs(len(out) - len(expected)) <= 1
    n = min(len(out), len(expected))
    assert np.max(np.abs(out[:n] - expected[:n])) < 0.02
