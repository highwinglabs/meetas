"""Phase 2: sample-rate fallback + optional system-audio capture flag.

The rate-walking is pure and deterministic (no PortAudio needed). The
system-audio path is gated: disabled by default, and it reports itself
unavailable instead of crashing when no loopback device exists."""
from __future__ import annotations

import pytest

import core.service as svc_mod
from core.audio.devices import resolve_system_audio_device
from core.audio.stream import LiveSource, try_rates


def test_try_rates_returns_first_working():
    seen = []

    def probe(rate):
        seen.append(rate)
        if rate != 44100:
            raise RuntimeError("device rejects 16 kHz")

    out = try_rates([16000, 48000, 44100], probe)
    assert out == 44100
    assert seen == [16000, 48000, 44100]


def test_try_rates_accepts_first():
    out = try_rates([16000, 48000], lambda r: None)
    assert out == 16000


def test_try_rates_raises_when_all_fail():
    def probe(rate):
        raise RuntimeError(f"reject {rate}")

    with pytest.raises(RuntimeError) as ei:
        try_rates([16000, 48000], probe)
    msg = str(ei.value)
    assert "16000" in msg and "48000" in msg


def test_try_rates_empty_raises():
    with pytest.raises(RuntimeError):
        try_rates([], lambda r: None)


def test_livesource_candidate_rates_dedup():
    src = LiveSource(None, 16000, 1, 50, rate_fallback=[16000, 48000, 44100])
    assert src._candidate_rates() == [16000, 48000, 44100]
    src2 = LiveSource(None, 48000, 1, 50, rate_fallback=[48000, 16000])
    assert src2._candidate_rates() == [48000, 16000]


def test_system_audio_resolution_never_raises():
    out = resolve_system_audio_device()
    assert out is None or isinstance(out, int)


def test_system_audio_disabled_raises(make_service, config):
    config.system_audio_enabled = False
    svc = make_service()
    with pytest.raises(ValueError):
        svc.start_meeting(title="S", source="system")


def test_system_audio_no_device_raises_gracefully(make_service, config, monkeypatch):
    config.system_audio_enabled = True
    # deterministic: no loopback device in the test env
    monkeypatch.setattr(svc_mod, "resolve_system_audio_device", lambda: None)
    svc = make_service()
    with pytest.raises(ValueError):
        svc.start_meeting(title="S", source="system")
