"""Audio sources.

The capture pipeline is stream-agnostic: everything downstream of an
`AudioSource` (chunking, persistence, crash recovery) is testable with
synthetic or file sources, independent of any real microphone.

`LiveSource` (real microphone) lazily imports `sounddevice` so the core and
its tests run in headless environments without PortAudio installed.
"""
from __future__ import annotations

import abc
import queue
import struct
import wave
from typing import Iterator

import numpy as np

from core.logging_setup import get_logger

log = get_logger("ma.audio.stream")

# How much audio (in seconds) a capture queue may buffer before it starts
# dropping the oldest block.  The PortAudio callback must never block, so a
# stalling consumer is protected by bounding the queue instead of letting it
# grow without limit for the whole session (L3).
_LIVE_QUEUE_BUFFER_S = 5.0


class BoundedChunkQueue:
    """Bounded FIFO of audio blocks with drop-oldest overflow.

    The audio-thread callback must never block (blocking would glitch the
    device), so when the consumer stalls the queue drops its oldest block
    rather than waiting, keeping memory bounded to roughly ``buffer_s`` of
    audio.  A rate-limited warning makes the stall visible without flooding
    the log.  The ``put``/``get``/``qsize`` surface matches :class:`queue.Queue`
    so existing call sites work unchanged.
    """

    def __init__(self, block_ms: int, buffer_s: float = _LIVE_QUEUE_BUFFER_S,
                 name: str = "live"):
        self._q: "queue.Queue[np.ndarray | None]" = queue.Queue(
            maxsize=max(8, int(buffer_s * 1000 / max(1, int(block_ms)))))
        self._name = name
        self._drops = 0

    @property
    def maxsize(self) -> int:
        return self._q.maxsize

    def put(self, item) -> None:
        while True:
            try:
                self._q.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()  # drop the oldest block
                except queue.Empty:
                    continue
                self._drops += 1
                if self._drops == 1 or self._drops % 100 == 0:
                    log.warning(
                        "audio_queue_overflow name=%s dropped_oldest=%s "
                        "(consumer stalling; oldest block dropped to bound memory)",
                        self._name, self._drops)

    def get(self, timeout: float | None = None):
        return self._q.get(timeout=timeout)

    def qsize(self) -> int:
        return self._q.qsize()


def try_rates(rates: list, probe) -> int:
    """Return the first sample rate that ``probe(rate)`` accepts.

    ``probe`` must raise when a rate is unusable. If every rate fails, a single
    ``RuntimeError`` is raised listing each attempt (no silent fallback, no
    retry loop). Pure and dependency-free so it is unit-testable without
    PortAudio."""
    if not rates:
        raise RuntimeError("Keine Samplerate zum Ausprobieren angegeben.")
    attempts = []
    for rate in rates:
        try:
            probe(rate)
            return rate
        except Exception as exc:  # noqa: BLE001 - any open failure is a reject
            attempts.append(f"{rate} Hz ({type(exc).__name__}: {exc})")
    raise RuntimeError(
        "Keine Samplerate vom Gerät akzeptiert: " + "; ".join(attempts))


def probe_portaudio(device_id: int | None, rate: int, channels: int,
                    block_ms: int = 50) -> None:
    """Open then immediately close a PortAudio input stream at ``rate`` (probe).

    Raises (via sounddevice) when the device rejects the rate, which is exactly
    what ``try_rates`` needs to walk the fallback list."""
    import sounddevice as sd  # pragma: no cover - env dependent
    stream = sd.InputStream(  # pragma: no cover
        samplerate=rate, channels=channels, dtype="float32",
        device=device_id, blocksize=max(1, int(rate * block_ms / 1000)))
    stream.start()
    stream.stop()
    stream.close()


class AudioSource(abc.ABC):
    sample_rate: int
    channels: int

    @abc.abstractmethod
    def frames(self) -> Iterator[np.ndarray]:
        """Yield blocks of float32 audio shaped (n, channels), values in [-1, 1]."""

    def close(self) -> None:  # optional cleanup
        return None


