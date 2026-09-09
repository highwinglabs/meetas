"""ASR model status, model downloads and benchmarking. (part of MeetingService)."""
from __future__ import annotations

import json
import os
import re
import wave
import time as time_module
import urllib.request
from sqlalchemy import select
from core.providers import ASRError
from core.providers.faster_whisper import FasterWhisperEngine
from core.security.secrets import NetworkBlockedError, is_loopback_endpoint
from core.store.db import session_scope
from core.transcribe.processor import pick_asr_audio
from core.store.models import Meeting, Recording
from core.logging_setup import get_logger
from core.services._common import _NO_REDIRECT_OPENER, UnknownMeetingError

log = get_logger("ma.service")


def _peak_rss_mb() -> float | None:
    """Peak resident-set size of this process in MB, or None when unavailable.

    ``resource`` is a POSIX-only standard-library module; importing it at module
    top would crash on non-POSIX platforms even though ``benchmark_asr`` is the
    only caller. Import it lazily and fall back to None when the import fails
    (L8).
    """
    try:
        import resource  # POSIX-only stdlib module
    except ImportError:
        return None
    try:
        return round(float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024, 1)
    except (AttributeError, ValueError, OSError):
        return None


class ModelsMixin:

    # --- Phase 2: ASR / search / export ---
    def asr_model_status(self, model_name: str | None = None) -> dict:
        engine = self._asr_engine(model_name)
        ready = engine.is_model_ready()
        note = getattr(engine, "model_size_note", lambda: "")()
        return {
            "name": engine.model_name,
            "ready": ready,
            "size": note or None,
            "network_allowed": self.config.network_allowed,
            "downloads_require_confirmation": self.config.downloads_require_confirmation,
        }

    def download_asr_model(self, confirm: bool = False,
                           model_name: str | None = None) -> dict:
        engine = self._asr_engine(model_name)
        engine.prepare_model(allow_download=confirm)
        return {"name": engine.model_name, "ready": engine.is_model_ready()}

    def install_ollama_model(self, model_name: str, confirm: bool = False,
                             on_progress=None) -> dict:
        """Pull a named model into the local Ollama catalog after confirmation.

        ``on_progress`` optionally receives ``(completed_bytes, total_bytes)``
        while the pull stream reports progress; the endpoint contract is
        unchanged when it is omitted.
        """
        model_name = str(model_name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", model_name):
            raise ValueError("Ungültiger Ollama-Modellname.")
        base = str(self.config.ollama_base_url or "").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        if not base or not is_loopback_endpoint(base):
            raise NetworkBlockedError(
                "Zum Installieren muss ein lokaler Ollama-Endpunkt konfiguriert sein.")
        from core.security.secrets import require_network_action
        require_network_action(
            f"download des Ollama-Modells '{model_name}'", self.config, confirmed=confirm)
        request = urllib.request.Request(
            base + "/api/pull",
            data=json.dumps({"name": model_name, "stream": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=30) as response:
                last_status = ""
                for raw_line in response:
                    if not raw_line.strip():
                        continue
                    payload = json.loads(raw_line.decode("utf-8"))
                    if payload.get("error"):
                        raise ValueError(str(payload["error"]))
                    last_status = str(payload.get("status") or last_status)
                    if on_progress is not None:
                        completed = payload.get("completed")
                        total = payload.get("total")
                        if (isinstance(completed, (int, float))
                                and isinstance(total, (int, float))):
                            on_progress(int(completed), int(total))
                if last_status != "success":
                    raise ValueError("Ollama hat die Modellinstallation nicht bestätigt.")
        except (OSError, ValueError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError("Ollama war nicht erreichbar oder der Download ist fehlgeschlagen.") from exc
        return {"name": model_name, "installed": True}

    def benchmark_asr(self, model_name: str, meeting_id: str | None = None) -> dict:
        """Measure an installed ASR model on an existing local recording.

        This is deliberately read-only: it never creates transcript rows and
        never downloads a model. A reference transcript is not assumed, so the
        result reports technical measurements and the raw recognition only.
        """
        model_name = (model_name or "").strip()
        if not model_name:
            raise ValueError("Bitte ein ASR-Modell auswählen.")
        with session_scope() as s:
            if meeting_id:
                meeting = s.get(Meeting, meeting_id)
                if meeting is None or meeting.deleted_at is not None:
                    raise UnknownMeetingError(meeting_id)
                rec = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
            else:
                rec = s.scalars(select(Recording).where(
                    Recording.original_path.is_not(None)).order_by(
                        Recording.created_at.desc())).first()
                meeting = s.get(Meeting, rec.meeting_id) if rec else None
            raw_path = rec.original_path if rec else None
        if not raw_path:
            raise ASRError("Keine lokale Aufnahme für den Benchmark vorhanden.")
        audio_path = pick_asr_audio(raw_path).resolve()
        audio_root = self.config.audio_dir.resolve()
        if audio_root not in audio_path.parents or not audio_path.is_file():
            raise ASRError("Die Benchmark-Aufnahme liegt nicht im lokalen Audio-Speicher.")

        engine = self._asr_engine(model_name)
        engine.prepare_model(allow_download=False)
        duration_s = None
        try:
            with wave.open(str(audio_path), "rb") as wav:
                duration_s = round(wav.getnframes() / max(1, wav.getframerate()), 3)
        except (OSError, wave.Error):
            pass
        wall_start = time_module.perf_counter()
        cpu_start = time_module.process_time()
        segments = engine.transcribe(audio_path, language=None)
        wall_s = round(time_module.perf_counter() - wall_start, 3)
        cpu_s = round(time_module.process_time() - cpu_start, 3)
        rss_mb = _peak_rss_mb()
        realtime = round(wall_s / duration_s, 3) if duration_s else None
        return {
            "model": engine.model_name,
            "meeting_id": meeting.id if meeting else None,
            "meeting_title": meeting.title if meeting else None,
            "audio_seconds": duration_s,
            "processing_seconds": wall_s,
            "cpu_seconds": cpu_s,
            "cpu_utilization_percent": round((cpu_s / wall_s) * 100.0, 1) if wall_s else 0.0,
            "cpu_utilization_host_percent": round(
                (cpu_s / wall_s) * 100.0 / max(1, os.cpu_count() or 1), 2) if wall_s else 0.0,
            "realtime_factor": realtime,
            "live_possible": bool(realtime is not None and realtime <= 1.0),
            "ram_max_mb_process": rss_mb,
            "gpu": "nicht verwendet (CPU-Benchmark; AMD-GPU nicht automatisch gemessen)",
            "reference_quality": "nicht bewertet – kein Referenztranskript angegeben",
            "language": segments[0].language if segments else None,
            "segments": [{"start_s": s.start_s, "end_s": s.end_s,
                          "text": s.text, "language": s.language}
                         for s in segments],
        }
