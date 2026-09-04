"""Rolling-window live transcription pipeline (Phase 5).

Design (local, CPU-friendly, no streaming model):

* A bounded rolling buffer of 16 kHz mono audio is appended as capture chunks
  arrive (resampled on the fly).
* A background thread re-transcribes the buffer every `period_s` using a
  (batch) ASR engine. The part older than `tail_s` is considered settled and is
  written to the database as *final* segments; the trailing `tail_s` is exposed
  as a live *partial* (not persisted) for the UI.
* Writes are append-only and idempotent: only segments beyond the already
  finalized boundary are inserted, so re-transcribing an overlapping window
  never duplicates rows. The authoritative transcript is still produced by the
  batch `transcribe()` at the end of the meeting.
* When a diarization engine is provided, each finalized segment (and the live
  partial) is labelled with a speaker by temporal overlap.

The pipeline is opt-in; with it disabled none of this code runs.
"""
from __future__ import annotations

import threading
import math
import time

import numpy as np

from core.audio.resample import StreamingLinearResampler
from core.live.engine import LiveASREngine
from core.logging_setup import get_logger
from core.providers.base import ASRSegment
from core.providers.diar import DiarSpan, DiarizationEngine
from core.store.db import session_scope
from core.store.fts import reindex_meeting
from core.store.models import TranscriptSegment

log = get_logger("ma.live")


