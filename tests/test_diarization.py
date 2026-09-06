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


def _reference_detect_changes(engine, feats: np.ndarray, times: np.ndarray):
    """The former O(n²) implementation, kept as the functional reference."""
    import numpy as np

    hop_s = float(times[1] - times[0]) if len(times) > 1 else 0.02
    n = len(feats)
    smooth_win = max(1, int(round(0.3 / hop_s)))
    kernel = np.ones(smooth_win, dtype=np.float32) / smooth_win
    sm = np.stack(
        [np.convolve(feats[:, k], kernel, mode="same") for k in range(feats.shape[1])],
        axis=1,
    )
    sm = sm / np.clip(np.linalg.norm(sm, axis=1, keepdims=True), 1e-12, None)
    regions: list[tuple[int, int]] = []
    start = 0
    for i in range(1, n):
        region_mean = np.sum(feats[start : i + 1], axis=0)
        region_mean = region_mean / max(1e-12, np.linalg.norm(region_mean))
        d = float(1.0 - np.clip(np.dot(sm[i], region_mean), -1.0, 1.0))
        region_len_s = (i - start) * hop_s
        if d > engine._change_thr and region_len_s >= engine._min_region_s:
            regions.append((start, i))
            start = i
    regions.append((start, n - 1))
    return regions


def _reference_cluster_regions(engine, regions, feats):
    """The former O(k³) pure-Python implementation, kept as the functional
    reference for the vectorized Lance-Williams rewrite (H1)."""
    import numpy as np

    n = len(regions)
    if n == 1:
        return [0]
    means = [np.sum(feats[a : b + 1], axis=0) for a, b in regions]
    means = [m / max(1e-12, np.linalg.norm(m)) for m in means]
    D = np.zeros((n, n), dtype=np.float64)
    for i in range(n):
        for j in range(i + 1, n):
            d = float(1.0 - np.clip(np.dot(means[i], means[j]), -1.0, 1.0))
            D[i, j] = D[j, i] = d
    clusters: list[list[int]] = [[i] for i in range(n)]
    while True:
        best, best_d = None, engine._cluster_thr
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                d = float(np.mean([D[i, j] for i in clusters[a] for j in clusters[b]]))
                if d < best_d:
                    best_d, best = d, (a, b)
        if best is None:
            break
        a, b = best
        clusters[a] = clusters[a] + clusters[b]
        clusters.pop(b)
    if len(clusters) > engine._max_speakers:
        while len(clusters) > engine._max_speakers:
            best, best_d = None, float("inf")
            for a in range(len(clusters)):
                for b in range(a + 1, len(clusters)):
                    d = float(np.mean([D[i, j] for i in clusters[a] for j in clusters[b]]))
                    if d < best_d:
                        best_d, best = d, (a, b)
            a, b = best
            clusters[a] = clusters[a] + clusters[b]
            clusters.pop(b)
    out = [0] * n
    for ci, members in enumerate(clusters):
        for m in members:
            out[m] = ci
    return out


def _partition_signature(labels: list[int]):
    groups: dict[int, list[int]] = {}
    for region, lab in enumerate(labels):
        groups.setdefault(lab, []).append(region)
    return frozenset(frozenset(v) for v in groups.values()), len(groups)


def test_cluster_regions_matches_reference():
    """H1: vectorized group-average linkage must produce the same region
    partition as the O(k³) reference. With k <= _max_exact_d the initial
    distance matrix is built with the same per-pair np.dot calls, so the only
    residual difference is the summation order of the Lance-Williams update
    (group average), which cannot flip a merge decision except for exact ties."""
    rng = np.random.default_rng(7)
    n_regions = 30
    n_frames = 6000
    # Meeting-like shape: long regions (200 frames) whose means wash out the
    # float32 GEMM-vs-dot rounding (max ~1e-9), 6 speaker directions with
    # small per-region jitter, and realistic speech-like noise.
    centers = rng.normal(size=(6, 3))
    centers = centers / np.linalg.norm(centers, axis=1, keepdims=True)
    feats = np.zeros((n_frames, 3), dtype=np.float32)
    step = n_frames // n_regions
    for r in range(n_regions):
        c = centers[r % 6] + 0.01 * rng.standard_normal(3)
        a, b = r * step, n_frames if r == n_regions - 1 else (r + 1) * step
        feats[a:b] = c[None, :] + 0.02 * rng.standard_normal((b - a, 3))
    feats = feats / np.clip(np.linalg.norm(feats, axis=1, keepdims=True), 1e-12, None)
    regions = [
        (r * step, n_frames - 1 if r == n_regions - 1 else (r + 1) * step - 1)
        for r in range(n_regions)
    ]

    eng = NumpyDiarizationEngine()
    got = eng._cluster_regions(regions, feats)
    want = _reference_cluster_regions(eng, regions, feats)
    sig_got, k_got = _partition_signature(got)
    sig_want, k_want = _partition_signature(want)
    assert k_got == k_want, f"{k_got} clusters vs {k_want} reference"
    assert sig_got == sig_want, "region partitions differ"


def test_cluster_regions_folds_to_max_speakers():
    """H1: the >max_speakers folding path stays capped after vectorization."""
    rng = np.random.default_rng(11)
    n_regions = 30
    n_frames = 3000
    centers = rng.normal(size=(8, 3))
    centers = centers / np.linalg.norm(centers, axis=1, keepdims=True)
    feats = np.zeros((n_frames, 3), dtype=np.float32)
    step = n_frames // n_regions
    for r in range(n_regions):
        c = centers[r % 8]
        a, b = r * step, (r + 1) * step
        feats[a:b] = c[None, :] + 0.05 * rng.standard_normal((b - a, 3))
    feats = feats / np.clip(np.linalg.norm(feats, axis=1, keepdims=True), 1e-12, None)
    regions = [(r * step, (r + 1) * step - 1) for r in range(n_regions)]
    eng = NumpyDiarizationEngine(max_speakers=3)
    got = eng._cluster_regions(regions, feats)
    _, k = _partition_signature(got)
    assert k <= 3


def test_detect_changes_matches_reference():
    """H1: the O(n·d) incremental walk must cut the same regions as the
    O(n²) reference (up to summation-order rounding at the decision border)."""
    eng = NumpyDiarizationEngine()
    rng = np.random.default_rng(42)
    n, d = 4000, 3
    # block-structured features so multiple cuts happen
    feats = np.zeros((n, d), dtype=np.float32)
    for block, center in enumerate([rng.normal(size=d) for _ in range(5)]):
        a, b = block * n // 5, (block + 1) * n // 5
        feats[a:b] = center[None, :] + 0.05 * rng.standard_normal((b - a, d))
    feats = feats / np.clip(np.linalg.norm(feats, axis=1, keepdims=True), 1e-12, None)
    times = np.arange(n, dtype=np.float32) * 0.02
    got = eng._detect_changes(feats, times)
    want = _reference_detect_changes(eng, feats, times)
    assert len(got) == len(want), f"{len(got)} regions vs {len(want)} reference"
    max_off = max(
        max(abs(ga - wa), abs(gb - wb))
        for (ga, gb), (wa, wb) in zip(got, want)
    )
    assert max_off <= 1, f"region boundaries differ by more than one frame: {got} vs {want}"
