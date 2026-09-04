"""Local Parakeet TDT provider.

The model is installed only after an explicit, network-gated download action.
Inference uses the lightweight ``onnx-asr`` CPU runtime and remains fully local
after the model files are present. Until then the service uses the configured
faster-whisper fallback for Live-Text.
"""
from __future__ import annotations

import re
import threading
import wave
from pathlib import Path

import numpy as np

from core.config import Config, get_config
from core.providers.base import ASREngine, ASRError, ASRSegment, ModelNotReadyError
from core.security.secrets import require_network_action


class ParakeetEngine(ASREngine):
    model_name = "parakeet-tdt-0.6b-v3-int8"
    _repo_id = "istupakov/parakeet-tdt-0.6b-v3-onnx"
    _required_files = (
        "config.json",
        "encoder-model.int8.onnx",
        "decoder_joint-model.int8.onnx",
        "nemo128.onnx",
        "vocab.txt",
    )

    def __init__(self, model_name: str | None = None, config: Config | None = None):
        self.config = config or get_config()
        self.model_name = (model_name or self.model_name).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]+", self.model_name):
            raise ValueError("Ungültiger lokaler Modellname.")
        self.model_dir = Path(self.config.models_dir) / self.model_name
        self._model = None
        self._model_lock = threading.RLock()

    def _onnx_model_files_ready(self) -> bool:
        return all((self.model_dir / name).is_file() for name in self._required_files)

    def is_model_ready(self) -> bool:
        # Never report a model ready when only a partial download is present.
        if not self.model_dir.exists() or not self._onnx_model_files_ready():
            return False
        try:
            import onnx_asr  # noqa: F401
            return True
        except Exception:
            return False

    def prepare_model(self, allow_download: bool = False) -> None:
        if self.is_model_ready():
            return
        if not allow_download:
            raise ModelNotReadyError(
                "Parakeet ist lokal noch nicht bereit. Starte den ausdrücklichen "
                "Download in der Modellverwaltung; bis dahin kann faster-whisper "
                "als Ersatz verwendet werden."
            )
        if self.model_name != self.model_name.strip() or self.model_name != "parakeet-tdt-0.6b-v3-int8":
            raise ASRError("Unbekanntes Parakeet-Modell.")
        require_network_action(
            f"download ASR model '{self.model_name}'", self.config, confirmed=True)
        try:
            from huggingface_hub import snapshot_download
            self.model_dir.mkdir(parents=True, exist_ok=True)
            snapshot_download(
                self._repo_id,
                local_dir=str(self.model_dir),
                allow_patterns=list(self._required_files),
                local_files_only=False,
            )
        except Exception as exc:  # noqa: BLE001 - provider error is user-facing
            raise ASRError(f"Parakeet-Download fehlgeschlagen: {exc}") from exc
        if not self._onnx_model_files_ready():
            raise ASRError("Parakeet-Download unvollständig; fehlende Modelldateien.")

    def _load_model(self):
        with self._model_lock:
            if self._model is None:
                if not self.is_model_ready():
                    raise ModelNotReadyError("Parakeet ist nicht vollständig installiert.")
                try:
                    import onnx_asr
                    self._model = onnx_asr.load_model(
                        "nemo-parakeet-tdt-0.6b-v3",
                        path=self.model_dir,
                        quantization="int8",
                    ).with_timestamps()
                except Exception as exc:  # noqa: BLE001 - normalize runtime errors
                    raise ASRError(f"Parakeet-Modell konnte nicht geladen werden: {exc}") from exc
            return self._model

    @staticmethod
    def _read_audio(audio_path: Path) -> tuple[np.ndarray, int]:
        try:
            with wave.open(str(audio_path), "rb") as wav:
                sample_rate = int(wav.getframerate())
                channels = int(wav.getnchannels())
                width = int(wav.getsampwidth())
                frames = wav.readframes(wav.getnframes())
        except (OSError, wave.Error) as exc:
            raise ASRError(f"Audio-Datei konnte für Parakeet nicht gelesen werden: {exc}") from exc
        if width != 2:
            raise ASRError("Parakeet benötigt eine 16-Bit-WAV-Datei.")
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        return audio, sample_rate

    def _transcribe_array(self, audio: np.ndarray, sample_rate: int,
                          offset_s: float = 0.0) -> list[ASRSegment]:
        if len(audio) == 0:
            return []
        with self._model_lock:
            result = self._load_model().recognize(audio, sample_rate=sample_rate)
        text = str(getattr(result, "text", result) or "").strip()
        if not text:
            return []
        timestamps = getattr(result, "timestamps", None) or []
        starts = [float(value) + offset_s for value in timestamps if value is not None]
        start_s = round(starts[0], 3) if starts else round(offset_s, 3)
        end_s = round(offset_s + len(audio) / max(1, sample_rate), 3)
        confidence = None
        logprobs = getattr(result, "logprobs", None) or []
        if logprobs:
            confidence = round(max(0.0, min(1.0, float(np.exp(np.mean(logprobs))))), 4)
        return [ASRSegment(start_s=start_s, end_s=end_s, text=text,
                           language=None, confidence=confidence, raw_text=text)]

    def transcribe(self, audio_path: Path | str,
                   language: str | None = None) -> list[ASRSegment]:
        self.prepare_model(False)
        audio_path = Path(audio_path)
        if not audio_path.is_file():
            raise ASRError(f"Audio-Datei nicht gefunden: {audio_path}")
        audio, sample_rate = self._read_audio(audio_path)
        # Parakeet's exported ONNX graph is intended for short windows. Chunk
        # longer recordings so full-meeting transcription remains reliable.
        chunk_samples = max(1, int(sample_rate * 20.0))
        out: list[ASRSegment] = []
        for begin in range(0, len(audio), chunk_samples):
            chunk = audio[begin:begin + chunk_samples]
            out.extend(self._transcribe_array(chunk, sample_rate, begin / max(1, sample_rate)))
        if language:
            for segment in out:
                segment.language = language
        return out
