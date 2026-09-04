"""Device enumeration and resolution. Degrades gracefully without audio HW."""
from __future__ import annotations

import pytest

from core.audio.devices import list_input_devices, resolve_input_device

pytestmark = pytest.mark.usefixtures("config")


def _has_portaudio() -> bool:
    try:
        import sounddevice  # noqa: F401
        return True
    except Exception:
        return False


def test_list_devices_returns_list():
    devs = list_input_devices()
    assert isinstance(devs, list)
    for d in devs:
        assert isinstance(d.id, int)
        assert isinstance(d.name, str)


def test_resolve_default_returns_valid_input():
    if not _has_portaudio():
        pytest.skip("no PortAudio")
    idx = resolve_input_device()
    inputs = {d.id for d in list_input_devices()}
    if not inputs:
        assert idx is None
    else:
        assert idx in inputs


def test_resolve_by_name_matches():
    if not _has_portaudio():
        pytest.skip("no PortAudio")
    devs = list_input_devices()
    if not devs:
        pytest.skip("no input devices")
    target = devs[0]
    resolved = resolve_input_device(name=target.name)
    assert resolved == target.id


def test_resolve_unknown_name_falls_back_to_default():
    if not _has_portaudio():
        pytest.skip("no PortAudio")
    # unknown name must not raise; it falls back to the default/first input
    resolved = resolve_input_device(name="definitely-not-a-real-device-xyz")
    inputs = {d.id for d in list_input_devices()}
    if not inputs:
        assert resolved is None
    else:
        assert resolved in inputs
