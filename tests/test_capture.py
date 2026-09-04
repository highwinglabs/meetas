"""Capture lifecycle: deterministic (synchronous) and threaded."""
from __future__ import annotations

import numpy as np

from core.audio.capture import CaptureSession, CaptureStatus
from core.audio.stream import SyntheticSource
from core.config import get_config


def _session(tmp_path, duration_s=2.0):
    src = SyntheticSource(duration_s, 16000, 1, block_seconds=0.1)
    return CaptureSession(tmp_path / "m", src, 16000, 1, 1.0, get_config())


def test_synchronous_capture_produces_original(tmp_path):
    s = _session(tmp_path, 2.0)
    s.begin()
    for block in SyntheticSource(2.0, 16000, 1, block_seconds=0.1).frames():
        s.feed(block)
    state = s.finish()
    assert state.status == CaptureStatus.stopped
    assert state.original_path is not None
    assert abs(state.duration_s - 2.0) < 0.05
    assert state.chunks == 2


def test_pause_discards_audio_and_records_events(tmp_path):
    s = _session(tmp_path, 2.0)
    s.begin()
    blocks = list(SyntheticSource(2.0, 16000, 1, block_seconds=0.1).frames())  # 20 blocks
    for b in blocks[:10]:  # 1.0s recorded
        s.feed(b)
    s.pause()
    for b in blocks[10:15]:  # 0.5s discarded (paused)
        s.feed(b)
    s.resume()
    for b in blocks[15:20]:  # 0.5s recorded
        s.feed(b)
    state = s.finish()
    # recorded ~1.5s (1.0 + 0.5), NOT 2.0
    assert 1.3 < state.duration_s < 1.8
    events = s.writer.read_events()
    types = [e["type"] for e in events]
    assert "pause" in types
    assert "resume" in types
    assert "stop" in types
    assert state.status == CaptureStatus.stopped


def test_threaded_capture_runs_to_completion(tmp_path):
    s = _session(tmp_path, 1.5)
    s.start()
    assert s.wait_done(timeout=10) is True
    state = s.state
    assert state.status == CaptureStatus.stopped
    assert state.original_path is not None
    from pathlib import Path
    assert Path(state.original_path).exists()


def test_partial_last_chunk_is_flushed(tmp_path):
    # 1.3s -> 1 full chunk + 0.3s partial chunk
    s = _session(tmp_path, 1.3)
    s.begin()
    for block in SyntheticSource(1.3, 16000, 1, block_seconds=0.1).frames():
        s.feed(block)
    state = s.finish()
    assert state.chunks == 2
    entries = s.writer.read_index()
    assert entries[1]["dur_s"] < 1.0
    assert abs(sum(e["dur_s"] for e in entries) - 1.3) < 0.05


def test_partial_last_chunk_reaches_live_listener(tmp_path):
    received = []
    src = SyntheticSource(1.3, 16000, 1, block_seconds=0.1)
    s = CaptureSession(tmp_path / "m", src, 16000, 1, 1.0, get_config(),
                       chunk_listener=lambda chunk, rate, start: received.append(
                           (len(chunk) / rate, start)))
    s.begin()
    for block in src.frames():
        s.feed(block)
    s.finish()
    assert len(received) == 2
    assert abs(sum(duration for duration, _ in received) - 1.3) < 0.05
    assert received[1][0] < 1.0


def test_frame_listener_receives_blocks_before_chunk_boundary(tmp_path):
    received = []
    src = SyntheticSource(0.3, 16000, 1, block_seconds=0.1)
    s = CaptureSession(tmp_path / "m", src, 16000, 1, 1.0, get_config(),
                       frame_listener=lambda chunk, rate, start: received.append(
                           (len(chunk) / rate, start)))
    s.begin()
    for block in src.frames():
        s.feed(block)
    s.finish()
    assert len(received) == 3
    assert received[0][1] == 0.0
    assert received[-1][1] > received[0][1]


def test_mic_enhancement_can_be_disabled(config, tmp_path):
    config.mic_enhancement_enabled = False
    received = []
    src = SyntheticSource(0.1, 16000, 1, block_seconds=0.1)
    s = CaptureSession(tmp_path / "m", src, 16000, 1, 1.0, config,
                       frame_listener=lambda chunk, rate, start: received.append(chunk))
    s.begin()
    for block in src.frames():
        s.feed(block)
    s.finish()
    assert len(received) == 1