class LiveTranscriptionPipeline:
    def __init__(
        self,
        *,
        meeting_id: str,
        asr_engine: LiveASREngine,
        diar_engine: DiarizationEngine | None = None,
        sample_rate: int = 16000,
        window_s: float = 60.0,
        period_s: float = 4.0,
        tail_s: float = 4.0,
        language: str | None = None,
    ) -> None:
        self._meeting_id = meeting_id
        self._asr = asr_engine
        self._diar = diar_engine
        self._sr = int(sample_rate)
        self._max_samples = max(1, int(window_s * self._sr))
        self._period = max(0.2, float(period_s))
        self._tail = max(0.5, float(tail_s))
        self._language = language
        self._resamplers: dict[int, StreamingLinearResampler] = {}

        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start_s = 0.0
        # Absolute audio timestamp of the newest sample already considered by
        # the worker. This must not be an index into the rolling buffer: that
        # index becomes invalid as soon as the left side is evicted.
        self._processed_end_s = 0.0
        self._audio_end_s = 0.0
        self._written_end_s = -1.0
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Serialises the check-and-set of _processing so the worker thread and
        # an API stop() call can never both drive process() concurrently.
        self._process_lock = threading.Lock()

        self._partial = ""
        self._partial_speaker: str | None = None
        self._last_end_s: float = 0.0
        self._final_count = 0
        self._error: str | None = None
        # The worker advances its absolute cursor only after a successful ASR
        # pass.  On a transient provider error the same audio must be retried;
        # otherwise the live transcript would contain a permanent gap.
        self._last_process_ok = True

    # -- feeding -----------------------------------------------------------
    def on_chunk(self, chunk: np.ndarray, sample_rate: int, start_s: float) -> None:
        """Append one capture chunk (native rate) to the rolling 16 kHz buffer."""
        if chunk is None or len(chunk) == 0:
            return
        mono = chunk.mean(axis=1) if chunk.ndim > 1 else chunk
        rate = int(sample_rate)
        resampler = self._resamplers.get(rate)
        if resampler is None:
            resampler = StreamingLinearResampler(rate, self._sr)
            self._resamplers[rate] = resampler
        res = resampler.process(mono)
        with self._cv:
            if len(self._buf) == 0 and self._audio_end_s == 0.0:
                self._buf_start_s = max(0.0, float(start_s))
            self._buf = np.concatenate([self._buf, res]) if len(self._buf) else res
            self._audio_end_s = max(self._audio_end_s,
                                    float(start_s) + len(res) / self._sr)
            excess = len(self._buf) - self._max_samples
            if excess > 0:
                self._buf = self._buf[excess:]
                self._buf_start_s += excess / self._sr
                if self._written_end_s < self._buf_start_s:
                    # dropped audio can no longer be finalized live; the batch
                    # transcribe at the end is authoritative for the full audio.
                    self._written_end_s = self._buf_start_s
            self._cv.notify_all()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name=f"live-{self._meeting_id[:8]}")
        self._thread.start()

    def stop(self) -> dict:
        """Stop the worker and flush the trailing partial as a final segment."""
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._process_final()
        self._finalize_jobs()
        return self.snapshot()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _worker(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            with self._cv:
                self._cv.wait(timeout=self._period)
                if self._stop.is_set():
                    break
                target_end_s = self._audio_end_s
                new_audio_s = target_end_s - self._processed_end_s
            now = time.monotonic()
            if (now - last) >= self._period and new_audio_s >= 1.0:
                try:
                    self.process()
                except Exception as exc:  # a live preview must not kill capture
                    log.warning("live_process_failed meeting=%s: %s",
                                self._meeting_id[:8], exc, exc_info=True)
                    with self._lock:
                        self._error = str(exc)
                with self._lock:
                    if self._last_process_ok:
                        # Do not include chunks appended while ASR was running;
                        # they belong to the next pass.
                        self._processed_end_s = max(self._processed_end_s, target_end_s)
                last = now

    # -- core --------------------------------------------------------------
    def process(self, finalize_all: bool = False) -> int:
        """Run one transcription pass and wait for an in-flight pass.

        Serialising callers is important for the worker's progress cursor: a
        non-blocking ``0`` return can otherwise be mistaken for a successful
        pass and advance the cursor past audio that was never transcribed.
        """
        with self._process_lock:
            return self._process_unlocked(finalize_all)

    def _process_final(self) -> int:
        """Blocking final pass (used by stop) to flush the trailing partial;
        waits for any in-flight periodic pass to finish first."""
        with self._process_lock:
            return self._process_unlocked(finalize_all=True)

    def _process_unlocked(self, finalize_all: bool = False) -> int:
        """Run one transcription pass. Returns the number of newly finalized
        segments written to the database (0 if nothing new)."""
        with self._lock:
            buf = self._buf.copy()
            buf_start = self._buf_start_s

        if len(buf) < self._sr:  # < 1 s of audio: not enough to transcribe
            return 0

        with self._lock:
            self._last_process_ok = False
        try:
            segs = self._asr.transcribe_window(buf, self._sr, self._language)
        except Exception as exc:  # live must never take down capture
            log.warning("live_transcribe_failed meeting=%s: %s", self._meeting_id[:8], exc)
            with self._lock:
                self._error = str(exc)
            return 0

        valid_segs = []
        for s in segs:
            try:
                start_s = float(s.start_s) + buf_start
                end_s = float(s.end_s) + buf_start
            except (TypeError, ValueError):
                continue
            if (not math.isfinite(start_s) or not math.isfinite(end_s)
                    or start_s < 0 or end_s < start_s
                    or not isinstance(s.text, str) or not s.text.strip()):
                continue
            s.start_s = round(start_s, 3)
            s.end_s = round(end_s, 3)
            valid_segs.append(s)
        segs = valid_segs

        now_s = buf_start + len(buf) / self._sr
        final_limit = now_s if finalize_all else (now_s - self._tail)
        finals = [s for s in segs if s.end_s <= final_limit + 1e-3]
        finals.sort(key=lambda s: s.start_s)

        with self._lock:
            new = [s for s in finals if s.end_s > self._written_end_s + 1e-3]
        if new:
            speakers = self._assign_speakers(new, buf, buf_start)
            self._write_finals(new, speakers)
            with self._lock:
                self._written_end_s = max(self._written_end_s, max(s.end_s for s in new))
                self._final_count += len(new)

        # live partial = trailing tail not yet settled
        partials = [s for s in segs
                    if s.end_s > final_limit - 1e-3 and s.start_s < now_s + 1e-3]
        partials.sort(key=lambda s: s.start_s)
        partial_text = " ".join(s.text for s in partials)
        partial_speaker: str | None = None
        if partials and self._diar is not None:
            spans = self._offset_spans(buf, buf_start)
            if spans:
                ids = self._diar.assign(partials, spans)
                partial_speaker = ids[-1]

        with self._lock:
            self._partial = partial_text
            self._partial_speaker = partial_speaker
            self._last_end_s = now_s if partials else self._written_end_s
            self._error = None
            self._last_process_ok = True

        return len(new)

    def _assign_speakers(self, segs: list[ASRSegment], buf: np.ndarray,
                         buf_start: float) -> list[str | None]:
        if self._diar is None or not segs:
            return [None] * len(segs)
        spans = self._offset_spans(buf, buf_start)
        if not spans:
            return [None] * len(segs)
        return self._diar.assign(segs, spans)

    def _offset_spans(self, buf: np.ndarray, buf_start: float) -> list[DiarSpan]:
        if self._diar is None or len(buf) == 0:
            return []
        raw = self._diar.diarize(buf, self._sr)
        return [DiarSpan(sp.start_s + buf_start, sp.end_s + buf_start, sp.speaker_id)
                for sp in raw]

    def _write_finals(self, segs: list[ASRSegment],
                      speakers: list[str | None]) -> None:
        # NOTE: the live path only appends segments; it does NOT drive the
        # diarize job (leaving it "running" here would strand it after the
        # meeting ends). Job finalisation happens once in stop().
        with session_scope() as s:
            for seg, sp in zip(segs, speakers):
                s.add(TranscriptSegment(
                    meeting_id=self._meeting_id,
                    start_s=seg.start_s, end_s=seg.end_s,
                    text=seg.text, raw_text=seg.raw_text or seg.text,
                    language=seg.language, confidence=seg.confidence,
                    audio_ref=None, speaker_id=sp, status="bestaetigt"))
            reindex_meeting(s, self._meeting_id)
            s.commit()

    def _finalize_jobs(self) -> None:
        """Keep post-stop jobs pending for the authoritative batch pipeline.

        Live rows are only a preview.  Marking ``transcribe`` done here would
        make the queue skip the selected quality model after a live meeting.
        The rows are intentionally replaced by :class:`TranscriptionProcessor`
        once the recording has been assembled.  ``stop`` still calls this
        method for compatibility with older callers, but it performs no DB
        mutation now.
        """
        return

    # -- observability -----------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": True,
                "engine": self._asr.name,
                "final_count": self._final_count,
                "partial_text": self._partial,
                "partial_speaker": self._partial_speaker,
                "last_end_s": round(self._last_end_s, 3),
                "error": self._error,
            }
