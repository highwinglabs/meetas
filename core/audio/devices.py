"""Audio device enumeration. Degrades gracefully without PortAudio."""
from __future__ import annotations

from dataclasses import dataclass

from core.logging_setup import get_logger

log = get_logger("ma.audio.devices")


@dataclass
class DeviceInfo:
    id: int
    name: str
    is_default: bool = False
    is_system_candidate: bool = False


def _looks_like_system_audio(name: str) -> bool:
    """Return a UI hint for common loopback/monitor device names.

    PortAudio exposes these differently on ALSA, PulseAudio and PipeWire, so
    this never guarantees that a stream can be opened; capture still validates
    the device before recording.
    """
    lowered = (name or "").lower()
    return any(word in lowered for word in (
        "monitor", "loopback", "what u hear", "what you hear", "stereo mix",
        "system audio",
    ))


def list_input_devices() -> list[DeviceInfo]:
    """Return available input devices, or [] if no audio subsystem is present.

    Headless environments (no PortAudio / no microphone) yield an empty list
    without raising, so the app stays usable for file-based workflows.
    """
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("sounddevice_unavailable err=%s", exc)
        return []
    try:
        devs = sd.query_devices()
        out: list[DeviceInfo] = []
        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = -1
        for idx, d in enumerate(devs):
            if int(d.get("max_input_channels", 0)) > 0:
                name = d.get("name", f"device-{idx}")
                out.append(DeviceInfo(
                    id=idx, name=name, is_default=(idx == default_in),
                    is_system_candidate=_looks_like_system_audio(name)))
        return out
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("device_query_failed err=%s", exc)
        return []


def default_input_rate(device_id: int | None) -> int | None:
    """Native samplerate of an input device (ALSA often can't do 16 kHz).

    Capturing at the native rate keeps the original high-fidelity; the 16 kHz
    ASR copy is produced during assembly. Returns None when unavailable.
    """
    if device_id is None:
        return None
    try:
        import sounddevice as sd
        return int(sd.query_devices(device_id)["default_samplerate"])
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("device_rate_query_failed id=%s err=%s", device_id, exc)
        return None


def resolve_system_audio_device() -> int | None:
    """Best-effort lookup of a loopback / "what-you-hear" input device.

    Used only when the user explicitly enables system-audio capture. Returns the
    device index, or None when no such device exists (the caller then reports the
    feature unavailable instead of crashing). Never raises.
    """
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("sounddevice_unavailable err=%s", exc)
        return None
    try:
        needle_words = ("loopback", "monitor", "what u hear", "what you hear",
                        "stereo mix", "system audio")
        devs = sd.query_devices()
        for idx, d in enumerate(devs):
            if int(d.get("max_input_channels", 0)) <= 0:
                continue
            name = d.get("name", "").lower()
            for w in needle_words:
                if w in name:
                    return idx
        return None
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("system_audio_lookup_failed err=%s", exc)
        return None


def resolve_input_device(name: str | None = None,
                         index: int | None = None) -> int | None:
    """Resolve a PortAudio input device index.

    Resolution is deliberately stable across PortAudio re-enumeration: a
    configured human-readable name wins over the legacy numeric index, which
    wins over the system default. If nothing is configured and no default is
    exposed, the first available input device is used.
    """
    try:
        import sounddevice as sd
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("sounddevice_unavailable err=%s", exc)
        return None
    try:
        devs = sd.query_devices()
        inputs = [(i, d) for i, d in enumerate(devs) if int(d.get("max_input_channels", 0)) > 0]
        if name:
            needle = name.strip().lower()
            for i, d in inputs:
                if needle in d.get("name", "").lower():
                    return i
            log.warning("configured_input_name_not_found name=%s", name)
        if index is not None:
            for i, _ in inputs:
                if i == index:
                    return index
            log.warning("configured_input_index_missing index=%s", index)
        default_in = sd.default.device[0]
        if default_in is not None and default_in >= 0:
            return default_in
        return inputs[0][0] if inputs else None
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("device_resolve_failed err=%s", exc)
        return None
