"""faster-whisper (CTranslate2) ASR engine.

Local, CPU-friendly (int8 by default). The model is downloaded on first use
into the project's models dir, and only after the network gate passes (network
enabled + explicit confirmation). `faster_whisper` is imported lazily so the
rest of the system runs without it installed.
"""
from __future__ import annotations

import threading
from pathlib import Path

from core.config import Config, get_config
from core.logging_setup import get_logger
from core.providers.base import ASREngine, ASRError, ASRSegment, ModelNotReadyError
from core.security.secrets import require_network_action

log = get_logger("ma.asr.whisper")


class FasterWhisperEngine(ASREngine):
    def __init__(self, model_name: str = "small", compute_type: str = "int8",
                 device: str = "cpu", config: Config | None = None):
        self.config = config or get_config()
        self.model_name = model_name
        self.compute_type = compute_type
        self.device = device
        self.download_root = Path(self.config.models_dir)
        self._model = None
        self._model_lock = threading.Lock()

    def _model_id(self) -> str:
        return f"Systran/faster-whisper-{self.model_name}"

    def _repo_dir(self) -> str:
        # huggingface_hub cache folder name for the repo (slashes -> "--").
        return "models--" + self._model_id().replace("/", "--")

    def _model_file(self) -> Path:
        # Canonical flat location (also what unit tests write to).
        return self.download_root / self._model_id().replace("/", "--") / "model.bin"

    def _cache_model_files(self) -> list[Path]:
        # faster-whisper uses download_root as an HF cache dir, so the weights
        # land in models--<publisher>--faster-whisper-<name>/snapshots/<rev>.
        # Recent faster-whisper releases moved large-v3-turbo from the
        # historical Systran publisher to mobiuslabsgmbh. Recognise both (and
        # any future publisher) instead of making an already downloaded model
        # appear missing in the UI.
        roots = [self.download_root / self._repo_dir()]
        pattern = f"models--*--faster-whisper-{self.model_name}"
        roots.extend(p for p in self.download_root.glob(pattern) if p not in roots)
        files: list[Path] = []
        for root in roots:
            snap = root / "snapshots"
            if snap.is_dir():
                files.extend(p for p in snap.glob("*/model.bin") if p.is_file())
        return sorted(set(files))

    def is_model_ready(self) -> bool:
        return self._model_file().is_file() or bool(self._cache_model_files())

    def model_size_note(self) -> str:
        return f"~{self._approx_mb(self.model_name)} MB (int8, {self.model_name})"

    @staticmethod
    def _approx_mb(name: str) -> int:
        return {
            "tiny": 75, "base": 145, "small": 485, "medium": 1500,
            "large-v2": 3100, "large-v3": 3100, "large-v3-turbo": 1600,
            "distil-large-v3": 1500,
        }.get(name, 485)

    def prepare_model(self, allow_download: bool = False) -> None:
        if self.is_model_ready():
            self._load_model(local_files_only=True)
            return
        if not allow_download:
            raise ModelNotReadyError(
                f"ASR-Modell '{self.model_name}' ist nicht heruntergeladen. "
                f"Starten Sie den Download zuerst (Netzwerk aktivieren + "
                f"ausdrücklich bestätigen). {self.model_size_note()}"
            )
        require_network_action(
            f"download ASR model '{self.model_name}'", self.config, confirmed=True)
        log.info("downloading ASR model '%s' -> %s", self.model_name, self.download_root)
        self._load_model(local_files_only=False)

    def _load_model(self, local_files_only: bool = False):
        if self._model is None:
            # Double-checked: only one thread pays for the expensive load; the
            # rest wait and reuse the shared model (L9).
            with self._model_lock:
                if self._model is None:
                    from faster_whisper import WhisperModel  # lazy heavy import
                    self.download_root.mkdir(parents=True, exist_ok=True)
                    log.info("loading ASR model=%s compute=%s device=%s local_only=%s",
                             self.model_name, self.compute_type, self.device, local_files_only)
                    self._model = WhisperModel(
                        self.model_name, device=self.device, compute_type=self.compute_type,
                        download_root=str(self.download_root), local_files_only=local_files_only)
        return self._model

    def transcribe(self, audio_path: Path | str,
                   language: str | None = None) -> list[ASRSegment]:
        return self.transcribe_with_progress(audio_path, language=language)

    def transcribe_with_progress(self, audio_path: Path | str,
                                 language: str | None = None,
                                 on_segment=None) -> list[ASRSegment]:
        audio_path = Path(audio_path)
        if not self.is_model_ready():
            raise ModelNotReadyError(
                f"ASR-Modell '{self.model_name}' ist nicht vorhanden.")
        if not audio_path.is_file():
            raise ASRError(f"Audio-Datei nicht gefunden: {audio_path}")
        model = self._load_model(local_files_only=True)
        segments, info = model.transcribe(
            str(audio_path), language=language or None,
            beam_size=5, vad_filter=True, vad_parameters={"min_silence_duration_ms": 500})
        out: list[ASRSegment] = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            conf = None
            if seg.avg_logprob is not None:
                conf = round(max(0.0, min(1.0, 1.0 + float(seg.avg_logprob))), 4)
            item = ASRSegment(
                start_s=round(seg.start, 3), end_s=round(seg.end, 3),
                text=text, raw_text=seg.text.strip(),
                language=info.language, confidence=conf)
            out.append(item)
            if on_segment is not None:
                on_segment(item)
        return out

    def transcribe_array(self, audio: "np.ndarray",
                         language: str | None = None,
                         *, beam_size: int = 1) -> list[ASRSegment]:
        """Transcribe an in-memory 16 kHz mono float32 array (live path).

        Returns segments with times relative to the *start of the array* (0-based);
        the caller applies any absolute offset. No VAD filter so the trailing,
        not-yet-settled speech is preserved for live display.
        """
        import numpy as np  # noqa: F811  (redundant; kept for clarity/lint)
        if audio is None or len(audio) == 0:
            return []
        model = self._load_model(local_files_only=True)
        seg_iter, info = model.transcribe(
            audio, language=language or None, beam_size=beam_size,
            condition_on_previous_text=False, vad_filter=False)
        out: list[ASRSegment] = []
        for seg in seg_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            conf = None
            if seg.avg_logprob is not None:
                conf = round(max(0.0, min(1.0, 1.0 + float(seg.avg_logprob))), 4)
            out.append(ASRSegment(
                start_s=round(seg.start, 3), end_s=round(seg.end, 3),
                text=text, raw_text=text,
                language=info.language, confidence=conf))
        return out
