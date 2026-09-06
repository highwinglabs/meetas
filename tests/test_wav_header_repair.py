"""Regression: WAV uploads with zeroed/wrong RIFF size fields must stay playable.

Real-world case: an uploaded original.wav had RIFF size 36 and data chunk size
0 while the PCM payload was complete.  ffmpeg tolerated it (read until EOF),
but browsers and the wave module saw zero frames, so the UI could not play the
recording and the waveform was empty."""
from __future__ import annotations

import struct
import wave

import pytest

from core.audio.assembly import repair_wav_header


def _write_wav(path, frames: int = 16000, rate: int = 16000, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * frames)


def _patch_field(path, offset: int, value: int) -> None:
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(struct.pack("<I", value))


def _data_chunk_offset(path) -> int:
    with open(path, "rb") as f:
        pos = 12
        while True:
            f.seek(pos)
            cid, size = f.read(4), int.from_bytes(f.read(4), "little")
            if cid == b"data":
                return pos
            pos += 8 + size + (size & 1)


def test_repair_fixes_zeroed_sizes(tmp_path):
    p = tmp_path / "broken.wav"
    _write_wav(p)
    _patch_field(p, _data_chunk_offset(p) + 4, 0)  # data size
    with wave.open(str(p), "rb") as w:
        assert w.getnframes() == 0  # unplayable before the repair
    _patch_field(p, 4, 36)  # RIFF size: even the container size is wrong

    assert repair_wav_header(p) is True

    with wave.open(str(p), "rb") as w:
        assert w.getnframes() == 16000
        assert w.readframes(w.getnframes()) == b"\x00\x00" * 16000


def test_repair_leaves_valid_and_foreign_files_untouched(tmp_path):
    valid = tmp_path / "valid.wav"
    _write_wav(valid)
    before = valid.read_bytes()
    assert repair_wav_header(valid) is False
    assert valid.read_bytes() == before

    foreign = tmp_path / "song.mp3"
    foreign.write_bytes(b"ID3" + b"\x00" * 64)
    assert repair_wav_header(foreign) is False
    assert foreign.read_bytes() == b"ID3" + b"\x00" * 64


def test_repair_repairs_read_only_file(tmp_path):
    p = tmp_path / "ro.wav"
    _write_wav(p)
    _patch_field(p, 4, 36)
    _patch_field(p, _data_chunk_offset(p) + 4, 0)
    p.chmod(0o444)
    assert repair_wav_header(p) is True
    with wave.open(str(p), "rb") as w:
        assert w.getnframes() == 16000
    assert oct(p.stat().st_mode & 0o777) == "0o444"


def test_import_upload_repairs_broken_wav_header(make_service, config):
    if not config.ffmpeg_available():
        pytest.skip("ffmpeg not available")
    svc = make_service()
    source = config.base_dir / "upload_tmp"
    source.mkdir(parents=True, exist_ok=True)
    wav = source / "bv-test.wav"
    _write_wav(wav, frames=32000)
    _patch_field(wav, 4, 36)
    _patch_field(wav, _data_chunk_offset(wav) + 4, 0)

    result = svc.import_upload("bv-test.wav", wav.read_bytes(), title="BV Test")
    meeting_id = result["meeting_id"]
    assert meeting_id

    wave_info = svc.audio_waveform(meeting_id, points=50)
    assert wave_info["duration_s"] == pytest.approx(2.0, abs=0.05)
    assert len(wave_info["peaks"]) > 0


def test_ffmpeg_concat_line_uses_single_quote_escaping():
    # Regression (L4): the concat list is parsed by ffmpeg, not Python, so a
    # path containing a single quote must use ffmpeg's '' escaping rather than
    # repr() (which switches to double quotes and backslash escapes).
    from core.audio.assembly import _ffmpeg_concat_line
    assert _ffmpeg_concat_line("/m/chunks/00000001.wav") == \
        "file '/m/chunks/00000001.wav'"
    assert _ffmpeg_concat_line("/m/O'Brien/x.wav") == \
        "file '/m/O''Brien/x.wav'"
