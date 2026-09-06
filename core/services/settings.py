"""Device enumeration/preferences and user settings updates. (part of MeetingService)."""
from __future__ import annotations

import threading
from urllib.parse import urlsplit
from core.audio.devices import list_input_devices, resolve_input_device
from core.audio.enhancement import normalize_profiles
from core.config import LIVE_PERIOD_S_MAX, LIVE_PERIOD_S_MIN
from core.security.secrets import NetworkBlockedError, is_loopback_endpoint
from core.logging_setup import get_logger
from core.services._common import redact_endpoint

log = get_logger("ma.service")


class SettingsMixin:

    def devices(self) -> list[dict]:
        return [{"id": d.id, "name": d.name, "is_default": d.is_default,
                 "is_system_candidate": getattr(d, "is_system_candidate", False)}
                for d in list_input_devices()]

    def device_preference(self) -> dict:
        with self._config_lock:
            name = self.config.input_device_name
            index = self.config.input_device_index
        resolved = resolve_input_device(name, index)
        available = {d.id: d.name for d in list_input_devices()}
        return {"name": name,
                "index": index,
                "resolved_id": resolved,
                "resolved_name": available.get(resolved)}

    def update_device_preference(self, name: str | None = None,
                                 index: int | None = None) -> dict:
        if name is not None and not isinstance(name, str):
            raise ValueError("Der Gerätename muss Text sein.")
        if name is not None and len(name) > 256:
            raise ValueError("Der Gerätename ist zu lang.")
        if index is not None:
            if isinstance(index, bool):
                raise ValueError("Die Geräte-ID muss eine Zahl sein.")
            try:
                index = int(index)
            except (TypeError, ValueError) as exc:
                raise ValueError("Die Geräte-ID muss eine Zahl sein.") from exc
            if index < 0:
                raise ValueError("Die Geräte-ID darf nicht negativ sein.")
        clean_name = name.strip() if isinstance(name, str) and name.strip() else None
        with self._config_lock:
            self.config.input_device_name = clean_name
            self.config.input_device_index = index
            self.config.save()
        return self.device_preference()

    # --- user settings and projects --------------------------------------
    def settings(self) -> dict:
        """Return only safe, user-facing configuration values."""
        with self._config_lock:
            cfg = self.config
            return {
                "asr_model": cfg.asr_model,
                "live_asr_model": cfg.live_asr_model,
                "live_fallback_asr_model": cfg.live_fallback_asr_model,
                "quality_asr_model": cfg.quality_asr_model,
                "asr_language": cfg.asr_language or "auto",
                "analysis_language": cfg.analysis_language,
                "default_speaker_mode": cfg.default_speaker_mode,
                "default_analysis_template": cfg.default_analysis_template,
                "default_summary_model": cfg.default_summary_model,
                "quality_analysis_model": cfg.quality_analysis_model,
                "live_transcription": bool(cfg.live_transcription),
                "live_window_s": float(cfg.live_window_s),
                "live_period_s": float(cfg.live_period_s),
                "live_tail_s": float(cfg.live_tail_s),
                "speaker_diarization": bool(cfg.speaker_diarization),
                "auto_pipeline": bool(cfg.auto_pipeline),
                "auto_analyze": bool(cfg.auto_analyze),
                "embeddings_enabled": bool(cfg.embeddings_enabled),
                "rag_enabled": bool(cfg.rag_enabled),
                "system_audio_enabled": bool(cfg.system_audio_enabled),
                "mic_enhancement_enabled": bool(cfg.mic_enhancement_enabled),
                "audio_enhancement_profile": cfg.audio_enhancement_profile,
                "audio_enhancement_profiles": normalize_profiles(cfg.audio_enhancement_profiles),
                "llm_base_url": redact_endpoint(cfg.llm_base_url),
                "ollama_base_url": redact_endpoint(cfg.ollama_base_url),
                "llm_model": cfg.llm_model,
                "network_allowed": bool(cfg.network_allowed),
                "pipeline_max_workers": int(cfg.pipeline_max_workers),
                "input_device_name": cfg.input_device_name,
                "input_device_index": cfg.input_device_index,
            }

    def update_settings(self, values: dict, *, confirm_network_allowed: bool = False) -> dict:
        with self._config_lock:
            return self._update_settings_unlocked(
                values, confirm_network_allowed=confirm_network_allowed)

    def _update_settings_unlocked(self, values: dict, *,
                                  confirm_network_allowed: bool = False) -> dict:
        """Persist a constrained set of UI settings; never accepts secrets."""
        if not isinstance(values, dict):
            raise ValueError("Einstellungen müssen ein Objekt sein.")
        # Enabling external (non-loopback) providers is a security-posture
        # change. Require an explicit confirmation when turning it ON; turning
        # it OFF is always allowed and needs no confirmation.
        if "network_allowed" in values:
            if not isinstance(values["network_allowed"], bool):
                raise ValueError("network_allowed muss boolesch sein.")
            if values["network_allowed"] and not self.config.network_allowed \
                    and not confirm_network_allowed:
                raise NetworkBlockedError(
                    "'Externe Anbieter erlauben' aktivieren Sie ausdrücklich mit "
                    "confirm_network_allowed=true. Dadurch werden nicht-lokale "
                    "LLM-Endpunkte erlaubt.")
        allowed = {
            "asr_model", "live_asr_model", "live_fallback_asr_model",
            "quality_asr_model", "asr_language", "analysis_language",
            "default_speaker_mode", "default_analysis_template", "default_summary_model",
            "quality_analysis_model", "live_transcription", "speaker_diarization",
            "live_window_s", "live_period_s", "live_tail_s",
            "auto_pipeline", "auto_analyze", "embeddings_enabled", "rag_enabled",
            "system_audio_enabled", "llm_base_url", "ollama_base_url", "llm_model",
            "mic_enhancement_enabled",
            "audio_enhancement_profile", "audio_enhancement_profiles",
            "network_allowed",
            "pipeline_max_workers",
        }
        requested_network = bool(values.get("network_allowed", self.config.network_allowed))
        # Validate the related live-window values before mutating the config.
        # This prevents a rejected update from leaving the running process with
        # a half-applied, internally inconsistent setting.
        try:
            proposed_window = float(values.get("live_window_s", self.config.live_window_s))
            proposed_tail = float(values.get("live_tail_s", self.config.live_tail_s))
        except (TypeError, ValueError) as exc:
            raise ValueError("Live-Fenster und Live-Nachlauf müssen Zahlen sein.") from exc
        if not 6.0 <= proposed_window <= 60.0:
            raise ValueError("Das Live-Fenster muss zwischen 6 und 60 Sekunden liegen.")
        if not 0.5 <= proposed_tail <= 10.0:
            raise ValueError("Der Live-Nachlauf muss zwischen 0,5 und 10 Sekunden liegen.")
        if proposed_tail >= proposed_window:
            raise ValueError("Der Live-Nachlauf muss kürzer als das Live-Fenster sein.")
        # Pass 1: validate every value and compute the final changes WITHOUT
        # touching the running config. A rejected update (e.g. a blocked
        # external endpoint) must leave the process completely unchanged, not
        # half-applied.
        changes = {}
        for key, value in values.items():
            if key not in allowed:
                continue
            if key == "default_speaker_mode" and value not in ("off", "live", "after"):
                raise ValueError("Ungültiger Sprecher-Modus.")
            if key in {"asr_model", "live_asr_model", "live_fallback_asr_model",
                       "quality_asr_model", "default_summary_model",
                       "quality_analysis_model", "llm_model"}:
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > 256:
                    raise ValueError(f"{key} muss ein nichtleerer Modellname sein.")
                value = value.strip()
            if key == "default_analysis_template" and value not in {
                    "standard", "compact", "audit", "action_items"}:
                raise ValueError("Ungültige Vorlage für KI-Auswertungen.")
            if key == "asr_language" and value not in (None, "", "auto", "de", "en"):
                raise ValueError("Ungültige Sprache.")
            if key == "analysis_language" and value not in (
                    None, "", "wie_transkript", "de", "en"):
                raise ValueError("Ungültige Auswertungssprache.")
            if key in {"live_transcription", "speaker_diarization", "auto_pipeline", "auto_analyze",
                       "embeddings_enabled", "rag_enabled", "system_audio_enabled",
                       "network_allowed", "mic_enhancement_enabled"}:
                if not isinstance(value, bool):
                    raise ValueError(f"{key} muss boolesch sein.")
            if key == "audio_enhancement_profile":
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
                    raise ValueError("Das Audio-Profil muss einen gültigen Namen haben.")
                value = value.strip()
            if key == "audio_enhancement_profiles":
                value = normalize_profiles(value)
            if key == "pipeline_max_workers":
                try:
                    value = max(1, min(4, int(value)))
                except (TypeError, ValueError) as exc:
                    raise ValueError("pipeline_max_workers muss eine Zahl sein.") from exc
            if key == "live_window_s":
                value = proposed_window
            if key == "live_period_s":
                try:
                    value = max(LIVE_PERIOD_S_MIN, min(LIVE_PERIOD_S_MAX, float(value)))
                except (TypeError, ValueError) as exc:
                    raise ValueError("live_period_s muss eine Zahl sein.") from exc
            if key == "live_tail_s":
                value = proposed_tail
            if key in {"llm_base_url", "ollama_base_url"}:
                value = str(value or "").strip()
                if not value:
                    raise ValueError(f"{key} darf nicht leer sein.")
                try:
                    parsed_url = urlsplit(value)
                except ValueError as exc:
                    raise ValueError(f"{key} ist keine gültige URL.") from exc
                if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                    raise ValueError(f"{key} muss eine HTTP(S)-URL mit Host sein.")
                if parsed_url.username or parsed_url.password:
                    raise ValueError(f"{key} darf keine Zugangsdaten in der URL enthalten.")
                if not is_loopback_endpoint(value) and not requested_network:
                    raise NetworkBlockedError(
                        "Externe LLM-Endpunkte sind blockiert. Aktiviere zuerst "
                        "'externe Anbieter erlauben'.")
            if key == "asr_language" and value in ("", "auto"):
                value = None
            changes[key] = value
        if "audio_enhancement_profile" in changes:
            available_profiles = changes.get(
                "audio_enhancement_profiles",
                normalize_profiles(self.config.audio_enhancement_profiles))
            if changes["audio_enhancement_profile"] not in available_profiles:
                raise ValueError("Das gewählte Audio-Profil ist nicht vorhanden.")
        # Pass 2: apply atomically, only after every value validated.
        for key, value in changes.items():
            setattr(self.config, key, value)
        # A pipeline_max_workers change must take effect on the running pool
        # immediately: rebuild the concurrency gate (in-flight work keeps its
        # old slot; newly scheduled work honours the new limit).
        if "pipeline_max_workers" in changes:
            self._pipeline_gate = threading.BoundedSemaphore(
                max(1, int(self.config.pipeline_max_workers)))
        self.config.save()
        self._providers.clear_cache()
        self._llm_providers.clear_cache()
        self._embed_manager.clear_cache()
        return self.settings()
