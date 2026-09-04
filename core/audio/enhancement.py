"""Automatic, non-destructive speech enhancement for recorded meetings.

The original WAV is never overwritten.  A profile is translated into an
FFmpeg filter chain because FFmpeg is already a required local dependency and
provides well-tested audio filters for denoising, compression and loudness.
The quietest stable window is measured first and its noise floor tunes the
denoiser for this particular recording.
"""
from __future__ import annotations

import copy
import math
import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np

from core.logging_setup import get_logger

log = get_logger("ma.audio.enhancement")

PROFILE_FIELDS = {
    "noise_reduction_db": (0.0, 20.0),
    "gate_threshold_db": (-60.0, -10.0),
    "gate_ratio": (1.0, 20.0),
    "gate_range": (0.0, 1.0),
    "gate_attack_ms": (1.0, 200.0),
    "gate_release_ms": (20.0, 2000.0),
    "gate_knee": (1.0, 8.0),
    "hum_frequency_hz": (50.0, 60.0),
    "hum_reduction_db": (0.0, 24.0),
    "highpass_hz": (0.0, 300.0),
    "presence_db": (-6.0, 6.0),
    "presence_hz": (1500.0, 6000.0),
    "presence_q": (0.3, 4.0),
    "deesser_intensity": (0.0, 1.0),
    "deesser_max_reduction": (0.0, 1.0),
    "deesser_treble_keep": (0.0, 1.0),
    "compressor_threshold_db": (-45.0, -3.0),
    "compressor_ratio": (1.0, 10.0),
    "compressor_attack_ms": (1.0, 200.0),
    "compressor_release_ms": (20.0, 2000.0),
    "compressor_knee": (1.0, 8.0),
    "compressor_makeup_db": (0.0, 12.0),
    "loudness_lufs": (-30.0, -12.0),
    "loudness_range_lu": (1.0, 20.0),
    "loudness_true_peak_db": (-6.0, -0.1),
    "limiter_db": (-6.0, -0.1),
    "limiter_attack_ms": (1.0, 100.0),
    "limiter_release_ms": (10.0, 1000.0),
}

PROFILE_TOGGLES = (
    "noise_reduction_enabled",
    "gate_enabled",
    "hum_filter_enabled",
    "highpass_enabled",
    "presence_enabled",
    "deesser_enabled",
    "compressor_enabled",
    "loudness_enabled",
    "limiter_enabled",
)

_DEFAULT_PROFILE = {
    "noise_reduction_enabled": False,
    "gate_enabled": False,
    "gate_threshold_db": -36.0,
    "gate_ratio": 2.0,
    "gate_range": 0.25,
    "gate_attack_ms": 30.0,
    "gate_release_ms": 450.0,
    "gate_knee": 4.0,
    "hum_filter_enabled": False,
    "hum_frequency_hz": 50.0,
    "hum_reduction_db": 12.0,
    "highpass_enabled": False,
    "presence_enabled": False,
    "deesser_enabled": False,
    "compressor_enabled": False,
    "loudness_enabled": False,
    "limiter_enabled": False,
    "noise_reduction_db": 6.0,
    "highpass_hz": 80.0,
    "presence_db": 2.0,
    "presence_hz": 3000.0,
    "presence_q": 1.0,
    "deesser_intensity": 0.35,
    "deesser_max_reduction": 0.5,
    "deesser_treble_keep": 0.5,
    "compressor_threshold_db": -20.0,
    "compressor_ratio": 2.5,
    "compressor_attack_ms": 15.0,
    "compressor_release_ms": 220.0,
    "compressor_knee": 3.0,
    "compressor_makeup_db": 0.0,
    "loudness_lufs": -19.0,
    "loudness_range_lu": 7.0,
    "loudness_true_peak_db": -1.0,
    "limiter_db": -1.0,
    "limiter_attack_ms": 5.0,
    "limiter_release_ms": 50.0,
}


def default_audio_profiles() -> dict[str, dict[str, float]]:
    """Return independent, user-editable starter profiles."""
    meeting = copy.deepcopy(_DEFAULT_PROFILE)
    natural = copy.deepcopy(_DEFAULT_PROFILE)
    natural.update(noise_reduction_db=3.0, presence_db=1.0,
                   compressor_threshold_db=-22.0, compressor_ratio=2.0)
    noisy = copy.deepcopy(_DEFAULT_PROFILE)
    noisy.update(noise_reduction_db=9.0, highpass_hz=90.0, presence_db=3.0,
                 compressor_threshold_db=-23.0, compressor_ratio=3.0)
    return {"natural": natural, "meeting": meeting, "noisy": noisy}


