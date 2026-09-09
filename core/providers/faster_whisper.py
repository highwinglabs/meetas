"""faster-whisper (CTranslate2) ASR engine.

Local, CPU-friendly (int8 by default). The model is downloaded on first use
into the project's models dir, and only after the network gate passes (network
enabled + explicit confirmation). `faster_whisper` is imported lazily so the
rest of the system runs without it installed.
"""
from __future__ import annotations

import ctypes
import os
import threading
from pathlib import Path

from core.config import Config, get_config
from core.logging_setup import get_logger
from core.providers.base import ASREngine, ASRError, ASRSegment, ModelNotReadyError
from core.security.secrets import require_network_action

log = get_logger("ma.asr.whisper")


class _ProgressTqdm:
    """Minimal tqdm stand-in for huggingface_hub progress reporting.

    huggingface_hub creates ``tqdm_class(total=..., unit=..., ...)`` per file
    and calls ``update(n)`` with byte increments. This class only tracks the
    running total so callers can render real download progress.
    """

    def __init__(self, on_progress=None, **kwargs):
        self._on_progress = on_progress
        self.total = int(kwargs.get("total") or 0)
        self.n = 0

    def update(self, n: int = 1) -> int:
        self.n += int(n)
        if self._on_progress is not None:
            self._on_progress(self.n, self.total)
        return self.n

    def close(self) -> None:  # pragma: no cover - huggingface_hub housekeeping
        pass

    def set_description(self, *args, **kwargs) -> None:  # pragma: no cover
        pass

    def set_postfix_str(self, *args, **kwargs) -> None:  # XET rate reporting
        pass

    @property
    def format_dict(self) -> dict:
        return {}

    def refresh(self) -> None:  # huggingface_hub redraws the (absent) bar
        pass

    def __enter__(self):  # huggingface_hub uses `with tqdm_class(...)` per file
        return self

    def __exit__(self, *exc):  # pragma: no cover - huggingface_hub housekeeping
        return False


def _progress_tqdm_factory(on_progress):
    """Return the tqdm replacement *callable* that huggingface_hub needs.

    huggingface_hub instantiates ``tqdm_class(desc=..., total=..., ...)``
    several times per snapshot (a network "Downloading bytes" bar and a disk
    "Reconstructing..." bar), so the callback must be bound by a factory —
    passing an instance would raise TypeError and silently disable progress.
    Only the reconstruction bar is reported; otherwise the manager's
    monotonic byte counter would be fed by two interleaved counters.
    """

    def factory(**kwargs):
        desc = str(kwargs.get("desc") or "")
        if not desc.startswith("Reconstructing"):
            return _ProgressTqdm(on_progress=None, **kwargs)
        return _ProgressTqdm(on_progress=on_progress, **kwargs)

    return factory


