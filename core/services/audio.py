"""Audio access: enhancement scheduling, waveform, previews, reprocessing. (part of MeetingService)."""
from __future__ import annotations

import numpy as np
import json
import math
import os
import wave
import time as time_module
import uuid
from pathlib import Path
from sqlalchemy import select
from core.audio.enhancement import enhance_audio, normalize_profile, normalize_profiles, pcm_channel_values
from core.store.db import session_scope
from core.store.models import Meeting, Recording, utcnow
from core.logging_setup import get_logger
from core.services._common import utc_iso, UnknownMeetingError

log = get_logger("ma.service")


class AudioMixin:

    def audio_path(self, meeting_id: str, variant: str = "enhanced") -> Path:
        """Return a local recording, preferring the enhanced copy when ready."""
        if variant not in {"original", "enhanced"}:
            raise ValueError("Unbekannte Audio-Version.")
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            recording = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
            raw = recording.original_path if recording else None
        if not raw:
            raise ValueError("Für dieses Meeting ist keine Audioaufnahme vorhanden.")
        path = Path(raw).resolve()
        root = self.config.audio_dir.resolve()
        if root not in path.parents:
            raise ValueError("Die Audiodatei liegt nicht im lokalen Audio-Speicher.")
        if not path.is_file():
            raise ValueError("Die Audioaufnahme wurde nicht gefunden.")
        if variant == "enhanced":
            enhanced = path.with_name("enhanced.wav").resolve()
            if root in enhanced.parents and enhanced.is_file():
                return enhanced
        return path

    def schedule_audio_enhancement(self, meeting_id: str, original_path: str | None,
                                   source: str = "mic", force: bool = False,
                                   profile_override: dict | None = None,
                                   noise_profile_start_s: float | None = None,
                                   noise_profile_end_s: float | None = None,
                                   noise_profile_mode: str = "automatic") -> bool:
        """Create the enhanced sibling WAV in the background, once per meeting."""
        if not original_path or source == "system":
            return False
        with self._config_lock:
            profile_name = self.config.audio_enhancement_profile
            profile = normalize_profile(profile_override) if profile_override is not None else normalize_profiles(self.config.audio_enhancement_profiles).get(profile_name)
            enabled = bool(self.config.mic_enhancement_enabled)
        if (not enabled and profile_override is None) or profile is None:
            return False
        if profile_override is not None:
            profile_name = "Benutzerdefiniert"
        with self._audio_enhancement_lock:
            if meeting_id in self._audio_enhancement_inflight:
                return False
            self._audio_enhancement_inflight.add(meeting_id)
            self._audio_enhancement_errors.pop(meeting_id, None)

        original = Path(original_path).resolve()
        output = original.with_name("enhanced.wav")
        metadata = output.with_name("enhanced.json")
        metadata_tmp = metadata.with_name(metadata.name + ".tmp")

        def work() -> None:
            try:
                enhance_audio(
                    original, output, profile, source=source, force=force,
                    noise_profile_mode=noise_profile_mode,
                    noise_profile_start_s=noise_profile_start_s,
                    noise_profile_end_s=noise_profile_end_s)
                metadata_tmp.write_text(json.dumps({
                    "profile_name": profile_name,
                    "profile": profile,
                    "noise_profile_mode": noise_profile_mode,
                    "noise_profile_start_s": noise_profile_start_s,
                    "noise_profile_end_s": noise_profile_end_s,
                    "created_at": utc_iso(utcnow()),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(metadata_tmp, metadata)
                try:
                    os.chmod(metadata, 0o444)
                except OSError:
                    pass
                with self._audio_enhancement_lock:
                    self._audio_enhancement_errors.pop(meeting_id, None)
                log.info("audio_enhancement_ready meeting=%s profile=%s",
                         meeting_id[:8], profile_name)
            except Exception as exc:
                with self._audio_enhancement_lock:
                    self._audio_enhancement_errors[meeting_id] = str(exc)[:500]
                try:
                    metadata_tmp.write_text(json.dumps({
                        "status": "failed",
                        "error": str(exc)[:500],
                        "profile_name": profile_name,
                        "created_at": utc_iso(utcnow()),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(metadata_tmp, metadata)
                except OSError:
                    log.debug("audio_enhancement_error_metadata_failed meeting=%s",
                              meeting_id[:8], exc_info=True)
                log.exception("audio_enhancement_failed meeting=%s", meeting_id[:8])
                try:
                    metadata_tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            finally:
                with self._audio_enhancement_lock:
                    self._audio_enhancement_inflight.discard(meeting_id)

        try:
            self._audio_enhancement_executor.submit(work)
        except Exception:
            with self._audio_enhancement_lock:
                self._audio_enhancement_inflight.discard(meeting_id)
            return False
        return True

    def _audio_enhancement_info(self, meeting_id: str, original_path: str | None,
                                source: str = "mic") -> dict:
        """Return the user-facing state of the enhanced sibling file."""
        if not original_path or source == "system":
            return {"status": "unavailable", "current": False}
        original = Path(original_path).resolve()
        enhanced = original.with_name("enhanced.wav")
        with self._audio_enhancement_lock:
            if meeting_id in self._audio_enhancement_inflight:
                return {"status": "processing", "current": False}
            error = self._audio_enhancement_errors.get(meeting_id)
        if error:
            return {"status": "failed", "current": False, "error": error}
        if not enhanced.is_file():
            return {"status": "pending", "current": False}
        metadata_path = enhanced.with_name("enhanced.json")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            metadata = {}
        if metadata.get("status") == "failed":
            return {
                "status": "failed",
                "current": False,
                "error": metadata.get("error") or "Audioverbesserung fehlgeschlagen.",
            }
        with self._config_lock:
            selected_name = self.config.audio_enhancement_profile
            selected = normalize_profiles(self.config.audio_enhancement_profiles).get(selected_name)
        applied_profile = metadata.get("profile")
        current = None if not isinstance(applied_profile, dict) else bool(
            selected and applied_profile == selected)
        return {
            "status": "ready",
            "current": current,
            "profile_name": metadata.get("profile_name"),
            "profile": applied_profile if isinstance(applied_profile, dict) else None,
            "created_at": metadata.get("created_at"),
            "noise_profile_mode": metadata.get("noise_profile_mode"),
            "noise_profile_start_s": metadata.get("noise_profile_start_s"),
            "noise_profile_end_s": metadata.get("noise_profile_end_s"),
        }

    def reprocess_audio(self, meeting_id: str, profile: dict | None = None,
                        noise_profile_mode: str = "disabled",
                        noise_profile_start_s: float | None = None,
                        noise_profile_end_s: float | None = None) -> dict:
        """Reapply the currently selected profile to an existing recording."""
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            recording = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
            original_path = recording.original_path if recording else None
            source = recording.source if recording else "mic"
        if not original_path:
            raise ValueError("Für dieses Meeting ist keine Audioaufnahme vorhanden.")
        if source == "system":
            raise ValueError("Systemaudio wird nicht automatisch verbessert.")
        if noise_profile_mode not in {"disabled", "automatic", "manual"}:
            raise ValueError("Unbekannter Rauschprofil-Modus.")
        if noise_profile_mode == "manual" and (noise_profile_start_s is None or noise_profile_end_s is None):
            raise ValueError("Für das manuelle Rauschprofil muss ein Bereich ausgewählt werden.")
        if noise_profile_mode == "disabled" and (noise_profile_start_s is not None or noise_profile_end_s is not None):
            raise ValueError("Ein Rauschprofil ist deaktiviert.")
        if noise_profile_start_s is not None or noise_profile_end_s is not None:
            if noise_profile_start_s is None or noise_profile_end_s is None:
                raise ValueError("Für das Rauschprofil müssen Anfang und Ende angegeben werden.")
            if float(noise_profile_end_s) <= float(noise_profile_start_s):
                raise ValueError("Das Rauschprofil muss einen gültigen Zeitbereich enthalten.")
        if not self.schedule_audio_enhancement(
                meeting_id, original_path, source, force=True,
                profile_override=profile,
                noise_profile_mode=noise_profile_mode,
                noise_profile_start_s=noise_profile_start_s,
                noise_profile_end_s=noise_profile_end_s):
            raise ValueError("Die Audioverbesserung ist deaktiviert oder läuft bereits.")
        return {
            "status": "scheduled",
            "profile": "Benutzerdefiniert" if profile is not None else self.config.audio_enhancement_profile,
            "noise_profile": (None if noise_profile_start_s is None else {
                "start_s": float(noise_profile_start_s),
                "end_s": float(noise_profile_end_s),
            }),
        }

    def audio_waveform(self, meeting_id: str, points: int = 240,
                       start_s: float = 0.0,
                       duration_s: float | None = None,
                       variant: str = "original") -> dict:
        """Return a compact peak waveform without loading the whole WAV in RAM."""
        path = self.audio_path(meeting_id, variant)
        points = max(60, min(1000, int(points)))
        try:
            with wave.open(str(path), "rb") as wav:
                rate = max(1, wav.getframerate())
                channels = max(1, wav.getnchannels())
                width = wav.getsampwidth()
                total = wav.getnframes()
                start_frame = min(total, max(0, int(float(start_s) * rate)))
                end_frame = total if duration_s is None else min(
                    total, start_frame + max(1, int(float(duration_s) * rate)))
                wav.setpos(start_frame)
                visible_frames = max(1, end_frame - start_frame)
                frames_per_point = max(1, math.ceil(visible_frames / points))
                peaks: list[float] = []
                remaining = visible_frames
                for _ in range(points):
                    if remaining <= 0:
                        break
                    raw = wav.readframes(frames_per_point)
                    if not raw:
                        break
                    values = pcm_channel_values(raw, width, channels)
                    if values.size == 0:
                        break
                    peaks.append(round(float(np.max(np.abs(values))), 4))
                    remaining -= len(values)
        except (OSError, wave.Error, ValueError) as exc:
            raise ValueError("Die Wellenform konnte nicht gelesen werden.") from exc
        full_duration = total / rate
        visible_start = start_frame / rate
        visible_end = end_frame / rate
        return {
            "duration_s": round(full_duration, 3),
            "view_start_s": round(visible_start, 3),
            "view_duration_s": round(max(0.0, visible_end - visible_start), 3),
            "peaks": peaks,
        }

    def create_audio_preview(self, meeting_id: str, start_s: float = 0.0,
                             duration_s: float = 12.0, profile: dict | None = None,
                             noise_profile_mode: str = "disabled",
                             noise_profile_start_s: float | None = None,
                             noise_profile_end_s: float | None = None) -> dict:
        """Render a short, unsaved A/B preview with the requested settings."""
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            recording = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
            source = recording.source if recording else "mic"
            original_path = recording.original_path if recording else None
        if source == "system":
            raise ValueError("Systemaudio kann nicht als Sprachvorschau bearbeitet werden.")
        original = self.audio_path(meeting_id, "original")
        clean_profile = normalize_profile(profile or {})
        if noise_profile_mode not in {"disabled", "automatic", "manual"}:
            raise ValueError("Unbekannter Rauschprofil-Modus.")
        if noise_profile_mode == "manual" and (noise_profile_start_s is None or noise_profile_end_s is None):
            raise ValueError("Für das manuelle Rauschprofil muss ein Bereich ausgewählt werden.")
        if noise_profile_mode == "disabled" and (noise_profile_start_s is not None or noise_profile_end_s is not None):
            raise ValueError("Ein Rauschprofil ist deaktiviert.")
        token = uuid.uuid4().hex
        preview = original.with_name(f"preview-{token}.wav")
        enhance_audio(original, preview, clean_profile, source=source, force=True,
                      noise_profile_mode=noise_profile_mode,
                      start_s=max(0.0, float(start_s)), duration_s=float(duration_s),
                      noise_profile_start_s=noise_profile_start_s,
                      noise_profile_end_s=noise_profile_end_s)
        with self._audio_preview_lock:
            now = time_module.time()
            for old_token, (_, old_path, expires) in list(self._audio_previews.items()):
                if expires <= now:
                    self._audio_previews.pop(old_token, None)
                    try:
                        old_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            self._audio_previews[token] = (meeting_id, preview, now + 600.0)
        return {"token": token, "duration_s": min(30.0, max(1.0, float(duration_s)))}

    def audio_preview_path(self, meeting_id: str, token: str) -> Path:
        with self._audio_preview_lock:
            item = self._audio_previews.get(token)
            if item is None or item[0] != meeting_id or item[2] <= time_module.time():
                raise ValueError("Die Audio-Vorschau ist nicht mehr verfügbar.")
            path = item[1]
        if not path.is_file():
            raise ValueError("Die Audio-Vorschau wurde nicht gefunden.")
        return path
