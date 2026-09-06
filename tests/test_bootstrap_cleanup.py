"""L22: bootstrap removes orphaned preview-*.wav files older than one hour."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from core.bootstrap import _STALE_PREVIEW_S, _cleanup_stale_previews
from core.config import Config


def _put(audio_dir: Path, name: str, age_s: float) -> Path:
    meeting_dir = audio_dir / "some-meeting"
    meeting_dir.mkdir(parents=True, exist_ok=True)
    p = meeting_dir / name
    p.write_bytes(b"RIFF" + b"0" * 64)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def test_cleanup_removes_stale_keeps_fresh_and_ignores_originals(config):
    stale = _put(config.audio_dir, "preview-stale.wav", _STALE_PREVIEW_S + 600)
    fresh = _put(config.audio_dir, "preview-fresh.wav", 60)
    # An original recording that is just as old is NOT a preview -> untouched.
    meeting_dir = config.audio_dir / "some-meeting"
    original = meeting_dir / "original_16k.wav"
    original.write_bytes(b"RIFF")
    t = time.time() - (_STALE_PREVIEW_S + 600)
    os.utime(original, (t, t))

    removed = _cleanup_stale_previews(config)
    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()          # too recent -> kept
    assert original.exists()       # not a preview -> kept


def test_cleanup_missing_audio_dir_is_safe():
    # No crash (and no removal) when the audio dir does not exist yet.
    cfg = Config(base_dir=Path(tempfile.mkdtemp()))
    assert _cleanup_stale_previews(cfg) == 0
