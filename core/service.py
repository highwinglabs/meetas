"""High-level meeting service (facade).

Wires capture, persistence, recovery and device enumeration together and
enforces the "at most one active meeting" invariant. DB access uses
short-lived sessions so it is safe across the capture worker thread.

The business logic is organised as one mixin per domain in
core/services/ (meetings, audio, projects, uploads, search, pipelines,
tasks, ...). This module keeps the core wiring: dependency injection,
the recording lifecycle (start/pause/resume/stop/status) and consent.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional
from sqlalchemy import select
from core.audio.capture import CaptureSession, CaptureStatus
from core.audio.devices import resolve_input_device, resolve_system_audio_device, default_input_rate
from core.audio.stream import AudioSource, CombinedSource, LiveSource, probe_portaudio, try_rates
from core.audio.assembly import verify_original
from core import i18n
from core.analysis.processor import AnalysisProcessor
from core.backup import create_backup
from core.config import Config
from core.jobs.queue import JobQueue
from core.live.engine import FasterWhisperLiveEngine, LiveASREngine, ParakeetLiveEngine
from core.live.pipeline import LiveTranscriptionPipeline
from core.llm import LLMEngine, LLMProviderManager
from core.search.embeddings import EmbeddingManager
from core.providers import ASREngine, ProviderManager, ParakeetEngine
from core.providers.diar import DiarizationEngine, NumpyDiarizationEngine, PyannoteDiarizationEngine
from core.providers.faster_whisper import FasterWhisperEngine
from core.recovery.recovery import active_meeting
from core.store.db import session_scope
from core.transcribe.processor import TranscriptionProcessor
from core.store.models import ConsentEvent, Meeting, Recording, TranscriptSegment, Project, utcnow
from core.services.analytics import AnalyticsMixin
from core.services.audio import AudioMixin
from core.services.backups import BackupsMixin
from core.services.downloads import DownloadsMixin, _idle_state
from core.services.markers import MarkersMixin
from core.services.meetings import MeetingsMixin
from core.services.models import ModelsMixin
from core.services.pipelines import PipelineMixin
from core.services.projects import ProjectsMixin
from core.services.search import SearchMixin
from core.services.setup import SetupMixin
from core.services.settings import SettingsMixin
from core.services.speakers import SpeakersMixin
from core.services.system import SystemMixin
from core.services.tasks import TasksMixin
from core.services.transcribe import TranscriptionMixin
from core.services.uploads import UploadsMixin
# Re-exported for backward compatibility (module-level helpers).
from core.services._common import (  # noqa: F401
    ActiveMeetingError,
    ConsentRequiredError,
    SpeakerMergeConflictError,
    UnknownMeetingError,
    UnknownTaskError,
    _NO_REDIRECT_OPENER,
    _analysis_markdown,
    berlin_date,
    parse_utc_datetime,
    redact_endpoint,
    utc_iso,
    _task_display_status,
)

from core.logging_setup import get_logger

log = get_logger("ma.service")

# Builds the audio source for a meeting capture (device_id, sample_rate, channels).
SourceFactory = Callable[[Optional[int], int, int], AudioSource]


class MeetingService(
    MeetingsMixin, AudioMixin, SettingsMixin, ProjectsMixin, UploadsMixin,
    TranscriptionMixin, ModelsMixin, SpeakersMixin, SearchMixin,
    AnalyticsMixin, BackupsMixin, SystemMixin, PipelineMixin, TasksMixin,
    MarkersMixin, DownloadsMixin, SetupMixin,
):
    def __init__(self, config: Config, source_factory: SourceFactory | None = None,
                 asr_engine: ASREngine | None = None,
                 llm_engine: LLMEngine | None = None,
                 live_engine: LiveASREngine | None = None,
                 diar_engine: DiarizationEngine | None = None):
        self.config = config
        self._source_factory = source_factory or self._default_source_factory
        self._sessions: dict[str, CaptureSession] = {}
        self._live_pipelines: dict[str, LiveTranscriptionPipeline] = {}
        # Guards the _sessions/_live_pipelines dicts against concurrent
        # mutation (API handler threads vs. capture/pipeline threads).
        self._state_lock = threading.Lock()
        # Serialises configuration snapshots and updates.  Settings are read by
        # API handlers as well as background capture/pipeline workers; a plain
        # sequence of attribute assignments can otherwise expose a mixed
        # configuration halfway through an update.
        self._config_lock = threading.RLock()
        # First-run setup wizard: one background model download at a time.
        self._downloads_lock = threading.Lock()
        self._download_state = _idle_state()
        self._download_thread: threading.Thread | None = None
        # Admission lock closes the check-then-insert race between two API
        # callers starting a meeting at the same time. SQLite transactions
        # alone cannot prevent both readers from observing no active row.
        self._meeting_start_guard = threading.Lock()
        # Serialises the check-and-import critical section of complete_upload so
        # a double-completion can never create two meetings for one upload.
        self._upload_complete_guard = threading.Lock()
        # Chunk writes, cancellation and completion must be serialized per
        # process; otherwise two requests with the same offset can append bytes
        # twice while the database records only one of them.
        self._upload_io_guard = threading.RLock()
        self._providers = ProviderManager(config)
        self._asr_override = asr_engine  # injected (tests) or None -> provider manager
        self._processor = TranscriptionProcessor(config)
        self._llm_providers = LLMProviderManager(config)
        self._llm_override = llm_engine  # injected (tests/dev) or None -> provider manager
        self._live_override = live_engine  # injected (tests) or None -> faster-whisper live
        self._diar_override = diar_engine  # injected (tests) or None -> numpy diarizer
        self._analysis_processor = AnalysisProcessor(config)
        # Active LLM analyses: meeting_id -> (token, cancel_event, engine). The
        # token guards against a restarted analysis being clobbered by the
        # (still-finishing) previous one.
        self._analysis_tokens: dict[str, int] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self._analysis_engines: dict[int, LLMEngine] = {}
        self._analysis_lock = threading.Lock()
        self._embed_manager = EmbeddingManager(config)
        # P3: non-blocking post-stop pipeline runner. One worker per meeting at
        # a time (guarded below). The executor is a fixed, bounded worker pool;
        # the *effective* concurrency is enforced by a semaphore gate so a
        # runtime change to ``pipeline_max_workers`` takes effect immediately
        # without recreating the executor (which would strand in-flight work).
        self._pipeline_executor = ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="ma-pipeline")
        self._pipeline_inflight: set[str] = set()
        self._pipeline_lock = threading.Lock()
        self._pipeline_gate = threading.BoundedSemaphore(
            max(1, int(getattr(config, "pipeline_max_workers", 1))))
        # Audio enhancement is deliberately isolated from transcription: a
        # slow denoise pass must not delay the transcript pipeline.
        self._audio_enhancement_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="ma-audio-enhance")
        self._audio_enhancement_inflight: set[str] = set()
        self._audio_enhancement_errors: dict[str, str] = {}
        self._audio_enhancement_lock = threading.Lock()
        self._audio_previews: dict[str, tuple[str, Path, float]] = {}
        self._audio_preview_lock = threading.Lock()
        self._manual_transcriptions: set[str] = set()
        # Serialize concurrent ASR requests for the same meeting. A direct
        # API/CLI call must not race an asynchronous retry and replace rows
        # while the other run is still writing them.
        self._transcription_locks: dict[str, threading.Lock] = {}
        self._transcription_locks_guard = threading.Lock()
        self._maybe_auto_backup()
        self._backfill_transcript_versions()

    def _maybe_auto_backup(self) -> None:
        """Best-effort local auto-backup at startup (local-only, never fatal)."""
        if not self.config.backup_enabled:
            return
        try:
            if self._backup_due():
                create_backup(self.config, kind="db", note="auto")
        except Exception as e:  # noqa: BLE001 - a backup must never crash startup
            log.warning("auto_backup_failed error=%s", e)

    # --- defaults ---
    @staticmethod
    def _default_source_factory(device_id, sr, ch) -> AudioSource:
        return LiveSource(device_id, sr, ch)

    def _asr_engine(self, model_name: str | None = None) -> ASREngine:
        return self._asr_override or self._providers.engine(model_name)

    def _llm_engine(self) -> LLMEngine:
        return self._llm_override or self._llm_providers.engine()

    def _live_engine(self, model_name: str | None = None) -> LiveASREngine | None:
        """The live ASR engine, or None when live is not usable.

        An injected engine (tests) is always used. Otherwise we wrap the
        faster-whisper engine; a non-whisper engine (e.g. the batch mock) has no
        in-memory transcribe, so live is unavailable rather than crashing."""
        if self._live_override is not None:
            return self._live_override
        requested = model_name or self.config.live_asr_model
        if requested.lower().startswith("parakeet"):
            parakeet = ParakeetEngine(model_name=requested, config=self.config)
            if parakeet.is_model_ready():
                return ParakeetLiveEngine(parakeet)
            # The optional Parakeet runtime is not installed on most CPU-only
            # systems. Use a real, installed Whisper fallback if available;
            # the UI exposes the effective engine so this is never silent.
            requested = self.config.live_fallback_asr_model or "small"
        base = self._asr_engine(requested)
        if isinstance(base, FasterWhisperEngine):
            label = "faster-whisper-live"
            if (model_name or self.config.live_asr_model).lower().startswith("parakeet"):
                label = f"faster-whisper-live-fallback:{requested}"
            return FasterWhisperLiveEngine(base, language=self.config.asr_language,
                                           display_name=label)
        return None

    def _normalise_meeting_settings(self, settings: dict | None) -> dict:
        """Keep only user-facing, JSON-safe per-meeting overrides."""
        raw = settings if isinstance(settings, dict) else {}
        allowed = {
            "live_transcription", "live_asr_model", "live_fallback_asr_model",
            "asr_model", "quality_asr_model",
            "language", "speaker_mode", "analysis_enabled", "analysis_model",
            "analysis_template", "analysis_language", "embeddings_enabled",
            "source", "system_device_id", "device_name",
        }
        out = {k: raw[k] for k in allowed if k in raw}
        for key in {"live_transcription", "analysis_enabled", "embeddings_enabled"}:
            if key in out and not isinstance(out[key], bool):
                out.pop(key, None)
        for key in {"live_asr_model", "live_fallback_asr_model", "asr_model", "quality_asr_model",
                    "analysis_model", "analysis_template", "device_name"}:
            if key in out:
                if not isinstance(out[key], str) or len(out[key]) > 256:
                    out.pop(key, None)
                else:
                    out[key] = out[key].strip()
        if "system_device_id" in out and (
                isinstance(out["system_device_id"], bool)
                or not isinstance(out["system_device_id"], int)
                or out["system_device_id"] < 0):
            out.pop("system_device_id", None)
        if out.get("speaker_mode") not in (None, "off", "live", "after"):
            out.pop("speaker_mode", None)
        if out.get("language") not in (None, "", "auto", "de", "en"):
            out.pop("language", None)
        if out.get("analysis_language") not in (
                None, "", "wie_transkript", "de", "en"):
            out.pop("analysis_language", None)
        return out

    def _meeting_settings(self, meeting_id: str) -> dict:
        with session_scope() as s:
            m = s.get(Meeting, meeting_id)
            if m is None or m.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            try:
                value = json.loads(m.settings_json or "{}")
            except (TypeError, json.JSONDecodeError):
                value = {}
            return self._normalise_meeting_settings(value if isinstance(value, dict) else {})

    def _diar_engine(self) -> DiarizationEngine:
        if self._diar_override is not None:
            return self._diar_override
        if (self.config.diarization_backend or "numpy").strip().lower() == "pyannote":
            return PyannoteDiarizationEngine(model_id=self.config.pyannote_model)
        return NumpyDiarizationEngine()

    # --- consent ---
    def consent_text(self) -> str:
        # Localise only the built-in German notice (or an empty one) for the
        # requesting language; a user-customised notice is returned verbatim.
        if (self.config.consent_text or "").strip() in ("", i18n.DEFAULT_CONSENT_DE.strip()):
            return i18n.consent_text_for_language(i18n.current_language.get())
        return self.config.consent_text

    def consent_acknowledged(self) -> bool:
        with session_scope() as s:
            ev = s.scalars(select(ConsentEvent).where(
                ConsentEvent.acknowledged.is_(True)).order_by(ConsentEvent.ts.desc())
            ).first()
            return ev is not None

    def record_consent(self, acknowledged: bool = True) -> None:
        with session_scope() as s:
            s.add(ConsentEvent(acknowledged=acknowledged, text=self.config.consent_text))

    # --- meetings ---
    def start_meeting(self, title: str = "Neues Meeting", device_id: int | None = None,
                      source: str = "mic", consent_ack: bool = False,
                      project_id: str | None = None,
                      settings: dict | None = None) -> str:
        if not isinstance(title, str):
            raise ValueError("Der Meetingtitel muss Text sein.")
        title = title.strip() or "Neues Meeting"
        if len(title) > 512:
            raise ValueError("Der Meetingtitel ist zu lang.")
        if not consent_ack and not self.consent_acknowledged():
            raise ConsentRequiredError(
                "Bitte bestätigen Sie zunächst den Einwilligungs- und Datenschutzhinweis."
            )
        if source not in ("mic", "system", "both"):
            raise ValueError("Unbekannte Audioquelle.")
        if device_id is not None and (isinstance(device_id, bool)
                                      or not isinstance(device_id, int) or device_id < 0):
            raise ValueError("Ungültige Mikrofon-Geräte-ID.")
        # Resolve the capture source/device. System-audio (loopback /
        # what-you-hear) is OFF by default and only used when the user enables it;
        # without a matching device it reports itself unavailable, never crashes.
        raw_settings = settings if isinstance(settings, dict) else {}
        requested_device_name = raw_settings.get("device_name")
        if not isinstance(requested_device_name, str) or not requested_device_name.strip():
            requested_device_name = None
        explicit_system_id = raw_settings.get("system_device_id")
        if isinstance(explicit_system_id, bool) or not isinstance(explicit_system_id, int):
            explicit_system_id = None
        system_device = None
        if source == "system":
            if not self.config.system_audio_enabled:
                raise ValueError(
                    "System-Audio-Aufnahme ist deaktiviert. Aktivieren Sie "
                    "'system_audio_enabled', um das zu nutzen.")
            if device_id is None:
                device_id = resolve_system_audio_device()
            if device_id is None:
                raise ValueError(
                    "Kein System-Audio-Loopback-Gerät gefunden -- "
                    "hier nicht verfügbar.")
            system_device = device_id
        elif source == "both":
            if not self.config.system_audio_enabled:
                raise ValueError(
                    "System-Audio-Aufnahme ist deaktiviert. Aktivieren Sie "
                    "'system_audio_enabled', um das zu nutzen.")
            # device_id is the microphone selected in the UI. System audio has
            # its own optional setting; never reinterpret the microphone id as
            # a loopback device.
            system_device = explicit_system_id or resolve_system_audio_device()
            if system_device is None:
                raise ValueError(
                    "Kein System-Audio-Loopback-Gerät gefunden -- "
                    "hier nicht verfügbar.")
            if requested_device_name or device_id is None:
                device_id = resolve_input_device(
                    requested_device_name or self.config.input_device_name,
                    None if requested_device_name else device_id or self.config.input_device_index)
            if device_id is None:
                raise ValueError("Kein Mikrofon für die gemeinsame Aufnahme gefunden.")
        elif requested_device_name or device_id is None:
            device_id = resolve_input_device(
                requested_device_name or self.config.input_device_name,
                None if requested_device_name else device_id or self.config.input_device_index)
        # Capture at the device's native rate (ALSA often can't do 16 kHz). The
        # original is stored high-fidelity; the 16 kHz ASR copy is made in assembly.
        capture_rate = self.config.sample_rate
        if source in ("mic", "system") and device_id is not None:
            capture_rate = default_input_rate(device_id) or self.config.sample_rate
        elif source == "both":
            capture_rate = (default_input_rate(device_id) or
                            default_input_rate(system_device) or self.config.sample_rate)
        capture_channels = 2 if source == "both" else self.config.channels

        with self._meeting_start_guard:
            with session_scope() as s:
                if active_meeting(s) is not None:
                    raise ActiveMeetingError("Es läuft bereits ein Meeting. Bitte zuerst beenden.")
                if project_id:
                    project = s.get(Project, project_id)
                    if project is None or project.deleted_at is not None:
                        raise UnknownMeetingError(project_id)
                meeting_settings = self._normalise_meeting_settings(settings)
                meeting = Meeting(title=title or "Neues Meeting", status="recording",
                                  project_id=project_id,
                                  settings_json=json.dumps(meeting_settings, ensure_ascii=False))
                s.add(meeting)
                s.flush()
                recorded_device = (f"mic:{device_id};system:{system_device}"
                                   if source == "both" else str(device_id))
                s.add(Recording(meeting_id=meeting.id, source=source, device=recorded_device,
                                sample_rate=capture_rate,
                                channels=capture_channels,
                                status="recording", in_progress=True))
                s.add(ConsentEvent(acknowledged=True, text=self.config.consent_text))
                meeting_id = meeting.id
                s.commit()

        audio_dir = self.config.audio_dir / meeting_id
        # Phase 5 (opt-in): attach a live transcription pipeline, wired to the
        # capture's short frame listener. It stays off unless config enables it
        # AND a ready live ASR engine exists (so the default path is unchanged).
        pipeline = None
        live_asr = None
        live_diar = None
        live_ready = False
        # Preparing the capture source (device probe, rate fallback, combined
        # source, chunk writer) can fail -- e.g. the device rejects every rate.
        # The already-committed Meeting/Recording rows must then be marked
        # failed, or a phantom "recording" meeting blocks every later start.
        src = None
        session = None
        try:
            try:
                audio_dir.mkdir(parents=True, exist_ok=True)
                audio_dir.chmod(0o700)
            except OSError as exc:
                raise OSError("Der lokale Audioordner konnte nicht angelegt werden.") from exc

            meeting_settings = self._meeting_settings(meeting_id)
            if meeting_settings.get("live_transcription", self.config.live_transcription):
                live_asr = self._live_engine(meeting_settings.get("live_asr_model"))
                live_ready = bool(live_asr is not None and live_asr.is_ready())
                if live_ready:
                    speaker_mode = meeting_settings.get("speaker_mode")
                    if speaker_mode is None:
                        speaker_mode = "after" if self.config.speaker_diarization else "off"
                    live_diar = self._diar_engine() if speaker_mode == "live" else None
                else:
                    log.info("live_transcription_unavailable meeting=%s requested=%s",
                             meeting_id[:8], meeting_settings.get("live_asr_model") or
                             self.config.live_asr_model)

            # Real mic capture: if the device rejects the chosen rate, walk the
            # fallback rates (ALSA often can't do 16 kHz) and resolve the *actual*
            # rate before building the chunk writer, so the writer always matches.
            if source == "both":
                rates = [capture_rate] + [r for r in self.config.capture_rate_fallback
                                          if r != capture_rate]
                resolved = try_rates(
                    rates,
                    lambda r: (probe_portaudio(device_id, r, 1),
                               probe_portaudio(system_device, r, 1)))
                if resolved != capture_rate:
                    log.warning("capture_rate_fallback meeting=%s %s->%s",
                                meeting_id[:8], capture_rate, resolved)
                    with session_scope() as s:
                        rec = s.scalars(select(Recording).where(
                            Recording.meeting_id == meeting_id)).first()
                        if rec is not None:
                            rec.sample_rate = resolved
                    capture_rate = resolved
                src = CombinedSource(device_id, system_device, capture_rate)
            else:
                src = self._source_factory(device_id, capture_rate, self.config.channels)
            if isinstance(src, LiveSource) and source in ("mic", "system"):
                rates = [capture_rate] + [r for r in self.config.capture_rate_fallback
                                          if r != capture_rate]
                resolved = try_rates(
                    rates,
                    lambda r: probe_portaudio(device_id, r, self.config.channels))
                if resolved != capture_rate:
                    log.warning("capture_rate_fallback meeting=%s %s->%s",
                                meeting_id[:8], capture_rate, resolved)
                    with session_scope() as s:
                        rec = s.scalars(select(Recording).where(
                            Recording.meeting_id == meeting_id)).first()
                        if rec is not None:
                            rec.sample_rate = resolved
                    capture_rate = resolved
                    src.sample_rate = resolved
            if live_ready and live_asr is not None:
                pipeline = LiveTranscriptionPipeline(
                    meeting_id=meeting_id,
                    asr_engine=live_asr,
                    diar_engine=live_diar,
                    # The live buffer is deliberately normalized to 16 kHz;
                    # ``on_chunk`` receives the native capture rate and
                    # resamples into this target before inference.
                    sample_rate=16000,
                    window_s=self.config.live_window_s,
                    period_s=self.config.live_period_s,
                    tail_s=self.config.live_tail_s,
                    language=meeting_settings.get("language") or self.config.asr_language,
                )
            session = CaptureSession(audio_dir, src, capture_rate,
                                      capture_channels, self.config.chunk_seconds,
                                      self.config,
                                      frame_listener=pipeline.on_chunk if pipeline else None,
                                      source_kind=source)
            session.writer.source = source
            session.writer.device = (f"mic:{device_id};system:{system_device}"
                                     if source == "both" else str(device_id))
            session.start()
        except Exception as exc:
            # ``CaptureSession.start`` normally either starts cleanly or fails
            # before exposing the session. If a source/thread was created and
            # then failed, stop it here so no worker or PortAudio handle
            # survives a failed start attempt.
            if session is not None:
                try:
                    session.stop(wait=True)
                    session.wait_done(timeout=5)
                except Exception:
                    log.debug("failed_start_session_cleanup_failed meeting=%s",
                              meeting_id[:8], exc_info=True)
            elif src is not None:
                try:
                    src.close()
                except Exception:
                    log.debug("failed_start_source_cleanup_failed meeting=%s",
                              meeting_id[:8], exc_info=True)
            # The DB row is intentionally retained as a failed attempt, but it
            # must not block the next recording as an active meeting.
            with session_scope() as s:
                meeting = s.get(Meeting, meeting_id)
                recording = s.scalar(select(Recording).where(
                    Recording.meeting_id == meeting_id))
                if meeting is not None:
                    meeting.status = "failed"
                if recording is not None:
                    recording.status = "failed"
                    recording.in_progress = False
                s.commit()
            log.error("meeting_start_failed meeting=%s error=%s", meeting_id[:8], exc)
            raise
        with self._state_lock:
            self._sessions[meeting_id] = session
            if pipeline is not None:
                self._live_pipelines[meeting_id] = pipeline
        if pipeline is not None:
            pipeline.start()
        log.info("meeting_started id=%s source=%s device=%s rate=%s live=%s",
                 meeting_id[:8], source, device_id, capture_rate, pipeline is not None)
        return meeting_id

    def pause(self, meeting_id: str) -> None:
        session = self._require(meeting_id)
        session.pause()
        self._set_status(meeting_id, "paused")

    def resume(self, meeting_id: str) -> None:
        session = self._require(meeting_id)
        session.resume()
        self._set_status(meeting_id, "recording")

    def stop(self, meeting_id: str) -> dict:
        session = self._require(meeting_id)
        state = session.stop(wait=True)
        if not session.wait_done(timeout=30):
            # Keep the session registered and its durable row active while the
            # worker may still be writing. Removing it here could admit a
            # second recording concurrently and corrupt the lifecycle state.
            log.error("session_done_timeout meeting=%s", meeting_id[:8])
            raise RuntimeError(
                "Die Aufnahme konnte innerhalb des Zeitlimits nicht beendet werden. "
                "Bitte erneut versuchen.")
        # Phase 5: stop the live pipeline; this flushes the trailing partial as a
        # final segment so nothing heard is lost from the live preview.
        with self._state_lock:
            pipeline = self._live_pipelines.pop(meeting_id, None)
        if pipeline is not None:
            try:
                pipeline.stop()
            except Exception:
                log.exception("live_pipeline_stop_failed meeting=%s", meeting_id[:8])
        # persist final state.  Assembly can fail after capture stopped (for
        # example because ffmpeg or a chunk is corrupt); that is a failed
        # recording, not a ready meeting with an unusable transcribe job.
        capture_ok = state.status == CaptureStatus.stopped and bool(state.original_path)
        enhancement_source = "mic"
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            recording = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
            if meeting is not None:
                meeting.status = ("processing" if self.config.auto_pipeline else "ready") \
                    if capture_ok else "failed"
                meeting.end_at = utcnow()
                if state.original_path:
                    meeting.duration_s = round(state.duration_s, 3)
            if recording is not None:
                recording.status = "assembled" if capture_ok else "failed"
                recording.in_progress = False
                recording.original_path = state.original_path
                enhancement_source = recording.source or "mic"
            if capture_ok:
                JobQueue.get_or_create(s, meeting_id, "transcribe")
            s.commit()
        with self._state_lock:
            self._sessions.pop(meeting_id, None)
        if capture_ok and self.config.mic_enhancement_enabled and enhancement_source != "system":
            self.schedule_audio_enhancement(meeting_id, state.original_path,
                                            enhancement_source)
        # P3: kick off the automatic post-stop pipeline (non-blocking).
        if capture_ok and self.config.auto_pipeline:
            self.schedule_pipeline(meeting_id)
        return self.get_status(meeting_id)

    def status(self, meeting_id: str) -> dict:
        return self.get_status(meeting_id)

    def get_status(self, meeting_id: str) -> dict:
        with self._state_lock:
            live = self._sessions.get(meeting_id)
            pipeline = self._live_pipelines.get(meeting_id)
        if live is not None:
            st = live.state
            result = {
                "id": meeting_id,
                "status": st.status.value,
                "duration_s": round(st.duration_s, 3),
                "level_db": round(st.level_db, 3) if st.level_db != float("-inf") else None,
                "peak_db": round(st.peak_db, 3) if st.peak_db != float("-inf") else None,
                "speaking": st.speaking,
                "chunks": st.chunks,
                "source": st.source,
                "device": st.device,
                "original_path": st.original_path,
                "error": st.error,
            }
            if pipeline is not None:
                result["live"] = pipeline.snapshot()
                # The live UI needs the complete confirmed history, not only
                # the newest partial line. Keep this bounded for long calls;
                # the meeting detail endpoint remains the full transcript.
                with session_scope() as s:
                    rows = s.scalars(
                        select(TranscriptSegment)
                        .where(TranscriptSegment.meeting_id == meeting_id)
                        .order_by(TranscriptSegment.start_s.desc())
                        .limit(500)
                    ).all()
                result["live_segments"] = [
                    {"id": row.id, "start_s": row.start_s, "end_s": row.end_s,
                     "text": row.text, "speaker_id": row.speaker_id,
                     "language": row.language, "status": row.status}
                    for row in reversed(rows)
                ]
            return result
        # not live: read from DB
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            recording = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
            integrity = None
            if recording is not None and recording.original_path:
                from pathlib import Path
                mdir = Path(recording.original_path).parent
                integrity = verify_original(mdir, self.config)
            return {
                "id": meeting_id,
                "status": meeting.status,
                "duration_s": meeting.duration_s,
                "original_path": recording.original_path if recording else None,
                "integrity": integrity,
            }

    def close(self) -> None:
        # Controlled shutdown: stop each capture session and give it a bounded
        # time to finalise its WAV (the worker finalises right after the stop
        # signal) instead of abandoning it mid-write.
        with self._state_lock:
            sessions = list(self._sessions.items())
        for mid, _session in sessions:
            try:
                # Reuse the normal stop path so the durable Meeting and
                # Recording rows receive the same assembled/failed state as a
                # user-initiated stop.  A raw CaptureSession.stop() alone
                # leaves ``in_progress`` and ``recording`` stuck after daemon
                # shutdown.
                self.stop(mid)
            except Exception:  # pragma: no cover
                log.warning("shutdown_session_stop_failed meeting=%s", mid[:8],
                            exc_info=True)
        with self._state_lock:
            pipelines = list(self._live_pipelines.items())
        for mid, pipe in pipelines:
            try:
                pipe.stop()
            except Exception:  # pragma: no cover
                log.warning("shutdown_live_pipeline_stop_failed meeting=%s",
                            mid[:8], exc_info=True)
        with self._state_lock:
            self._live_pipelines.clear()
            self._sessions.clear()
        # P3: queued work is cancelled, but already-running stages are drained
        # before the process releases its PID/lock.  Returning while an ASR or
        # LLM worker still writes the database would allow a second daemon to
        # start against the same DB and duplicate a non-idempotent stage.
        try:
            self._pipeline_executor.shutdown(wait=True, cancel_futures=True)
        except Exception:  # pragma: no cover
            pass
        try:
            self._audio_enhancement_executor.shutdown(wait=True, cancel_futures=True)
        except Exception:  # pragma: no cover
            pass

    # --- helpers ---
    def _require(self, meeting_id: str) -> CaptureSession:
        with self._state_lock:
            session = self._sessions.get(meeting_id)
        if session is None:
            raise UnknownMeetingError(meeting_id)
        return session

    def _set_status(self, meeting_id: str, status: str) -> None:
        with session_scope() as s:
            m = s.get(Meeting, meeting_id)
            if m is not None:
                m.status = status
                s.commit()