def normalize_profile(value: Any) -> dict[str, Any]:
    """Clamp malformed hand-edited profile values to safe DSP ranges."""
    out: dict[str, Any] = {}
    for field, (low, high) in PROFILE_FIELDS.items():
        raw = value.get(field) if isinstance(value, dict) else None
        try:
            number = float(raw)
        except (TypeError, ValueError):
            number = float(_DEFAULT_PROFILE[field])
        if not math.isfinite(number):
            number = float(_DEFAULT_PROFILE[field])
        out[field] = round(max(low, min(high, number)), 3)
    for field in PROFILE_TOGGLES:
        raw = value.get(field) if isinstance(value, dict) else None
        out[field] = raw if isinstance(raw, bool) else bool(_DEFAULT_PROFILE[field])
    return out


def normalize_profiles(value: Any) -> dict[str, dict[str, float]]:
    defaults = default_audio_profiles()
    if not isinstance(value, dict):
        return defaults
    profiles: dict[str, dict[str, float]] = {}
    for name, profile in value.items():
        if not isinstance(name, str):
            continue
        clean_name = name.strip()[:64]
        if clean_name:
            profiles[clean_name] = normalize_profile(profile)
    if not profiles:
        return defaults
    # Built-ins remain available even when a user profile was hand-edited.
    for name, profile in defaults.items():
        profiles.setdefault(name, profile)
    return profiles


def pcm_channel_values(raw: bytes, width: int, channels: int,
                       channel: int = 0) -> np.ndarray:
    """Decode one interleaved PCM channel into normalized float samples."""
    frame_width = width * channels
    usable = len(raw) - (len(raw) % frame_width)
    if usable <= 0:
        return np.zeros(0, dtype=np.float32)
    frames = np.frombuffer(raw[:usable], dtype=np.uint8).reshape(-1, frame_width)
    selected = frames[:, channel * width:(channel + 1) * width]
    if width == 1:
        return (selected[:, 0].astype(np.float32) - 128.0) / 128.0
    if width == 2:
        values = selected.copy().view("<i2").reshape(-1).astype(np.float32)
        return values / 32768.0
    if width == 3:
        values = (selected[:, 0].astype(np.int32)
                  | (selected[:, 1].astype(np.int32) << 8)
                  | (selected[:, 2].astype(np.int32) << 16))
        values = np.where(values & 0x800000, values - 0x1000000, values)
        return values.astype(np.float32) / 8388608.0
    if width == 4:
        values = selected.copy().view("<i4").reshape(-1).astype(np.float32)
        return values / 2147483648.0
    raise ValueError(f"Nicht unterstützte PCM-Bittiefe: {width * 8} Bit")


