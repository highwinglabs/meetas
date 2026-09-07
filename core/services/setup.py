"""First-run setup wizard: status aggregation and completion. (part of MeetingService)."""
from __future__ import annotations

import shutil

import httpx

from core.logging_setup import get_logger
from core.providers.base import ModelNotReadyError
from core.providers.faster_whisper import FasterWhisperEngine

log = get_logger("ma.service.setup")

# Bump when the wizard gains a new mandatory prerequisite; existing users are
# not re-prompted as long as their on-disk state still satisfies the checks.
SETUP_VERSION = 1

_OLLAMA_INSTALL_HINT_OFFICIAL = "curl -fsSL https://ollama.com/install.sh | sh"
_OLLAMA_INSTALL_HINT_PACMAN = "sudo pacman -Sy --noconfirm --needed ollama"


def _ollama_probe(config) -> tuple[bool, list[str]]:
    """Lightweight reachability probe (0.8 s) against the local Ollama API."""
    base = str(getattr(config, "ollama_base_url", "") or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if not base:
        return False, []
    try:
        response = httpx.get(base + "/api/tags", timeout=0.8)
        response.raise_for_status()
        rows = response.json().get("models", [])
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return False, []
    names = [str(row["name"]) for row in rows
             if isinstance(row, dict) and row.get("name")]
    return True, names


def _model_present(wanted: str, names: list[str]) -> bool:
    if wanted in names:
        return True
    base = str(wanted).split(":")[0]
    return any(str(name).split(":")[0] == base for name in names)


class SetupMixin:
    """Mix-in for MeetingService (attributes are initialised there)."""

    def _ollama_install_hint(self) -> str | None:
        """Distro-specific one-liner to install Ollama.

        None when the binary already exists (in that case only the service may
        be stopped). Arch uses the community package; everything else uses the
        official installer, which handles elevation itself.
        """
        if shutil.which("ollama"):
            return None
        if shutil.which("pacman"):
            return _OLLAMA_INSTALL_HINT_PACMAN
        return _OLLAMA_INSTALL_HINT_OFFICIAL

    def setup_check(self) -> dict:
        """Aggregate the first-run state. No downloads, no long-running calls."""
        cfg = self.config
        asr_model = str(cfg.quality_asr_model or "").strip()
        try:
            engine = self._asr_engine(asr_model)
            asr_ready = bool(engine.is_model_ready())
            size_mb = (FasterWhisperEngine._approx_mb(asr_model)
                       if isinstance(engine, FasterWhisperEngine) else 0)
        except Exception:
            asr_ready, size_mb = False, 0
        disk_free_mb = None
        try:
            usage = shutil.disk_usage(cfg.base_dir)
            disk_free_mb = int(usage.free // (1024 * 1024))
        except OSError:
            pass
        reachable, names = _ollama_probe(cfg)
        summary_model = str(cfg.default_summary_model or "").strip()
        return {
            "setup_completed": bool(cfg.setup_completed),
            "setup_version": int(cfg.setup_version),
            "ffmpeg": bool(cfg.ffmpeg_available()),
            # The wizard uses this to enable the network gate on the explicit
            # download click (it cannot prompt for confirm_network_allowed
            # after the fact once the download worker has started).
            "network_allowed": bool(cfg.network_allowed),
            "disk_free_mb": disk_free_mb,
            "asr": {
                "model": asr_model,
                "size_mb": int(size_mb),
                "installed": asr_ready,
            },
            "ollama": {
                "reachable": reachable,
                "selected_model": summary_model,
                "has_model": _model_present(summary_model, names) if reachable else False,
                "install_hint": self._ollama_install_hint(),
            },
            "download": self.download_status(),
        }

    def setup_complete(self) -> dict:
        """Mark setup as done; only allowed once the selected ASR model is on disk."""
        model = str(self.config.quality_asr_model or "").strip()
        ready = False
        try:
            ready = bool(self._asr_engine(model).is_model_ready())
        except Exception:
            ready = False
        if not ready:
            raise ModelNotReadyError(
                "Der Setup-Assistent kann erst abgeschlossen werden, wenn das "
                f"ASR-Modell '{model}' installiert ist.")
        with self._config_lock:
            self.config.setup_completed = True
            self.config.setup_version = SETUP_VERSION
            self.config.save()
        log.info("setup_completed model=%s", model)
        return {"setup_completed": True, "setup_version": SETUP_VERSION}

    def setup_skip(self) -> dict:
        """Deliberately defer setup: mark it complete without the ASR model.

        Backs the wizard's "Später" button on the ASR screen. Transcription
        stays gracefully unavailable (clear error on demand, model can be
        downloaded later under Settings → Models) instead of trapping the
        user in the wizard.
        """
        with self._config_lock:
            self.config.setup_completed = True
            self.config.setup_version = SETUP_VERSION
            self.config.save()
        log.info("setup_skipped asr_model=%s (not installed yet)",
                 str(self.config.quality_asr_model or "").strip() or "<unset>")
        return {"setup_completed": True, "setup_version": SETUP_VERSION}

    def setup_maybe_auto_complete(self) -> bool:
        """Migration for existing installations.

        If the selected ASR model is already on disk, the wizard was never
        needed for this user: silently set ``setup_completed`` on first start
        so the main app appears instead of the wizard.
        """
        if self.config.setup_completed:
            return False
        model = str(self.config.quality_asr_model or "").strip()
        try:
            ready = bool(self._asr_engine(model).is_model_ready())
        except Exception:
            return False
        if not ready:
            return False
        with self._config_lock:
            self.config.setup_completed = True
            self.config.setup_version = SETUP_VERSION
            self.config.save()
        log.info("setup_auto_completed existing_model=%s", model)
        return True
