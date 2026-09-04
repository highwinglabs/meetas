"""Crash-safe chunk writer.

Every 1-second chunk is written as a complete WAV file and an index line is
appended + fsynced. The index is the single source of truth: a torn/partial
file that has no index line is simply dropped during recovery (bounded loss
<= 1 second). The original is assembled later from indexed chunks only.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from core.logging_setup import get_logger

log = get_logger("ma.audio.chunker")


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int, channels: int) -> int:
    """Write float32 (n, channels) [-1,1] as 16-bit PCM WAV. Returns byte size."""
    samples = np.ascontiguousarray(samples, dtype=np.float32)
    if samples.ndim == 1:
        samples = samples.reshape(-1, 1)
    n, ch = samples.shape
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path.stat().st_size


class ChunkWriter:
    def __init__(self, meeting_dir: Path, sample_rate: int, channels: int,
                 chunk_seconds: float = 1.0, source: str = "mic", device: str | None = None):
        self.meeting_dir = Path(meeting_dir)
        self.chunk_dir = self.meeting_dir / "chunks"
        self.index_path = self.meeting_dir / "chunks.index.jsonl"
        self.events_path = self.meeting_dir / "events.jsonl"
        self.meta_path = self.meeting_dir / "meta.json"
        self.in_progress_marker = self.meeting_dir / "in_progress"
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_seconds = chunk_seconds
        self.source = source
        self.device = device
        self.seq = 0
        self.next_start_s = 0.0
        self._index_file = None
        self._events_file = None

    # --- lifecycle ---
    def begin(self) -> None:
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        for directory in (self.meeting_dir, self.chunk_dir):
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        meta = {
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "chunk_seconds": self.chunk_seconds,
            "source": self.source,
            "device": self.device,
            "created": datetime.now(timezone.utc).isoformat(),
        }
        self.meta_path.write_text(json.dumps(meta, indent=2))
        self.in_progress_marker.write_text(json.dumps(meta))
        for p in (self.meta_path, self.in_progress_marker):
            try:
                p.chmod(0o600)
            except OSError:
                pass
        self._index_file = open(self.index_path, "a", encoding="utf-8")
        self._events_file = open(self.events_path, "a", encoding="utf-8")
        for p in (self.index_path, self.events_path):
            try:
                p.chmod(0o600)
            except OSError:
                pass
        log.info("chunk_writer_started dir=%s sr=%s ch=%s", self.meeting_dir,
                 self.sample_rate, self.channels)

    def write_chunk(self, samples: np.ndarray) -> dict:
        start_s = self.next_start_s
        samples = np.ascontiguousarray(samples, dtype=np.float32)
        if samples.ndim == 1:
            samples = samples.reshape(-1, 1)
        n = samples.shape[0]
        dur_s = n / self.sample_rate
        file_name = f"chunk_{self.seq:06d}.wav"
        wav_path = self.chunk_dir / file_name
        size = _write_wav(wav_path, samples, self.sample_rate, self.channels)
        digest = hashlib.sha256(wav_path.read_bytes()).hexdigest()
        entry = {
            "seq": self.seq,
            "start_s": round(start_s, 6),
            "dur_s": round(dur_s, 6),
            "file": file_name,
            "bytes": size,
            "sha256": digest,
        }
        self._append_index(entry)
        self.next_start_s += dur_s
        self.seq += 1
        return entry

    def write_event(self, **fields) -> None:
        if self._events_file is None:
            return
        line = dict(fields)
        line["at"] = datetime.now(timezone.utc).isoformat()
        self._events_file.write(json.dumps(line) + "\n")
        self._events_file.flush()
        os.fsync(self._events_file.fileno())

    def close(self) -> None:
        for fh in (self._index_file, self._events_file):
            if fh is not None:
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                    fh.close()
                except Exception:
                    pass
        self._index_file = None
        self._events_file = None

    def _append_index(self, entry: dict) -> None:
        if self._index_file is None:
            raise RuntimeError("ChunkWriter not started (call begin first)")
        self._index_file.write(json.dumps(entry) + "\n")
        self._index_file.flush()
        os.fsync(self._index_file.fileno())

    # --- read back ---
    @property
    def is_in_progress(self) -> bool:
        return self.in_progress_marker.exists()

    def read_index(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        entries = []
        with open(self.index_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    # a torn last line (crash mid-write) is ignored
                    continue
        return entries

    def read_events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        out = []
        with open(self.events_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def clear_in_progress(self) -> None:
        if self.in_progress_marker.exists():
            self.in_progress_marker.unlink()

    def total_duration_s(self) -> float:
        return sum(e["dur_s"] for e in self.read_index())
