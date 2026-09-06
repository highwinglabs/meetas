"""Capture session: orchestrates an AudioSource into crash-safe chunks.

- Runs the capture in a background thread (UI stays responsive).
- Pause = stop recording (recorded audio is contiguous, no silence gaps).
  Pause spans are recorded as metadata events for the UI.
- On stop: final partial chunk is flushed, the original is assembled, and the
  session signals completion. On a hard crash the worker never flushes, so the
  next startup recovers from the index (see core.recovery).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

import numpy as np

from core.audio.assembly import assemble_original
from core.audio.chunker import ChunkWriter
from core.audio.mic_enhancer import MicEnhancer
from core.audio.stream import AudioSource
from core.audio.vad import energy_rms_dbfs, is_speaking
from core.config import Config, get_config
from core.logging_setup import get_logger

log = get_logger("ma.audio.capture")


class CaptureStatus(str, Enum):
    idle = "idle"
    recording = "recording"
    paused = "paused"
    stopped = "stopped"
    error = "error"


@dataclass
class CaptureState:
    status: CaptureStatus = CaptureStatus.idle
    duration_s: float = 0.0
    level_db: float = float("-inf")
    peak_db: float = float("-inf")
    speaking: bool = False
    chunks: int = 0
    source: str = "mic"
    device: str | None = None
    original_path: str | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)


class CaptureSession:
    def __init__(self, meeting_dir: Path, source: AudioSource,
                 sample_rate: int, channels: int, chunk_seconds: float = 1.0,
                 config: Config | None = None,
                 chunk_listener: Callable[[np.ndarray, int, float], None] | None = None,
                 frame_listener: Callable[[np.ndarray, int, float], None] | None = None,
                 source_kind: str | None = None):
        # Optional durable hook: called after each chunk is written, with
        # (chunk, sample_rate, audio_start_s). Live transcription uses the
        # frame hook below so it does not wait for durable chunk boundaries.
        self._chunk_listener = chunk_listener
        # The frame listener feeds live transcription immediately. The chunk
        # listener remains post-durable for compatibility with callers.
        self._frame_listener = frame_listener
        self._source_kind = source_kind or "mic"
        self.config = config or get_config()
        self.meeting_dir = Path(meeting_dir)
        self.source = source
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_seconds = chunk_seconds
        self.writer = ChunkWriter(self.meeting_dir, sample_rate, channels,
                                  chunk_seconds, source.name if hasattr(source, "name") else "mic")
        self._chunk_samples = max(1, int(chunk_seconds * sample_rate))
        self._buf: list[np.ndarray] = []
        self._buf_len = 0
        self._state = CaptureState(source=self.writer.source, device=self.writer.device)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._begun = False
        self._pause_start_audio = 0.0
        self._pause_start_wall = 0.0
        self._mic_enhancer = (MicEnhancer(sample_rate)
                              if self.config.mic_enhancement_enabled
                              and self._source_kind in {"mic", "both"} else None)

    # --- public API ---
    @property
    def state(self) -> CaptureState:
        with self._lock:
            self._state.duration_s = self.writer.next_start_s
            self._state.chunks = self.writer.seq
            return self._state

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Capture already started")
        self.writer.begin()
        self._begun = True
        self._state.status = CaptureStatus.recording
        self._thread = threading.Thread(target=self._worker, name="capture", daemon=True)
        self._thread.start()
        log.info("capture_started dir=%s", self.meeting_dir)

    def pause(self) -> None:
        if not self._begun:
            return
        self._paused.set()
        with self._lock:
            self._state.status = CaptureStatus.paused
            self._state.speaking = False
        self._pause_start_audio = self.writer.next_start_s
        self._pause_start_wall = time.time()
        self.writer.write_event(type="pause", audio_time_s=round(self._pause_start_audio, 3))
        log.info("capture_paused at_audio=%.3f", self._pause_start_audio)

    def resume(self) -> None:
        if not self._begun:
            return
        real = time.time() - self._pause_start_wall
        self.writer.write_event(type="resume", audio_time_s=round(self.writer.next_start_s, 3),
                                paused_real_s=round(real, 3))
        self._paused.clear()
        with self._lock:
            self._state.status = CaptureStatus.recording
        log.info("capture_resumed paused_real=%.3f", real)

    def stop(self, wait: bool = True) -> CaptureState:
        self._stop.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=30)
        return self.state

    def wait_done(self, timeout: float = 30.0) -> bool:
        return self._done.wait(timeout)

    # --- synchronous feed (used by tests / file reprocessing) ---
    def begin(self) -> None:
        """Start the writer without a background thread (deterministic mode)."""
        self.writer.begin()
        self._begun = True
        with self._lock:
            self._state.status = CaptureStatus.recording

    def feed(self, samples: np.ndarray) -> None:
        self._process(samples)

    def finish(self) -> CaptureState:
        self._finalize()
        return self.state

    # --- worker ---
    def _worker(self) -> None:
        try:
            for samples in self.source.frames():
                if self._stop.is_set():
                    break
                self._process(samples)
            self._finalize()
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("capture_worker_error")
            # Keep the durable index and in-progress marker usable for startup
            # recovery, but never leave file descriptors open after a source
            # or chunk-write failure.
            try:
                self.writer.close()
            except Exception:
                log.debug("chunk_writer_close_failed", exc_info=True)
            with self._lock:
                self._state.status = CaptureStatus.error
                self._state.error = str(exc)
            self._done.set()
        finally:
            # ``frames()`` normally closes the source itself, but an error
            # during source initialisation can happen before its generator
            # enters that ``finally`` block.  Closing here makes failed starts
            # release PortAudio handles as well and is idempotent for sources
            # that already cleaned themselves up.
            try:
                self.source.close()
            except Exception:  # pragma: no cover - defensive
                log.debug("audio_source_close_failed", exc_info=True)

    def _process(self, samples: np.ndarray) -> None:
        if self._paused.is_set():
            with self._lock:
                self._state.level_db = float("-inf")
                self._state.speaking = False
            return
        # Keep the durable recording untouched. The cleaned signal is only
        # used for live transcription/meters; archival enhancement creates a
        # separate copy after stop.
        raw_samples = np.ascontiguousarray(samples, dtype=np.float32)
        if raw_samples.ndim == 1:
            raw_samples = raw_samples.reshape(-1, 1)
        samples = raw_samples
        if self._mic_enhancer is not None:
            samples = self._mic_enhancer.process(
                samples, mic_channels=1 if self._source_kind == "both" else None)
        frame_start_s = self.writer.next_start_s + self._buf_len / self.sample_rate
        if self._frame_listener is not None:
            try:
                self._frame_listener(samples, self.sample_rate, frame_start_s)
            except Exception:
                log.exception("frame_listener_error")
        with self._lock:
            db = energy_rms_dbfs(samples)
            self._state.level_db = db
            self._state.peak_db = max(self._state.peak_db, db)
            self._state.speaking = is_speaking(samples)
        self._buf.append(raw_samples)
        self._buf_len += raw_samples.shape[0]
        while self._buf_len >= self._chunk_samples:
            chunk = self._drain_chunk()
            start_s = self.writer.next_start_s
            self.writer.write_chunk(chunk)
            if self._chunk_listener is not None:
                try:
                    self._chunk_listener(chunk, self.sample_rate, start_s)
                except Exception:
                    log.exception("chunk_listener_error")

    def _drain_chunk(self) -> np.ndarray:
        parts: list[np.ndarray] = []
        need = self._chunk_samples
        while need > 0 and self._buf:
            head = self._buf[0]
            if head.shape[0] <= need:
                parts.append(head)
                need -= head.shape[0]
                self._buf_len -= head.shape[0]
                self._buf.pop(0)
            else:
                parts.append(head[:need])
                self._buf[0] = head[need:]
                self._buf_len -= need
                need = 0
        return np.vstack(parts)

    def _finalize(self) -> None:
        # flush the final partial chunk (clean stop only; a crash skips this)
        if self._buf_len > 0 and self._buf:
            chunk = np.vstack(self._buf)
            # The listener is fed for every durable chunk, including the final
            # short one.  Omitting this block leaves a gap in the live preview
            # at stop time even though the authoritative recording is intact.
            start_s = self.writer.next_start_s
            self.writer.write_chunk(chunk)
            if self._chunk_listener is not None:
                try:
                    self._chunk_listener(chunk, self.sample_rate, start_s)
                except Exception:
                    log.exception("chunk_listener_error")
            self._buf = []
            self._buf_len = 0
        self.writer.write_event(type="stop", audio_time_s=round(self.writer.next_start_s, 3))
        self.writer.close()
        original = None
        try:
            original = assemble_original(self.meeting_dir, self.config)
            self.writer.clear_in_progress()
            with self._lock:
                self._state.status = CaptureStatus.stopped
                self._state.original_path = str(original)
        except Exception as exc:
            log.exception("finalize_failed")
            with self._lock:
                self._state.status = CaptureStatus.error
                self._state.error = str(exc)
        self._done.set()
