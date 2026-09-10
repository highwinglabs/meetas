"""whisper.cpp (ggml) ASR engine via the optional ``pywhispercpp`` runtime.

Second, optional ASR backend for installs where faster-whisper/CTranslate2 is
not the best GPU path (notably AMD GPUs via Vulkan: build ``pywhispercpp``
with ``GGML_VULKAN=1``, plain PyPI wheels are CPU-only). The runtime is
imported lazily so the rest of the system and the tests run without it.

Models are the official ``ggml-<name>.bin`` files from the
``ggerganov/whisper.cpp`` HuggingFace repo. Like every other model they are
downloaded only through the network gate + explicit confirmation and stored in
the project's models dir.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import threading
from pathlib import Path

from core.config import Config, get_config
from core.logging_setup import get_logger
from core.providers.base import ASREngine, ASRError, ASRSegment, ModelNotReadyError
from core.security.secrets import require_network_action

log = get_logger("ma.asr.whispercpp")

HF_REPO_ID = "ggerganov/whisper.cpp"

# ggml files exist for these model ids in the official repo.
SUPPORTED_MODELS = (
    "tiny", "base", "small", "medium", "large-v2", "large-v3", "large-v3-turbo",
)

_APPROX_MB = {
    "tiny": 75, "base": 145, "small": 485, "medium": 1500,
    "large-v2": 3100, "large-v3": 3100, "large-v3-turbo": 1600,
}


def vulkan_available() -> bool:
    """Best-effort Vulkan loader probe (runtime library present)."""
    for name in ("libvulkan.so.1", "libvulkan.so", "vulkan-1.dll", "libvulkan.dylib"):
        try:
            ctypes.CDLL(name)
            return True
        except OSError:
            continue
    return False


def _parse_vulkan_summary(stdout: str) -> int | None:
    """First DISCRETE_GPU index from ``vulkaninfo --summary`` output, else None."""
    index: int | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("GPU") and line.endswith(":"):
            try:
                index = int(line[3:-1])
            except ValueError:
                index = None
        elif index is not None and "deviceType" in line:
            if "DISCRETE_GPU" in line:
                return index
            index = None
    return None


def discrete_vulkan_device_index() -> int | None:
    """Index of the first discrete GPU in Vulkan's device list, else None.

    whisper.cpp (ggml-vulkan) uses the *first* Vulkan device by default, which
    on hybrid systems (iGPU + dGPU) is usually the slow iGPU. To make the
    discrete card the one that is actually used, the caller restricts ggml's
    device list via the ``GGML_VK_VISIBLE_DEVICES`` environment variable
    (ggml's own CUDA_VISIBLE_DEVICES-style switch). The index must be the
    raw ``vkEnumeratePhysicalDevices`` index; ``vulkaninfo --summary``
    (vulkan-tools) reports exactly that. Returns None when the tool is not
    available so the caller falls back to whisper.cpp's default behavior.
    """
    exe = shutil.which("vulkaninfo")
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, "--summary"], capture_output=True, text=True,
                              timeout=15)
        if proc.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_vulkan_summary(proc.stdout)


def binding_available() -> bool:
    """True when the pywhispercpp native binding can be imported."""
    try:
        from pywhispercpp.model import Model  # noqa: F401
        return True
    except Exception:
        return False


_vulkan_available = vulkan_available  # internal alias


class WhisperCppEngine(ASREngine):
    def __init__(self, model_name: str = "small", device: str = "auto",
                 config: Config | None = None):
        self.config = config or get_config()
        self.model_name = model_name
        self.device = device
        self.download_root = Path(self.config.models_dir)
        self._model = None
        self._model_lock = threading.Lock()

    def _model_file(self) -> Path:
        return self.download_root / f"ggml-{self.model_name}.bin"

    def is_model_ready(self) -> bool:
        return self._model_file().is_file()

    def model_size_note(self) -> str:
        return f"~{_APPROX_MB.get(self.model_name, 485)} MB (ggml, {self.model_name})"

    def prepare_model(self, allow_download: bool = False) -> None:
        if self.is_model_ready():
            self._load_model(local_files_only=True)
            return
        if self.model_name not in SUPPORTED_MODELS:
            raise ASRError(
                f"Das Modell '{self.model_name}' ist für whisper-cpp nicht "
                f"verfügbar (unterstützt: {', '.join(SUPPORTED_MODELS)}).")
        if not allow_download:
            raise ModelNotReadyError(
                f"ASR-Modell '{self.model_name}' ist nicht vorhanden. "
                "Bitte in der Modellverwaltung ausdrücklich herunterladen.")
        require_network_action(
            f"download ASR model '{self.model_name}' (whisper.cpp/ggml)",
            self.config, confirmed=True)
        self._download_model()
        self._load_model(local_files_only=True)

    def download_with_progress(self, on_progress=None, confirmed: bool = False) -> None:
        """Explicit download that honours the confirmation gate and reports
        ``(downloaded_bytes, total_bytes)`` progress to the UI. Used by the
        background download service, exactly like the faster-whisper path.
        """
        if self.is_model_ready():
            return
        if self.model_name not in SUPPORTED_MODELS:
            raise ASRError(
                f"Das Modell '{self.model_name}' ist für whisper-cpp nicht "
                f"verfügbar (unterstützt: {', '.join(SUPPORTED_MODELS)}).")
        require_network_action(
            f"download ASR model '{self.model_name}' (whisper.cpp/ggml)",
            self.config, confirmed=confirmed)
        self._download_model(on_progress=on_progress)

    def _download_model(self, on_progress=None) -> None:
        from huggingface_hub import snapshot_download
        self.download_root.mkdir(parents=True, exist_ok=True)
        log.info("downloading whisper-cpp model=%s repo=%s", self.model_name, HF_REPO_ID)
        from core.providers.faster_whisper import _progress_tqdm_factory
        try:
            snapshot_download(
                repo_id=HF_REPO_ID,
                cache_dir=str(self.download_root),
                allow_patterns=[f"ggml-{self.model_name}.bin"],
                tqdm_class=_progress_tqdm_factory(on_progress),
            )
        except TypeError:
            # huggingface_hub too old for tqdm_class: no progress, same result.
            snapshot_download(
                repo_id=HF_REPO_ID,
                cache_dir=str(self.download_root),
                allow_patterns=[f"ggml-{self.model_name}.bin"],
            )
        # The HF cache stores files under models--<repo>/snapshots/<rev>/ as
        # (relative) symlinks into the blobs dir. Link the real file into the
        # canonical flat location without breaking the layout.
        pattern = (f"models--{HF_REPO_ID.replace('/', '--')}/**/"
                   f"ggml-{self.model_name}.bin")
        for cand in self.download_root.glob(pattern):
            if cand == self._model_file():
                continue
            target = cand.resolve()
            if target.is_file():
                self._model_file().parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(target, self._model_file())
                except OSError:
                    import shutil
                    shutil.copy2(target, self._model_file())
                break
        if not self._model_file().is_file():
            raise ASRError(f"ASR-Modell '{self.model_name}' wurde nicht gefunden.")

    def resolved_device(self) -> str:
        """Normalize to cpu | vulkan | auto ('cuda' means: GPU intent)."""
        if self.device == "cpu":
            return "cpu"
        if self.device in ("vulkan", "cuda"):
            return "vulkan"
        return "auto"

    def _gpu_layers(self) -> int:
        """-1 = all layers on the GPU (whisper.cpp); 0 = CPU only."""
        if self.resolved_device() == "cpu":
            return 0
        if self.resolved_device() == "vulkan":
            return -1
        # auto: use the GPU when a Vulkan loader is present; CPU-only builds
        # of pywhispercpp ignore the layer count harmlessly and stay on CPU.
        return -1 if _vulkan_available() else 0

    def _load_model(self, local_files_only: bool = False):
        if self._model is None:
            # Double-checked: only one thread pays for the expensive load; the
            # rest wait and reuse the shared model.
            with self._model_lock:
                if self._model is None:
                    if not self._model_file().is_file():
                        raise ModelNotReadyError(
                            f"ASR-Modell '{self.model_name}' ist nicht vorhanden.")
                    try:
                        from pywhispercpp.model import Model  # lazy heavy import
                    except ImportError as exc:
                        raise ASRError(
                            "Das optionale ASR-Backend 'whisper-cpp' ist nicht "
                            "installiert (Extra 'whisper-cpp'). "
                            "Für GPU (z.B. AMD/Vulkan): "
                            "GGML_VULKAN=1 pip install pywhispercpp aus dem "
                            "Quellbaum, s. README.") from exc
                    layers = self._gpu_layers()
                    log.info("loading whisper-cpp model=%s gpu_layers=%s local_only=%s",
                             self.model_name, layers, local_files_only)
                    if layers != 0:
                        self._model = self._load_gpu_model(Model)
                    else:
                        self._model = Model(model=str(self._model_file()),
                                             context_params={"use_gpu": False})
        return self._model

    def _load_gpu_model(self, model_cls):
        # whisper.cpp (ggml-vulkan) picks the first visible Vulkan device;
        # on hybrid systems that is usually the iGPU. Restrict ggml's device
        # list to the discrete GPU (if identifiable) so the fast card is used.
        idx = discrete_vulkan_device_index()
        if idx is not None:
            os.environ["GGML_VK_VISIBLE_DEVICES"] = str(idx)
            log.info("whisper-cpp: using discrete Vulkan GPU (device %s)", idx)
        return model_cls(model=str(self._model_file()),
                         context_params={"use_gpu": True})

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

        def _normalize(seg) -> ASRSegment | None:
            # pywhispercpp yields dicts ("start"/"end"/"text") or Segment
            # objects (t0/t1/text); normalize both.
            if isinstance(seg, dict):
                text = str(seg.get("text") or "").strip()
                start_raw, end_raw = seg.get("start", 0.0), seg.get("end", 0.0)
            else:
                text = str(getattr(seg, "text", "") or "").strip()
                # whisper.cpp internal timestamps are centiseconds (1/100 s);
                # the binding passes them through unconverted.
                start_raw = float(getattr(seg, "t0", 0)) / 100.0
                end_raw = float(getattr(seg, "t1", 0)) / 100.0
            if not text:
                return None
            try:
                start_s = round(float(start_raw), 3)
                end_s = round(float(end_raw), 3)
            except (TypeError, ValueError):
                raise ASRError("ASR lieferte ungültige Zeitdaten.")
            if end_s < start_s:
                raise ASRError("ASR lieferte ungültige Zeitdaten.")
            return ASRSegment(start_s=start_s, end_s=end_s, text=text,
                              language=language, raw_text=text)

        out: list[ASRSegment] = []
        out_lock = threading.Lock()

        # Stream segments while they are decoded so callers can report real
        # progress (whisper.cpp would otherwise only yield at the very end).
        # The callback fires from the native decode thread: keep it fast and
        # never let an exception escape into the C layer.
        use_callback = False
        if on_segment is not None:
            import inspect
            try:
                use_callback = "new_segment_callback" in inspect.signature(
                    model.transcribe).parameters
            except (TypeError, ValueError):
                # Binding without an inspectable signature (e.g. native
                # method): stay on the batch fallback, never crash.
                use_callback = False

            def _on_new_segment(seg) -> None:
                try:
                    item = _normalize(seg)
                    if item is None:
                        return
                    with out_lock:
                        out.append(item)
                    on_segment(item)
                except Exception:
                    log.debug("whisper-cpp segment callback failed",
                              exc_info=True)

        # v1.5.x binding: beam search is configured via a nested dict.
        if use_callback:
            result = model.transcribe(
                str(audio_path),
                language=language or None,
                beam_search={"beam_size": 5, "patience": -1.0},
                new_segment_callback=_on_new_segment,
            )
        else:
            result = model.transcribe(
                str(audio_path),
                language=language or None,
                beam_search={"beam_size": 5, "patience": -1.0},
            )

        # Fallback for bindings without the callback: report everything at
        # the end (no live progress, same segments).
        if not out:
            for seg in result:
                item = _normalize(seg)
                if item is None:
                    continue
                out.append(item)
                if on_segment is not None:
                    on_segment(item)
        return out
