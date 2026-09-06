"""Microphone cleanup is stateful, conservative and leaves system audio alone."""
from __future__ import annotations

import numpy as np
import math
import shutil
import wave
import pytest

from core.audio.mic_enhancer import MicEnhancer
from core.audio.enhancement import (
    _filter_chain, default_audio_profiles, detect_noise_profile, enhance_audio,
    pcm_channel_values,
)


def test_limiter_prevents_clipping():
    enhancer = MicEnhancer(16000)
    out = enhancer.process(np.full(160, 1.4, dtype=np.float32))
    assert out.shape == (160,)
    assert np.max(np.abs(out)) <= 0.97 + 1e-6


def test_combined_audio_only_processes_mic_channel():
    enhancer = MicEnhancer(16000)
    mic = np.full(160, 1.4, dtype=np.float32)
    system = np.full(160, 0.4, dtype=np.float32)
    out = enhancer.process(np.column_stack((mic, system)), mic_channels=1)
    assert np.max(np.abs(out[:, 0])) <= 0.97 + 1e-6
    assert np.array_equal(out[:, 1], system)


def test_high_pass_state_continues_across_blocks():
    enhancer = MicEnhancer(16000, high_pass_hz=80)
    first = enhancer.process(np.ones(160, dtype=np.float32))
    second = enhancer.process(np.ones(160, dtype=np.float32))
    assert first[-1] > 0
    assert abs(second[-1]) < abs(first[0])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg nicht installiert")
def test_archival_enhancement_creates_separate_readonly_copy(tmp_path):
    rate = 16000
    t = np.arange(rate * 3, dtype=np.float32) / rate
    audio = 0.02 * np.sin(2 * math.pi * 120 * t) + 0.18 * np.sin(2 * math.pi * 440 * t)
    original = tmp_path / "original.wav"
    enhanced = tmp_path / "enhanced.wav"
    with wave.open(str(original), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((audio * 32767).astype("<i2").tobytes())

    before = original.read_bytes()
    result = enhance_audio(original, enhanced, default_audio_profiles()["meeting"])

    assert result["status"] == "ready"
    assert original.read_bytes() == before
    assert enhanced.is_file()
    assert enhanced.stat().st_mode & 0o222 == 0
    with wave.open(str(enhanced), "rb") as wav:
        assert (wav.getnchannels(), wav.getframerate(), wav.getnframes()) == (1, rate, 3 * rate)
    assert enhanced.read_bytes() == before


def test_manual_noise_profile_uses_ffmpeg_spectral_sampling():
    profile = default_audio_profiles()["meeting"]
    profile["noise_reduction_enabled"] = True
    chain = _filter_chain(profile, -45.0, 0.0, 1.5)
    assert "asendcmd=0 afftdn@denoise sn start" in chain
    assert "asendcmd=1.5 afftdn@denoise sn stop" in chain
    assert "afftdn@denoise" in chain


def test_voice_effect_chain_contains_expander_deesser_and_separate_true_peak():
    profile = default_audio_profiles()["meeting"]
    profile.update(
        gate_enabled=True,
        deesser_enabled=True,
        compressor_enabled=True,
        loudness_enabled=True,
        gate_knee=4.0,
        presence_enabled=True,
        presence_q=1.2,
        compressor_knee=3.0,
        compressor_makeup_db=2.0,
        loudness_true_peak_db=-1.5,
    )
    chain = _filter_chain(profile, -45.0)
    assert "agate=" in chain and "knee=4" in chain
    assert "deesser=i=0.35:m=0.5:f=0.5" in chain
    assert "equalizer=f=3000:t=q:w=1.2" in chain
    assert "acompressor=" in chain and "makeup=1.259" in chain
    assert "loudnorm=I=-19:TP=-1.5:LRA=7" in chain


def test_pcm_channel_values_supports_24_bit_samples():
    samples = np.array([-8388608, -1, 0, 8388607], dtype=np.int32)
    packed = bytearray()
    for sample in samples:
        value = int(sample) & 0xFFFFFF
        packed.extend((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF))
    decoded = pcm_channel_values(bytes(packed), 3, 1)
    assert np.allclose(decoded, samples / 8388608.0)


def test_noise_profile_ignores_digital_silence_and_finds_quiet_window(tmp_path):
    rate = 1000
    quiet = np.full(rate * 2, 0.003, dtype=np.float32)
    loud = np.full(rate * 2, 0.2, dtype=np.float32)
    original = tmp_path / "noise.wav"
    with wave.open(str(original), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.zeros(rate * 2, dtype="<i2").tobytes())
        wav.writeframes((quiet * 32767).astype("<i2").tobytes())
        wav.writeframes((loud * 32767).astype("<i2").tobytes())
    profile = detect_noise_profile(original, window_s=2.0)
    assert profile["start_s"] == 2.0
    assert profile["noise_floor_db"] is not None