def detect_noise_profile(path: Path, channel: int = 0,
                         window_s: float = 2.0,
                         start_s: float | None = None,
                         end_s: float | None = None) -> dict[str, float | None]:
    """Find the quietest non-digital-silence window and estimate its floor.

    This is the automated equivalent of selecting a short silent region in
    Audacity.  The result is used to tune FFmpeg's adaptive denoiser.  A
    changing noise source is handled by the denoiser's tracking mode.
    """
    best_rms = float("inf")
    best_start = None
    manual_requested = start_s is not None or end_s is not None
    try:
        with wave.open(str(path), "rb") as wav:
            rate = max(1, wav.getframerate())
            channels = max(1, wav.getnchannels())
            channel = min(max(0, int(channel)), channels - 1)
            width = wav.getsampwidth()
            if start_s is not None or end_s is not None:
                raw_start = float(start_s) if start_s is not None else 0.0
                raw_end = float(end_s) if end_s is not None else raw_start + window_s
                if not math.isfinite(raw_start) or not math.isfinite(raw_end):
                    raise ValueError("Das Rauschprofil muss einen gültigen Zeitbereich enthalten.")
                manual_start = max(0.0, raw_start)
                manual_end = raw_end
                file_duration = wav.getnframes() / rate
                if manual_start >= file_duration:
                    raise ValueError("Das Rauschprofil liegt außerhalb der Aufnahme.")
                manual_end = min(file_duration, manual_end)
                if manual_end <= manual_start:
                    raise ValueError("Das Rauschprofil muss einen gültigen Zeitbereich enthalten.")
                wav.setpos(min(wav.getnframes(), int(manual_start * rate)))
                remaining = max(1, int((manual_end - manual_start) * rate))
                sum_squares = 0.0
                sample_count = 0
                while remaining > 0:
                    raw = wav.readframes(min(remaining, rate * 5))
                    if not raw:
                        break
                    values = pcm_channel_values(raw, width, channels, channel)
                    if values.size == 0:
                        break
                    sum_squares += float(np.sum(np.square(values), dtype=np.float64))
                    sample_count += len(values)
                    remaining -= len(values)
                if sample_count == 0:
                    raise ValueError("Das Rauschprofil konnte nicht gelesen werden.")
                rms = float(np.sqrt(sum_squares / sample_count))
                return {
                    "start_s": round(manual_start, 3),
                    "duration_s": round(manual_end - manual_start, 3),
                    "noise_floor_db": round(max(-70.0, min(-20.0, 20.0 * math.log10(max(rms, 1e-12)))), 2),
                }
            frames_per_window = max(1, int(rate * window_s))
            start_frame = 0
            while True:
                raw = wav.readframes(frames_per_window)
                if not raw:
                    break
                values = pcm_channel_values(raw, width, channels, channel)
                if values.size == 0:
                    break
                rms = float(np.sqrt(np.mean(np.square(values))))
                # Ignore digital silence: it contains no usable noise profile.
                if 1e-5 < rms < best_rms:
                    best_rms = rms
                    best_start = start_frame / rate
                start_frame += len(values)
    except (OSError, wave.Error):
        return {"start_s": None, "duration_s": None, "noise_floor_db": -50.0}
    except ValueError:
        if manual_requested:
            raise
        return {"start_s": None, "duration_s": None, "noise_floor_db": -50.0}
    if best_start is None:
        return {"start_s": None, "duration_s": None, "noise_floor_db": -50.0}
    return {
        "start_s": round(best_start, 3),
        "duration_s": float(window_s),
        "noise_floor_db": round(max(-70.0, min(-20.0, 20.0 * math.log10(best_rms))), 2),
    }


def _ffmpeg_number(value: float) -> str:
    return f"{float(value):.3f}".rstrip("0").rstrip(".")


