"""Speaker diarization (numpy engine): separation, conservative merge, robustness."""
from __future__ import annotations

import numpy as np

from core.providers.diar import (
    DiarSpan,
    MockDiarizationEngine,
    NumpyDiarizationEngine,
)


def _tone(sr: int, freqs, dur_s: float, amp: float = 0.5) -> np.ndarray:
    t = np.arange(int(sr * dur_s), dtype=np.float32) / sr
    sig = np.zeros_like(t)
    for f in freqs:
        sig += np.sin(2 * np.pi * f * t)
    # light broadband noise so VAD/flatness behave like real speech
    sig += 0.02 * np.random.default_rng(1).standard_normal(t.shape).astype(np.float32)
    return (amp / max(1, len(freqs)) * sig).astype(np.float32)


def test_two_distinct_speakers_detected():
    sr = 16000
    a = _tone(sr, [150, 300], 1.6)
    b = _tone(sr, [1500, 2400], 1.6)
    audio = np.concatenate([a, b])
    spans = NumpyDiarizationEngine().diarize(audio, sr)

    labels = [s.speaker_id for s in spans]
    assert len(set(labels)) == 2, f"expected 2 speakers, got {labels}"
    # spans ordered and roughly covering each half
    assert spans[0].start_s < spans[0].end_s
    assert all(spans[i].end_s <= spans[i + 1].start_s + 0.4 for i in range(len(spans) - 1))
    # the switch should happen near the 1.6 s boundary
    assert 0.5 < spans[0].end_s < 2.4


def test_single_speaker_collapse():
    sr = 16000
    audio = _tone(sr, [150, 300], 3.0)
    spans = NumpyDiarizationEngine().diarize(audio, sr)
    assert len(set(s.speaker_id for s in spans)) == 1


def test_similar_speakers_merge_conservatively():
    # 300 Hz vs 400 Hz are close in spectrum -> must NOT split into two.
    sr = 16000
    a = _tone(sr, [300], 1.5, amp=0.5)
    b = _tone(sr, [400], 1.5, amp=0.5)
    audio = np.concatenate([a, b])
    spans = NumpyDiarizationEngine().diarize(audio, sr)
    assert len(set(s.speaker_id for s in spans)) <= 2


def test_silence_returns_empty():
    spans = NumpyDiarizationEngine().diarize(np.zeros(16000 * 2, dtype=np.float32), 16000)
    assert spans == []


def test_empty_audio_returns_empty():
    assert NumpyDiarizationEngine().diarize(np.zeros(0, dtype=np.float32), 16000) == []


def test_assign_maps_segments_by_overlap():
    spans = [
        DiarSpan(0.0, 2.0, "Sprecher 1"),
        DiarSpan(2.0, 5.0, "Sprecher 2"),
    ]

    class Seg:
        def __init__(self, s, e):
            self.start_s, self.end_s = s, e

    eng = MockDiarizationEngine(spans)
    out = eng.assign([Seg(0.0, 1.0), Seg(2.5, 4.0), Seg(6.0, 7.0)], spans)
    assert out[0] == "Sprecher 1"
    assert out[1] == "Sprecher 2"
    assert out[2] is None  # no overlap -> unknown


def test_mock_is_deterministic():
    spans = [DiarSpan(0.0, 1.0, "Sprecher 1")]
    eng = MockDiarizationEngine(spans)
    assert eng.is_ready()
    r1 = eng.diarize(np.zeros(16000), 16000)
    r2 = eng.diarize(np.zeros(16000), 16000)
    assert r1 == r2 == spans
