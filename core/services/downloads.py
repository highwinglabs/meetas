"""Background model downloads with live progress (first-run setup wizard).

A single worker thread downloads one model at a time; the UI polls
``download_status()`` for phase/bytes/speed. All existing security gates are
reused, never bypassed: the ASR path goes through the engine's
``download_with_progress`` (which calls ``require_network_action``) and the
Ollama path reuses ``install_ollama_model`` (loopback check + network gate).
"""
from __future__ import annotations

import threading
import time
from core.logging_setup import get_logger

log = get_logger("ma.service.downloads")

# Internal bookkeeping keys, stripped from the public status view.
_INTERNAL_KEYS = ("_file_base", "_last_bytes", "_last_total", "_last_t", "_g_bytes")


def _idle_state() -> dict:
    return {
        "state": "idle", "kind": None, "model": None, "phase": "",
        "downloaded_bytes": 0, "total_bytes": 0, "speed_bps": 0.0, "error": None,
    }


class DownloadsMixin:
    """Mix-in for MeetingService. State attributes are initialised there."""

    # --- status / control ---
    def download_status(self) -> dict:
        with self._downloads_lock:
            state = dict(self._download_state)
        for key in _INTERNAL_KEYS:
            state.pop(key, None)
        return state

    def _update_download(self, **fields) -> None:
        with self._downloads_lock:
            self._download_state.update(fields)

    def start_model_download(self, kind: str, model: str | None = None,
                             confirm: bool = False) -> dict:
        """Start one background model download (non-blocking; UI polls status)."""
        kind = str(kind or "").strip().lower()
        if kind not in ("asr", "ollama"):
            raise ValueError("Unbekannter Download-Typ. Erwartet: 'asr' oder 'ollama'.")
        model = str(model or "").strip() or (
            self.config.quality_asr_model if kind == "asr"
            else self.config.default_summary_model)
        model = str(model).strip()
        if not model:
            raise ValueError("Bitte ein Modell auswählen.")
        with self._downloads_lock:
            if self._download_state.get("state") == "running":
                raise ValueError("Es läuft bereits ein Modell-Download.")
            if kind == "asr":
                engine = self._asr_engine(model)
                if engine.is_model_ready():
                    raise ValueError(f"ASR-Modell '{model}' ist bereits installiert.")
            now = time.monotonic()
            self._download_state = {
                "state": "running", "kind": kind, "model": model, "phase": "start",
                "downloaded_bytes": 0, "total_bytes": 0, "speed_bps": 0.0, "error": None,
                "_file_base": 0, "_last_bytes": 0, "_last_total": 0,
                "_last_t": now, "_g_bytes": 0,
            }
        thread = threading.Thread(
            target=self._run_model_download, args=(kind, model, confirm),
            daemon=True, name="ma-model-download")
        with self._downloads_lock:
            self._download_thread = thread
        thread.start()
        return self.download_status()

    # --- worker ---
    def _run_model_download(self, kind: str, model: str, confirm: bool) -> None:
        try:
            if kind == "asr":
                self._update_download(phase="download")
                engine = self._asr_engine(model)
                # Every engine with download_with_progress (faster-whisper,
                # whisper-cpp) honours the confirmation gate and reports
                # byte progress; anything else falls back to prepare_model.
                if hasattr(engine, "download_with_progress"):
                    engine.download_with_progress(self._report_progress,
                                                  confirmed=confirm)
                    if not engine.is_model_ready():
                        raise RuntimeError(
                            f"ASR-Modell '{model}' ist nach dem Download "
                            f"nicht vorhanden.")
                else:
                    engine.prepare_model(allow_download=confirm)
            else:
                self._update_download(phase="pull")
                self.install_ollama_model(model, confirm=confirm,
                                          on_progress=self._report_progress)
            self._update_download(state="done", phase="done", speed_bps=0.0,
                                  error=None)
            log.info("model_download_done kind=%s model=%s", kind, model)
        except Exception as exc:  # report to the UI; the manager stays usable
            self._update_download(state="error", error=str(exc), speed_bps=0.0)
            log.warning("model_download_error kind=%s model=%s error=%s",
                        kind, model, exc)

    # --- progress accounting ---
    def _report_progress(self, downloaded_bytes: int, total_bytes: int) -> None:
        """Per-file (n, total) callback; the manager accumulates across files so
        the UI progress bar is monotonic and speed is measured globally."""
        downloaded_bytes = max(0, int(downloaded_bytes or 0))
        total_bytes = max(0, int(total_bytes or 0))
        now = time.monotonic()
        with self._downloads_lock:
            state = self._download_state
            last_bytes = int(state.get("_last_bytes") or 0)
            last_t = float(state.get("_last_t") or now)
            base = int(state.get("_file_base") or 0)
            if downloaded_bytes < last_bytes:
                # A new (smaller) file started; the previous file contributed
                # its reported total (or what we had seen of it).
                base += int(state.get("_last_total") or last_bytes)
                state["_file_base"] = base
            if total_bytes > 0:
                state["total_bytes"] = base + total_bytes
            state["downloaded_bytes"] = base + downloaded_bytes
            speed = (base + downloaded_bytes
                     - int(state.get("_g_bytes") or 0)) / max(0.05, now - last_t)
            state["speed_bps"] = round(max(0.0, speed), 1)
            state["_g_bytes"] = base + downloaded_bytes
            state["_last_bytes"] = downloaded_bytes
            state["_last_total"] = total_bytes
            state["_last_t"] = now