def _filter_chain(profile: dict[str, float], noise_floor_db: float,
                  noise_sample_start_s: float | None = None,
                  noise_sample_end_s: float | None = None) -> str:
    filters: list[str] = []
    if profile["noise_reduction_enabled"] and profile["noise_reduction_db"] > 0:
        if noise_sample_start_s is not None and noise_sample_end_s is not None:
            filters.extend([
                f"asendcmd={_ffmpeg_number(noise_sample_start_s)} afftdn@denoise sn start",
                f"asendcmd={_ffmpeg_number(noise_sample_end_s)} afftdn@denoise sn stop",
                "afftdn@denoise="
                f"nr={_ffmpeg_number(profile['noise_reduction_db'])}:"
                f"nf={_ffmpeg_number(noise_floor_db)}:tn=0",
            ])
        else:
            filters.append(
                "afftdn="
                f"nr={_ffmpeg_number(profile['noise_reduction_db'])}:"
                f"nf={_ffmpeg_number(noise_floor_db)}:tn=1")
    if profile["hum_filter_enabled"] and profile["hum_reduction_db"] > 0:
        for harmonic in (1, 2, 3):
            frequency = profile["hum_frequency_hz"] * harmonic
            filters.append(
                f"equalizer=f={_ffmpeg_number(frequency)}:t=q:w=8:"
                f"g=-{_ffmpeg_number(profile['hum_reduction_db'])}")
    if profile["highpass_enabled"] and profile["highpass_hz"] > 0:
        filters.append(f"highpass=f={_ffmpeg_number(profile['highpass_hz'])}")
    if profile["gate_enabled"]:
        gate_threshold = 10.0 ** (profile["gate_threshold_db"] / 20.0)
        filters.append(
            "agate="
            f"threshold={_ffmpeg_number(gate_threshold)}:"
            f"ratio={_ffmpeg_number(profile['gate_ratio'])}:"
            f"range={_ffmpeg_number(profile['gate_range'])}:"
            f"attack={_ffmpeg_number(profile['gate_attack_ms'])}:"
            f"release={_ffmpeg_number(profile['gate_release_ms'])}:"
            f"knee={_ffmpeg_number(profile['gate_knee'])}:makeup=1:detection=rms")
    if profile["presence_enabled"] and abs(profile["presence_db"]) > 0.01:
        filters.append(
            f"equalizer=f={_ffmpeg_number(profile['presence_hz'])}:"
            f"t=q:w={_ffmpeg_number(profile['presence_q'])}:g={_ffmpeg_number(profile['presence_db'])}")
    if profile["deesser_enabled"] and profile["deesser_intensity"] > 0:
        filters.append(
            "deesser="
            f"i={_ffmpeg_number(profile['deesser_intensity'])}:"
            f"m={_ffmpeg_number(profile['deesser_max_reduction'])}:"
            f"f={_ffmpeg_number(profile['deesser_treble_keep'])}")
    threshold_linear = 10.0 ** (profile["compressor_threshold_db"] / 20.0)
    if profile["compressor_enabled"] and profile["compressor_ratio"] > 1.01:
        makeup_linear = 10.0 ** (profile["compressor_makeup_db"] / 20.0)
        filters.append(
            "acompressor="
            f"threshold={_ffmpeg_number(threshold_linear)}:"
            f"ratio={_ffmpeg_number(profile['compressor_ratio'])}:"
            f"attack={_ffmpeg_number(profile['compressor_attack_ms'])}:"
            f"release={_ffmpeg_number(profile['compressor_release_ms'])}:"
            f"knee={_ffmpeg_number(profile['compressor_knee'])}:"
            f"makeup={_ffmpeg_number(makeup_linear)}:detection=rms")
    if profile["loudness_enabled"]:
        filters.append(
            f"loudnorm=I={_ffmpeg_number(profile['loudness_lufs'])}:"
            f"TP={_ffmpeg_number(profile['loudness_true_peak_db'])}:"
            f"LRA={_ffmpeg_number(profile['loudness_range_lu'])}")
    if profile["limiter_enabled"]:
        limit_linear = 10.0 ** (profile["limiter_db"] / 20.0)
        filters.append(
            f"alimiter=limit={_ffmpeg_number(limit_linear)}:"
            f"attack={_ffmpeg_number(profile['limiter_attack_ms'])}:"
            f"release={_ffmpeg_number(profile['limiter_release_ms'])}:latency=1")
    return ",".join(filters)