def _preload_rocm_runtime() -> None:
    """Make the CTranslate2 ROCm wheel importable without a system LD_LIBRARY_PATH.

    The official AMD wheel links against ROCm shared libraries (libamdhip64,
    libhiprand, libhipblas, librocrand, ...) that are often installed in
    /opt/rocm-*/lib, which glibc does not search by default. Preloading them
    (RTLD_GLOBAL) resolves the import at runtime; when the loader already finds
    them (e.g. via ldconfig or LD_LIBRARY_PATH) this is a no-op.
    """
    lib_dirs = [d for d in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep) if d]
    lib_dirs += ["/opt/rocm-7.2.0/lib", "/opt/rocm/lib"]
    for name in ("libamdhip64.so.7", "libhsa-runtime64.so.1", "libamd_comgr.so.3",
                 "libhipblas.so.3", "libhiprand.so.1", "librocrand.so.1"):
        for d in lib_dirs:
            path = os.path.join(d, name)
            if os.path.exists(path):
                try:
                    ctypes.CDLL(path, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    log.warning("could not preload ROCm library %s", path)
                break


class FasterWhisperEngine(ASREngine):
    def __init__(self, model_name: str = "small", compute_type: str = "int8",
                 device: str = "auto", config: Config | None = None):
        self.config = config or get_config()
        self.model_name = model_name
        self.compute_type = compute_type
        self.device = device
        self.download_root = Path(self.config.models_dir)
        self._model = None
        self._model_lock = threading.Lock()

    def _model_id(self) -> str:
        return f"Systran/faster-whisper-{self.model_name}"

    def hf_repo_ids(self) -> list[str]:
        """HuggingFace repo candidates in preference order.

        ``large-v3-turbo`` moved from Systran to mobiuslabsgmbh; accept both so
        neither an old nor a new download is reported as missing.
        """
        if self.model_name == "large-v3-turbo":
            return [
                f"mobiuslabsgmbh/faster-whisper-{self.model_name}",
                f"Systran/faster-whisper-{self.model_name}",
            ]
        return [f"Systran/faster-whisper-{self.model_name}"]

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

    def download_with_progress(self, on_progress=None, confirmed: bool = False) -> None:
        """Explicit HuggingFace snapshot download into the standard cache layout.

        Unlike :meth:`prepare_model` this never loads the model and reports
        ``(downloaded_bytes, total_bytes)`` through ``on_progress`` (per file).
        Readiness is unchanged: weights land in
        ``models--<publisher>--faster-whisper-<name>/snapshots/<rev>/model.bin``
        which :meth:`is_model_ready` already recognises.
        """
        if self.is_model_ready():
            return
        require_network_action(
            f"download ASR model '{self.model_name}'", self.config, confirmed=confirmed)
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:  # pragma: no cover - always a faster-whisper dep
            raise ASRError("huggingface_hub ist nicht installiert.") from exc
        last_error: Exception | None = None
        for repo_id in self.hf_repo_ids():
            try:
                log.info("downloading ASR model '%s' from %s -> %s",
                         self.model_name, repo_id, self.download_root)
                try:
                    snapshot_download(
                        repo_id=repo_id,
                        cache_dir=str(self.download_root),
                        tqdm_class=_progress_tqdm_factory(on_progress),
                    )
                except TypeError:
                    # huggingface_hub too old for tqdm_class: no progress, same result.
                    snapshot_download(repo_id=repo_id, cache_dir=str(self.download_root))
                return
            except Exception as exc:  # try the next candidate repo
                last_error = exc
        raise ASRError(
            f"ASR-Modell '{self.model_name}' konnte nicht heruntergeladen werden "
            f"({last_error})") from last_error

    def resolved_device(self) -> str:
        """Resolve "auto" to the best available CTranslate2 device.

        Requires the CTranslate2 module to be importable; with a ROCm wheel
        the runtime libraries must be preloaded first (see _load_model).
        """
        if self.device in ("cpu", "cuda"):
            return self.device
        try:
            import ctranslate2
            return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception as exc:
            log.warning("ASR device auto-detect failed, using CPU: %s", exc)
            return "cpu"

    def _load_model(self, local_files_only: bool = False):
        if self._model is None:
            # Double-checked: only one thread pays for the expensive load; the
            # rest wait and reuse the shared model (L9).
            with self._model_lock:
                if self._model is None:
                    _preload_rocm_runtime()  # no-op when loader already resolves ROCm libs
                    from faster_whisper import WhisperModel  # lazy heavy import
                    device = self.resolved_device()  # after preload: ROCm libs are importable
                    # int8 is a CPU quantization; on the GPU fp16 is the
                    # supported and faster choice.
                    compute = ("float16" if device == "cuda" and self.compute_type == "int8"
                               else self.compute_type)
                    self.download_root.mkdir(parents=True, exist_ok=True)
                    log.info("loading ASR model=%s compute=%s device=%s local_only=%s",
                             self.model_name, compute, device, local_files_only)
                    self._model = WhisperModel(
                        self.model_name, device=device, compute_type=compute,
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
