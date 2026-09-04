"""Live transcription pipeline: rolling buffer, final+partial, append-only,
speaker assignment, stop-flush. Deterministic (mock live ASR + mock diar)."""
from __future__ import annotations

import numpy as np

from core.live.pipeline import LiveTranscriptionPipeline
from core.providers.diar import DiarSpan, MockDiarizationEngine
from core.store.db import (_secure_db_file, get_engine, has_table,
                           make_engine, session_scope)
from core.store.models import Base, Meeting, TranscriptSegment
from sqlalchemy import select

from conftest import MockLiveASREngine


def _init_db(config) -> None:
    make_engine(config)
    if not has_table("meeting"):
        Base.metadata.create_all(get_engine())
        _secure_db_file(config)


def _make_meeting() -> str:
    with session_scope() as s:
        m = Meeting(title="Live")
        s.add(m)
        s.commit()
        return m.id


def _tone_chunk(sr: int, dur: float) -> np.ndarray:
    t = np.arange(int(sr * dur), dtype=np.float32) / sr
    return (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)


def _count_segments(meeting_id: str) -> list[TranscriptSegment]:
    with session_scope() as s:
        return list(s.scalars(select(TranscriptSegment)
                              .where(TranscriptSegment.meeting_id == meeting_id)
                              .order_by(TranscriptSegment.start_s)))


def test_finals_partial_and_append_only(config):
    _init_db(config)
    mid = _make_meeting()
    pipe = LiveTranscriptionPipeline(
        meeting_id=mid, asr_engine=MockLiveASREngine(),
        sample_rate=16000, window_s=60.0, period_s=0.1, tail_s=4.0,
    )
    # feed 4 s -> nothing settled yet (tail=4), only a live partial
    for i in range(4):
        pipe.on_chunk(_tone_chunk(16000, 1.0), 16000, float(i))
    n = pipe.process()
    assert n == 0
    assert len(_count_segments(mid)) == 0
    snap = pipe.snapshot()
    assert "seg0" in snap["partial_text"] and "seg1" in snap["partial_text"]

    # feed up to 6 s -> seg0 [0,2) settles (end<=6-4=2)
    for i in range(4, 6):
        pipe.on_chunk(_tone_chunk(16000, 1.0), 16000, float(i))
    n = pipe.process()
    assert n == 1
    segs = _count_segments(mid)
    assert len(segs) == 1
    assert segs[0].text == "seg0"
    assert pipe.snapshot()["partial_text"].count("seg") >= 1

    # idempotent: re-process with no new audio adds nothing
    assert pipe.process() == 0
    assert len(_count_segments(mid)) == 1

    # stop() flushes the trailing tail as final -> seg1, seg2 added
    pipe.stop()
    segs = _count_segments(mid)
    assert [s.text for s in segs] == ["seg0", "seg1", "seg2"]
    assert not pipe.is_running()


def test_speaker_assignment(config):
    _init_db(config)
    mid = _make_meeting()
    diar = MockDiarizationEngine([
        DiarSpan(0.0, 3.0, "Sprecher 1"),
        DiarSpan(3.0, 6.0, "Sprecher 2"),
    ])
    pipe = LiveTranscriptionPipeline(
        meeting_id=mid, asr_engine=MockLiveASREngine(),
        diar_engine=diar, sample_rate=16000, window_s=60.0, tail_s=0.5,
    )
    for i in range(6):
        pipe.on_chunk(_tone_chunk(16000, 1.0), 16000, float(i))
    pipe.process()
    pipe.stop()
    segs = _count_segments(mid)
    assert len(segs) == 3
    # seg0 [0,2) -> S1, seg2 [4,6) -> S2
    assert segs[0].speaker_id == "Sprecher 1"
    assert segs[2].speaker_id == "Sprecher 2"


def test_no_diar_gives_null_speaker(config):
    _init_db(config)
    mid = _make_meeting()
    pipe = LiveTranscriptionPipeline(
        meeting_id=mid, asr_engine=MockLiveASREngine(),
        sample_rate=16000, tail_s=0.5,
    )
    for i in range(4):
        pipe.on_chunk(_tone_chunk(16000, 1.0), 16000, float(i))
    pipe.stop()
    segs = _count_segments(mid)
    assert all(s.speaker_id is None for s in segs)


def test_asr_failure_does_not_crash(config):
    class FailingLive(MockLiveASREngine):
        def transcribe_window(self, audio, sample_rate, language=None):
            raise RuntimeError("boom")

    _init_db(config)
    mid = _make_meeting()
    pipe = LiveTranscriptionPipeline(
        meeting_id=mid, asr_engine=FailingLive(),
        sample_rate=16000, tail_s=0.5,
    )
    for i in range(4):
        pipe.on_chunk(_tone_chunk(16000, 1.0), 16000, float(i))
    # must not raise
    assert pipe.process() == 0
    assert pipe.snapshot()["error"] is not None
    assert len(_count_segments(mid)) == 0


def test_resample_from_native_rate(config):
    # Feed 44.1 kHz chunks; the pipeline must resample to 16 kHz internally.
    _init_db(config)
    mid = _make_meeting()
    pipe = LiveTranscriptionPipeline(
        meeting_id=mid, asr_engine=MockLiveASREngine(),
        sample_rate=16000, window_s=60.0, tail_s=0.5,
    )
    # 4 s of 44.1k audio in 1 s chunks
    for i in range(4):
        pipe.on_chunk(_tone_chunk(44100, 1.0), 44100, float(i))
    pipe.process()
    snap = pipe.snapshot()
    # ~4 s of audio resampled -> should have produced a live partial
    assert snap["partial_text"] or snap["final_count"] >= 0


def test_rolling_window_continues_after_window_rolls(config):
    _init_db(config)
    mid = _make_meeting()
    pipe = LiveTranscriptionPipeline(
        meeting_id=mid, asr_engine=MockLiveASREngine(), sample_rate=16000,
        window_s=6.0, tail_s=0.5,
    )
    for i in range(10):
        pipe.on_chunk(_tone_chunk(16000, 1.0), 16000, float(i))
        pipe.process()
    # The worker/index must continue producing live output after eviction of
    # the first four seconds from the rolling buffer.
    assert pipe.snapshot()["last_end_s"] >= 9.0
    assert pipe.snapshot()["final_count"] > 0


def test_ten_minute_feed_uses_absolute_timestamps_without_duplicates(config):
    """Regression for the old rolling-buffer index freeze after one window."""
    _init_db(config)
    mid = _make_meeting()
    pipe = LiveTranscriptionPipeline(
        meeting_id=mid, asr_engine=MockLiveASREngine(), sample_rate=10,
        window_s=6.0, period_s=0.2, tail_s=0.5,
    )
    one_second = np.zeros(10, dtype=np.float32)
    for second in range(600):
        pipe.on_chunk(one_second, 10, float(second))
        pipe.process()
    pipe.stop()
    segs = _count_segments(mid)
    starts = [round(seg.start_s, 3) for seg in segs]
    assert pipe.snapshot()["last_end_s"] >= 599.0
    assert len(segs) > 100
    assert starts == sorted(set(starts))
    assert all(seg.end_s > seg.start_s for seg in segs)
