"""Local, self-contained speaker diarization (Phase 5).

No external ML models, no network, no new dependencies: features are computed
with numpy only (per-frame band energies + spectral centroid + zero-crossing
rate), a light energy VAD selects voiced frames, speaker changes are found by
cosine change-detection on a smoothed feature curve, and the resulting regions
are grouped by agglomerative (average-linkage) clustering.

It is intentionally conservative: when there is no clear evidence for a second
speaker it collapses to a single speaker, which matches most 1-on-1 meetings
and degrades gracefully to "Sprecher 1" instead of hallucinating speakers.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

from core.providers.base import ASRError


@dataclass(frozen=True)
class DiarSpan:
    start_s: float
    end_s: float
    speaker_id: str


class DiarizationEngine(ABC):
    name: str = "base"

    @abstractmethod
    def is_ready(self) -> bool:
        """True when the engine can produce spans (offline engines: always)."""

    @abstractmethod
    def diarize(self, audio: np.ndarray, sample_rate: int) -> list[DiarSpan]:
        """Return ordered speaker spans covering the voiced parts of `audio`."""

    def assign(self, segments, spans: list[DiarSpan]) -> list[str | None]:
        """Map ASR segments (objects with start_s/end_s) to speakers by overlap."""
        out: list[str | None] = []
        for seg in segments:
            best_id: str | None = None
            best_ov = 0.0
            for sp in spans:
                ov = max(0.0, min(seg.end_s, sp.end_s) - max(seg.start_s, sp.start_s))
                if ov > best_ov:
                    best_ov = ov
                    best_id = sp.speaker_id
            out.append(best_id if best_ov > 0.05 else None)
        return out


def _hanning(n: int) -> np.ndarray:
    if n <= 1:
        return np.ones(n, dtype=np.float32)
    return np.hanning(n).astype(np.float32)


def _feature_frames(audio: np.ndarray, sample_rate: int, frame_ms: int, hop_ms: int):
    """Return (features, frame_times, voiced) for a mono float signal.

    features: (n_frames, F) L2-normalized rows.
    frame_times: (n_frames,) start time in seconds.
    voiced: (n_frames,) bool, energy-based VAD.
    """
    sr = int(sample_rate)
    frame_len = max(16, int(sr * frame_ms / 1000.0))
    hop = max(1, int(sr * hop_ms / 1000.0))
    n = len(audio)
    if n < frame_len:
        return np.zeros((0, 1), dtype=np.float32), np.zeros(0, dtype=np.float32), np.zeros(0, dtype=bool)
    n_frames = 1 + (n - frame_len) // hop
    idx = (np.arange(n_frames)[:, None] * hop) + np.arange(frame_len)[None, :]
    idx = np.clip(idx, 0, n - 1)
    frames = audio[idx].astype(np.float32) * _hanning(frame_len)

    spec = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(frame_len, d=1.0 / sr)
    edges = [0, 150, 400, 1000, 2500, 5000, 8000]
    band_cols = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (freqs >= lo) & (freqs < hi)
        if not mask.any():
            mask = np.ones_like(mask, dtype=bool)
        band_cols.append(np.log1p((spec[:, mask] ** 2).sum(axis=1)))
    centroid = ((freqs * spec).sum(axis=1) / (spec.sum(axis=1) + 1e-12)) / 1000.0
    sign = np.sign(frames)
    zcr = ((sign[:, 1:] * sign[:, :-1] < 0).sum(axis=1) / max(1, frame_len - 1)) * 100.0

    feats = np.column_stack([np.column_stack(band_cols), centroid, zcr]).astype(np.float32)
    norms = np.linalg.norm(feats, axis=1, keepdims=True)
    feats = feats / np.clip(norms, 1e-12, None)

    frame_times = (np.arange(n_frames) * hop) / float(sr)
    power = (frames ** 2).mean(axis=1)
    dbfs = 10.0 * np.log10(power + 1e-12)
    voiced = dbfs > -45.0
    return feats, frame_times, voiced


class NumpyDiarizationEngine(DiarizationEngine):
    """Offline, numpy-only diarization. See module docstring for the approach."""

    name = "numpy-diar"

    # Up to this many regions, the pairwise distance matrix is built with the
    # same per-pair np.dot calls as before (bit-identical merge decisions);
    # above it a single GEMM is used (the previous implementation was
    # already unusable at that scale).
    _max_exact_d = 2000

    def __init__(
        self,
        *,
        frame_ms: int = 30,
        hop_ms: int = 20,
        min_region_s: float = 0.25,
        change_cos_threshold: float = 0.22,
        cluster_dist_threshold: float = 0.34,
        min_speakers: int = 1,
        max_speakers: int = 6,
    ) -> None:
        self._frame_ms = frame_ms
        self._hop_ms = hop_ms
        self._min_region_s = min_region_s
        self._change_thr = change_cos_threshold
        self._cluster_thr = cluster_dist_threshold
        self._min_speakers = max(1, min_speakers)
        self._max_speakers = max(min_speakers, max_speakers)

    def is_ready(self) -> bool:
        return True

    def diarize(self, audio: np.ndarray, sample_rate: int) -> list[DiarSpan]:
        if audio is None or len(audio) == 0:
            return []
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        audio = np.ascontiguousarray(audio, dtype=np.float32)

        feats, times, voiced = _feature_frames(audio, sample_rate, self._frame_ms, self._hop_ms)
        vf = np.where(voiced)[0]
        if len(vf) < 3:
            return []
        v_feats = feats[vf]
        v_times = times[vf]

        regions = self._detect_changes(v_feats, v_times)
        if not regions:
            return []
        cluster_of = self._cluster_regions(regions, v_feats)
        return self._to_spans(regions, cluster_of, v_times, self._hop_ms / 1000.0)

    def _detect_changes(self, feats: np.ndarray, times: np.ndarray) -> list[tuple[int, int]]:
        """Walk voiced frames, cutting a new region when the feature drifts.

        O(n·d): the running region sum is accumulated incrementally instead of
        re-summing the whole region for every frame (which was O(n²·d)). The
        per-frame comparison is unchanged, so region boundaries stay identical
        up to floating-point rounding of the summation order.
        """
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
        region_sum = feats[0].copy()
        for i in range(1, n):
            region_sum = region_sum + feats[i]
            region_mean = region_sum / max(1e-12, np.linalg.norm(region_sum))
            d = float(1.0 - np.clip(np.dot(sm[i], region_mean), -1.0, 1.0))
            region_len_s = (i - start) * hop_s
            if d > self._change_thr and region_len_s >= self._min_region_s:
                regions.append((start, i))
                start = i
                region_sum = feats[i].copy()
        regions.append((start, n - 1))
        return regions

    def _cluster_regions(self, regions: list[tuple[int, int]], feats: np.ndarray) -> list[int]:
        """Return a cluster index per region (0..K-1), by average-linkage cosine.

        The agglomerative loop updates a group-average distance matrix with the
        Lance-Williams rule, so each merge is a vectorized O(k) step instead of an
        O(|A|·|B|) Python loop (the whole clustering was O(k³) interpreted ops).
        The initial distance matrix is built from the same per-pair np.dot calls
        as before (bit-identical merge decisions) up to ``_max_exact_d`` regions,
        then a single GEMM. Merge semantics are preserved: strict '<' against the
        threshold, ties resolved by the first pair in row-major scan order, the
        higher-indexed cluster is popped.
        """
        n = len(regions)
        if n == 1:
            return [0]
        # Per-vector sum + normalization, exactly as before (a 1D norm is not
        # bit-identical to an axis=1 norm of the stacked array; the per-vector
        # loop keeps the unit means -- and hence every merge decision below
        # 2000 regions -- identical to the previous implementation).
        means = [np.sum(feats[a : b + 1], axis=0) for a, b in regions]
        means = [m / max(1e-12, np.linalg.norm(m)) for m in means]
        means = np.stack(means, axis=0)
        if n <= self._max_exact_d:
            # Bit-identical to the former per-pair np.dot (same inputs, same
            # BLAS call), so merge decisions on realistic region counts are
            # unchanged. Bounded: 2000 regions -> ~2M tiny dots, well under a
            # second of the previous runtime.
            C = np.zeros((n, n), dtype=np.float64)
            for i in range(n):
                for j in range(i + 1, n):
                    d = float(1.0 - np.clip(np.dot(means[i], means[j]), -1.0, 1.0))
                    C[i, j] = C[j, i] = d
        else:
            # Above this count the previous O(k³) pure-Python clustering was
            # already unusable (minutes..hours), so the ULP-level difference
            # between a GEMM and per-pair dots is acceptable.
            G = means @ means.T
            C = (1.0 - np.clip(G, -1.0, 1.0)).astype(np.float64)
            np.fill_diagonal(C, 0.0)

        sizes = np.ones(n, dtype=np.float64)
        root = list(range(n))  # region id of each current cluster (list order = cluster index)
        parent = list(range(n))  # union-find over region ids: b's root attaches to a's root

        def merge(a: int, b: int) -> None:
            nonlocal C, sizes, root, parent
            sa, sb = sizes[a], sizes[b]
            keep = [i for i in range(len(root)) if i not in (a, b)]
            for c in keep:
                C[a, c] = C[c, a] = (sa * C[a, c] + sb * C[b, c]) / (sa + sb)
            keep_all = [i for i in range(len(root)) if i != b]
            C = C[np.ix_(keep_all, keep_all)]
            sizes = np.delete(sizes, b)  # a < b, so index a is unchanged
            sizes[a] = sa + sb
            parent[root[b]] = root[a]
            root.pop(b)

        while len(root) > 1:
            rows, cols = np.triu_indices(len(root), 1)
            vals = C[rows, cols]
            below = np.flatnonzero(vals < self._cluster_thr)
            if below.size == 0:
                break
            p = below[int(np.argmin(vals[below]))]
            merge(int(rows[p]), int(cols[p]))

        while len(root) > self._max_speakers:  # fold the closest until within cap
            rows, cols = np.triu_indices(len(root), 1)
            vals = C[rows, cols]
            p = int(np.argmin(vals))
            merge(int(rows[p]), int(cols[p]))

        cluster_of_root = {r: ci for ci, r in enumerate(root)}
        out = [0] * n
        for r in range(n):
            x = r
            while parent[x] != x:  # path halving to the final cluster root
                parent[x] = parent[parent[x]]
                x = parent[x]
            out[r] = cluster_of_root[x]
        return out

    def _to_spans(
        self,
        regions: list[tuple[int, int]],
        cluster_of: list[int],
        v_times: np.ndarray,
        hop_s: float,
    ) -> list[DiarSpan]:
        # speaker label by order of first appearance
        order: dict[int, str] = {}
        for ci in cluster_of:
            if ci not in order:
                order[ci] = f"Sprecher {len(order) + 1}"

        merged: list[DiarSpan] = []
        prev_cluster: int | None = None
        for (a, b), ci in zip(regions, cluster_of):
            start_s = float(v_times[a])
            end_s = float(v_times[b]) + hop_s
            if prev_cluster == ci and merged:
                merged[-1] = DiarSpan(merged[-1].start_s, end_s, merged[-1].speaker_id)
            else:
                merged.append(DiarSpan(start_s, end_s, order[ci]))
                prev_cluster = ci
        return merged


class PyannoteDiarizationEngine(DiarizationEngine):
    """Optional pyannote-based diarization (Phase 4, **opt-in**).

    The default diarizer is the dependency-free numpy engine. pyannote is only
    used when the user explicitly sets ``diarization_backend = "pyannote"``. It is
    strictly local: the engine never downloads anything on its own -- it requires
    ``pyannote.audio`` to be installed and the chosen pipeline to already be
    present in the local HuggingFace cache (loaded with ``local_files_only``).
    When either is missing, ``is_ready()`` is False and :meth:`diarize` fails
    clearly instead of touching the network.
    """

    name = "pyannote"

    def __init__(self, model_id: str = "pyannote/speaker-diarization-3.1") -> None:
        self._model_id = model_id
        self._pipeline = None  # lazy
        self._checked = False
        self._lock = threading.Lock()

    def is_ready(self) -> bool:
        """True only if pyannote is installed AND the pipeline loads offline."""
        try:
            import pyannote.audio  # noqa: F401
        except Exception:
            return False
        if self._checked:
            return self._pipeline is not None
        with self._lock:
            # Double-checked: only one thread runs the expensive
            # Pipeline.from_pretrained; the rest wait and reuse the result (L19).
            if self._checked:
                return self._pipeline is not None
            self._checked = True
            try:
                import os
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                from pyannote.audio import Pipeline
                self._pipeline = Pipeline.from_pretrained(
                    self._model_id, use_auth_token=False)
                return True
            except Exception:
                self._pipeline = None
                return False

    def diarize(self, audio: np.ndarray, sample_rate: int) -> list[DiarSpan]:
        if self._pipeline is None and not self.is_ready():
            raise ASRError(
                "pyannote-Diarization ist opt-in und derzeit nicht verfügbar. "
                "Installiere 'pyannote.audio' und lade das Pipeline-Modell "
                f"('{self._model_id}') manuell in den lokalen HF-Cache, oder setze "
                "diarization_backend auf 'numpy' (Standard, offline). Es wurde "
                "nichts heruntergeladen."
            )
        # audio is a 1-D float32 mono array at `sample_rate` (16 kHz after decode);
        # feed pyannote an in-memory (waveform, sample_rate) pair -- no I/O.
        waveform = audio.reshape(1, -1)
        ann = self._pipeline({"audio": (waveform, sample_rate)})
        spans: list[DiarSpan] = []
        for turn, _, speaker in ann.itertracks(yield_label=True):
            spans.append(DiarSpan(float(turn.start), float(turn.end),
                                   str(speaker).strip() or "Sprecher 1"))
        spans.sort(key=lambda sp: sp.start_s)
        return spans


class MockDiarizationEngine(DiarizationEngine):
    """Deterministic engine for tests: always returns the fixed spans given."""

    name = "mock-diar"

    def __init__(self, spans: list[DiarSpan]) -> None:
        self._spans = spans

    def is_ready(self) -> bool:
        return True

    def diarize(self, audio: np.ndarray, sample_rate: int) -> list[DiarSpan]:
        return list(self._spans)