def enhance_audio(input_path: Path, output_path: Path, profile: dict[str, Any],
                  source: str = "mic", force: bool = False,
                  start_s: float = 0.0, duration_s: float | None = None,
                  noise_profile_mode: str = "automatic",
                  noise_profile_start_s: float | None = None,
                  noise_profile_end_s: float | None = None) -> dict[str, Any]:
    """Create an enhanced WAV next to the immutable original recording."""
    input_path = Path(input_path).resolve()
    output_path = Path(output_path).resolve()
    if source == "system":
        return {"status": "skipped", "reason": "system_audio"}
    if noise_profile_mode not in {"disabled", "automatic", "manual"}:
        raise ValueError("Unbekannter Rauschprofil-Modus.")
    if noise_profile_mode == "manual" and (noise_profile_start_s is None or noise_profile_end_s is None):
        raise ValueError("Für das manuelle Rauschprofil muss ein Bereich ausgewählt werden.")
    if noise_profile_mode == "disabled" and (noise_profile_start_s is not None or noise_profile_end_s is not None):
        raise ValueError("Ein Rauschprofil ist deaktiviert.")
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if output_path.exists() and not force:
        return {"status": "ready", "path": str(output_path)}
    start_s = max(0.0, float(start_s))
    if duration_s is not None:
        duration_s = max(1.0, min(30.0, float(duration_s)))
    try:
        with wave.open(str(input_path), "rb") as wav:
            channels = wav.getnchannels()
            rate = wav.getframerate()
            duration = wav.getnframes() / max(1, rate)
    except (OSError, wave.Error) as exc:
        raise ValueError("Originalaufnahme ist keine gültige WAV-Datei.") from exc

    if noise_profile_start_s is not None or noise_profile_end_s is not None:
        if noise_profile_start_s is None or noise_profile_end_s is None:
            raise ValueError("Für das Rauschprofil müssen Anfang und Ende angegeben werden.")
        profile_start = float(noise_profile_start_s)
        profile_end = float(noise_profile_end_s)
        if (not math.isfinite(profile_start) or not math.isfinite(profile_end)
                or profile_start < 0 or profile_end <= profile_start
                or profile_end > duration or profile_end - profile_start > 30.0):
            raise ValueError("Das Rauschprofil muss innerhalb der Aufnahme liegen und darf höchstens 30 Sekunden lang sein.")

    clean_profile = normalize_profile(profile)
    has_denoiser = (clean_profile["noise_reduction_enabled"]
                    and clean_profile["noise_reduction_db"] > 0)
    noise = (detect_noise_profile(
        input_path, channel=0,
        start_s=noise_profile_start_s,
        end_s=noise_profile_end_s)
             if has_denoiser and noise_profile_mode != "disabled" else
             {"start_s": None, "duration_s": None, "noise_floor_db": -50.0})
    noise_floor = float(noise.get("noise_floor_db") or -50.0)
    sample_start = noise_profile_start_s if has_denoiser and noise_profile_mode == "manual" else None
    sample_end = noise_profile_end_s if has_denoiser and noise_profile_mode == "manual" else None
    filter_from_full_source = False
    if sample_start is not None and sample_end is not None and duration_s is not None:
        view_end = start_s + duration_s
        if sample_start < start_s or sample_end > view_end:
            # Keep absolute timestamps so the manual noise sample is still
            # available when the preview listens to a different part.
            filter_from_full_source = True
        else:
            sample_start -= start_s
            sample_end -= start_s
    chain = _filter_chain(clean_profile, noise_floor, sample_start, sample_end)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_name(output_path.name + ".tmp")
    if not chain and start_s == 0.0 and duration_s is None:
        shutil.copyfile(input_path, tmp)
        os.replace(tmp, output_path)
        os.chmod(output_path, 0o444)
        return {"status": "ready", "path": str(output_path), "noise_profile": noise,
                "profile": clean_profile, "unchanged": True}
    if source == "both" and channels == 2 and chain:
        # Preserve the system channel bit-for-bit while cleaning only the mic.
        filter_complex = (
            "[0:a]channelsplit=channel_layout=stereo[mic][system];"
            f"[mic]{chain}[cleanmic];[cleanmic][system]amerge=inputs=2[a]"
        )
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if start_s > 0 and not filter_from_full_source:
            command += ["-ss", _ffmpeg_number(start_s)]
        if duration_s is not None and not filter_from_full_source:
            command += ["-t", _ffmpeg_number(duration_s)]
        command += ["-i", str(input_path),
                   "-filter_complex", filter_complex, "-map", "[a]",
                   *( ["-ss", _ffmpeg_number(start_s), "-t", _ffmpeg_number(duration_s)]
                      if filter_from_full_source and duration_s is not None else []),
                   "-ac", "2", "-ar", str(rate), "-c:a", "pcm_s16le", "-f", "wav", "-y", str(tmp)]
    else:
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if start_s > 0 and not filter_from_full_source:
            command += ["-ss", _ffmpeg_number(start_s)]
        if duration_s is not None and not filter_from_full_source:
            command += ["-t", _ffmpeg_number(duration_s)]
        command += ["-i", str(input_path)]
        if chain:
            command += ["-af", chain]
        if filter_from_full_source and duration_s is not None:
            command += ["-ss", _ffmpeg_number(start_s), "-t", _ffmpeg_number(duration_s)]
        command += ["-ac", str(channels), "-ar", str(rate),
                   "-c:a", "pcm_s16le", "-f", "wav", "-y", str(tmp)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[:500] or "FFmpeg-Audioaufbereitung fehlgeschlagen.")
        os.replace(tmp, output_path)
        os.chmod(output_path, 0o444)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    log.info("audio_enhanced source=%s profile=%s noise=%s start=%s output=%s",
             source, clean_profile, noise_floor, noise.get("start_s"), output_path)
    return {"status": "ready", "path": str(output_path), "noise_profile": noise,
            "profile": clean_profile}