class SyntheticSource(AudioSource):
    """Deterministic source (tone + low-level noise) for tests and demos."""

    def __init__(self, duration_s: float, sample_rate: int = 16000,
                 channels: int = 1, block_seconds: float = 0.05,
                 freq: float = 440.0, amplitude: float = 0.3, seed: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        self.duration_s = duration_s
        self.block_seconds = block_seconds
        self.freq = freq
        self.amplitude = amplitude
        self.seed = seed

    def frames(self) -> Iterator[np.ndarray]:
        rng = np.random.default_rng(self.seed)
        block = max(1, int(self.block_seconds * self.sample_rate))
        total = int(self.duration_s * self.sample_rate)
        produced = 0
        while produced < total:
            n = min(block, total - produced)
            t = np.arange(produced, produced + n) / self.sample_rate
            tone = self.amplitude * np.sin(2 * np.pi * self.freq * t)
            noise = 0.01 * rng.standard_normal(n)
            mono = (tone + noise).astype(np.float32)
            produced += n
            if self.channels == 1:
                yield mono.reshape(-1, 1)
            else:
                yield np.tile(mono[:, None], (1, self.channels))

    def close(self) -> None:
        return None


class FileSource(AudioSource):
    """Read a WAV file and yield fixed-size blocks (for offline reprocessing)."""

    def __init__(self, path, sample_rate: int | None = None, block_seconds: float = 0.05):
        self.path = str(path)
        self._block_seconds = block_seconds
        with wave.open(self.path, "rb") as w:
            self.channels = w.getnchannels()
            self.sample_rate = sample_rate or w.getframerate()
            self._nframes = w.getnframes()
            self._sampwidth = w.getsampwidth()
            self._raw = w.readframes(self._nframes)

    def frames(self) -> Iterator[np.ndarray]:
        block = max(1, int(self._block_seconds * self.sample_rate))
        if self._sampwidth == 2:
            fmt = "<i2"
        elif self._sampwidth == 1:
            fmt = "<u1"
        else:  # pragma: no cover
            raise ValueError(f"unsupported sample width {self._sampwidth}")
        count = len(self._raw) // (self._sampwidth * self.channels)
        samples = struct.unpack(fmt * count, self._raw)
        arr = np.array(samples, dtype=np.float32)
        if self._sampwidth == 2:
            arr = arr / 32768.0
        else:
            arr = (arr - 128) / 128.0
        arr = arr.reshape(-1, self.channels)
        for i in range(0, len(arr), block):
            yield arr[i:i + block]

    def close(self) -> None:
        return None


class LiveSource(AudioSource):
    """Real microphone capture via PortAudio (sounddevice).

    Requires `sounddevice` and a PortAudio device. Raises a clear error if
    either is unavailable so callers can degrade gracefully.
    """

    def __init__(self, device_id: int | None, sample_rate: int = 16000,
                 channels: int = 1, block_ms: int = 50,
                 rate_fallback: list | None = None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.device_id = device_id
        self.block_ms = block_ms
        self.rate_fallback = list(rate_fallback or [])
        self._queue = BoundedChunkQueue(block_ms, name="live")
        self._stream = None
        self._closed = False

    def _callback(self, indata, _frames, _time_info, _status):  # pragma: no cover
        if self._closed:
            return
        self._queue.put(np.copy(indata))

    def _open(self):  # pragma: no cover - needs real PortAudio
        try:
            import sounddevice as sd
        except Exception as exc:
            raise RuntimeError(
                "sounddevice/PortAudio ist nicht verfügbar. Installieren Sie "
                "die System-Bibliothek 'libportaudio2' und das Paket 'sounddevice'."
            ) from exc
        # If the requested rate is rejected by the device, walk the fallback
        # rates instead of failing outright (ALSA often can't do 16 kHz).
        def open_at(rate: int):
            stream = sd.InputStream(
                samplerate=rate, channels=self.channels, dtype="float32",
                device=self.device_id,
                blocksize=max(1, int(rate * self.block_ms / 1000)),
                callback=self._callback)
            try:
                stream.start()
            except Exception:
                # ``try_rates`` intentionally retries another rate.  Close a
                # stream whose start failed, otherwise every rejected rate
                # leaks a PortAudio handle until process exit.
                try:
                    stream.close()
                except Exception:
                    pass
                raise
            self._stream = stream

        self.sample_rate = try_rates(self._candidate_rates(), open_at)

    def _candidate_rates(self) -> list[int]:
        return [self.sample_rate] + [r for r in (self.rate_fallback or [])
                                     if r != self.sample_rate]

    def frames(self) -> Iterator[np.ndarray]:  # pragma: no cover
        self._open()
        try:
            while not self._closed:
                try:
                    block = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                if block is None:
                    break
                yield block
        finally:
            self.close()

    def close(self) -> None:  # pragma: no cover
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass


class CombinedSource(AudioSource):
    """Capture microphone and a Linux loopback device as two channels.

    The two PortAudio streams are kept separate until the writer stores the
    chunk.  This preserves the original sources; ffmpeg creates the mono
    16-kHz ASR copy later.  If either stream cannot be opened, both are closed
    and the caller receives a clear error instead of silently recording only
    one source.
    """

    def __init__(self, microphone_id: int | None, system_id: int,
                 sample_rate: int = 16000, block_ms: int = 50):
        self.sample_rate = int(sample_rate)
        self.channels = 2
        self.microphone_id = microphone_id
        self.system_id = int(system_id)
        self.block_ms = int(block_ms)
        self._queues = (BoundedChunkQueue(self.block_ms, name="mic"),
                        BoundedChunkQueue(self.block_ms, name="system"))
        self._streams = []
        self._closed = False

    def _callback(self, index):
        def callback(indata, _frames, _time_info, _status):  # pragma: no cover
            if not self._closed:
                self._queues[index].put(np.copy(indata))
        return callback

    def _open(self):  # pragma: no cover - needs PortAudio
        try:
            import sounddevice as sd
            for index, device in enumerate((self.microphone_id, self.system_id)):
                stream = sd.InputStream(
                    samplerate=self.sample_rate, channels=1, dtype="float32",
                    device=device, blocksize=max(1, int(self.sample_rate * self.block_ms / 1000)),
                    callback=self._callback(index))
                try:
                    stream.start()
                except Exception:
                    # The failed stream has not been added to ``_streams``
                    # yet, so close it explicitly before closing the streams
                    # that were already started.
                    try:
                        stream.close()
                    except Exception:
                        pass
                    raise
                self._streams.append(stream)
        except Exception as exc:
            self.close()
            raise RuntimeError(
                "Mikrofon und Systemaudio konnten nicht gemeinsam geöffnet werden. "
                "Bitte beide Geräte prüfen; die Mikrofonaufnahme allein bleibt verfügbar."
            ) from exc

    def frames(self) -> Iterator[np.ndarray]:  # pragma: no cover
        self._open()
        try:
            while not self._closed:
                try:
                    mic = self._queues[0].get(timeout=0.5)
                    system = self._queues[1].get(timeout=0.5)
                except queue.Empty:
                    continue
                n = min(len(mic), len(system))
                if n:
                    yield np.concatenate((mic[:n, :1], system[:n, :1]), axis=1)
        finally:
            self.close()

    def close(self) -> None:  # pragma: no cover
        if self._closed:
            return
        self._closed = True
        for stream in self._streams:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._streams.clear()
