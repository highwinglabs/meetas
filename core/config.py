"""Configuration for the meeting-assistant core.

Precedence: built-in defaults < config.json (in base dir) < environment.
All storage locations are configurable (privacy requirement: configurable paths).
Network is OFF by default; model downloads always require explicit confirmation.
"""
from __future__ import annotations

import json
import math
import os
import shutil
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

ENV_PREFIX = "MA_"


def _default_audio_profiles() -> dict:
    # Import lazily: core.audio.__init__ exposes capture/assembly, which itself
    # depends on Config. Importing DSP helpers at module import time would make
    # database migrations hit a circular import.
    from core.audio.enhancement import default_audio_profiles
    return default_audio_profiles()


def default_base_dir() -> Path:
    env = os.environ.get(f"{ENV_PREFIX}BASE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "meeting_assistant"


def _load_dotenv(base_dir: Path) -> None:
    """Optionally load a local ``.env`` file into the process environment.

    Real environment variables always win (``override=False``), so a ``.env``
    only fills in ``MA_*`` values the user has not already set. This lets users
    keep their configuration in a ``.env`` (see ``.env.example``) without editing
    ``config.json``. ``python-dotenv`` ships with ``uvicorn[standard]``; if it is
    unavailable the ``.env`` is silently ignored.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - uvicorn[standard] provides it
        return
    for candidate in (Path.cwd() / ".env", base_dir / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(f"{ENV_PREFIX}{name}")
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    return os.environ.get(f"{ENV_PREFIX}{name}", default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(f"{ENV_PREFIX}{name}")
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        try:
            return int(default)
        except (TypeError, ValueError):
            return 0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(f"{ENV_PREFIX}{name}")
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        try:
            return float(default)
        except (TypeError, ValueError):
            return 0.0


@dataclass
class Config:
    # --- storage (all configurable) ---
    base_dir: Path = field(default_factory=default_base_dir)

    # --- capture defaults ---
    sample_rate: int = 16000
    channels: int = 1
    chunk_seconds: float = 1.0
    default_source: str = "mic"  # mic | system | both (system/both added post-MVP)
    input_device_name: Optional[str] = None  # case-insensitive substring, e.g. "headset"
    input_device_index: Optional[int] = None  # fallback: explicit PortAudio index

    # --- API ---
    host: str = "127.0.0.1"  # bound to loopback only (security)
    port: int = 8765

    # --- ASR / providers ---
    # ``asr_model`` remains the backwards-compatible final/batch default.
    # Live and final processing are independently selectable per meeting.
    asr_model: str = "small"  # final fallback when no meeting override exists
    live_asr_model: str = "parakeet-tdt-0.6b-v3-int8"
    # Used only when the optional Parakeet runtime/model is unavailable.
    # The fallback is explicit in the UI and never pretends to be Parakeet.
    live_fallback_asr_model: str = "small"
    # A model already present in the current CPU-first installation. Larger
    # models remain selectable but must be downloaded explicitly first.
    quality_asr_model: str = "small"
    asr_compute_type: str = "int8"  # CPU-efficient quantization
    asr_language: Optional[str] = None  # None = auto-detect per meeting (de/en expected)
    # Output language for the LLM analysis. "wie_transkript" (default) writes in
    # the transcript's own language; "de"/"en" force that language. This is the
    # *global seed* for new meetings; each meeting stores its own choice.
    analysis_language: str = "wie_transkript"
    default_speaker_mode: str = "off"  # off | after (manual) | live (opt-in)
    default_analysis_template: str = "standard"
    default_summary_model: str = "qwen3.5:4b"
    quality_analysis_model: str = "qwen3.8-27b-q4kxl"

    # --- live transcription + speaker diarization (Phase 5) - OFF by default ---
    # Both run fully offline and are opt-in so the default (batch) behaviour and
    # the existing test-suite are untouched.
    live_transcription: bool = False   # rolling-window live transcript while recording
    live_window_s: float = 10.0        # short rolling window; avoids repeated long CPU jobs
    live_period_s: float = 2.0         # re-transcribe at most every N seconds
    live_tail_s: float = 2.0           # trailing "partial" not yet settled, in seconds
    speaker_diarization: bool = False  # label segments with the local numpy diarizer

    # --- Phase 2: capture robustness + optional system audio ---
    # The device's native rate is preferred (ALSA often cannot open 16 kHz). If
    # PortAudio still refuses the chosen rate we walk these fallbacks before
    # giving up, so a mismatched device degrades instead of failing to record.
    capture_rate_fallback: list = field(default_factory=lambda: [16000, 48000, 44100])
    # Safe, local microphone cleanup. System audio remains unchanged.
    mic_enhancement_enabled: bool = False
    # The archival enhancement is non-destructive: the original WAV stays the
    # source of truth and a separate enhanced WAV is created after stop.
    audio_enhancement_profile: str = "meeting"
    audio_enhancement_profiles: dict = field(default_factory=_default_audio_profiles)
    # System-audio (loopback / what-you-hear) capture is OFF by default and only
    # tried when the user explicitly enables it; without an audio subsystem the
    # feature reports itself unavailable instead of crashing the core.
    system_audio_enabled: bool = False

    # --- Phase 4: diarization backend (local-only) ---
    # "numpy" is the dependency-free default (works offline, no download).
    # "pyannote" is an opt-in, higher-accuracy engine that needs a model download
    # (gated by network_allowed + confirmation) and torch -- it is never used
    # silently.
    diarization_backend: str = "numpy"  # numpy | pyannote
    pyannote_model: str = "pyannote/speaker-diarization-3.1"

    # --- Phase 5: local relevance index + RAG ---
    # The local lexical vectorizer needs no model download and no network.
    embeddings_enabled: bool = True
    embedding_dim: int = 256
    rag_enabled: bool = True
    rag_top_k: int = 6
    # Reranking is opt-in and off by default (a cross-encoder is an extra model).
    # When off, hybrid results are already RRF-fused and well ordered.
    reranking_enabled: bool = False

    # --- Phase 6/7: tasks + comfort features ---
    tasks_enabled: bool = True          # extract central tasks from an analysis
    auto_title_tags: bool = True        # derive title/tags from the analysis (local, free)
    # --- P3: automatic post-stop pipeline ---
    # After stop, automatically run transcribe -> diarize (if enabled) ->
    # embed (if enabled) -> analyze (if auto_analyze) as a non-blocking,
    # crash-resumable background pipeline. Off = manual per-stage actions.
    auto_pipeline: bool = False
    auto_analyze: bool = False          # analysis is started explicitly by the user
    pipeline_max_retries: int = 3       # give up on a failed stage after this many attempts
    pipeline_max_workers: int = 1       # CPU/RAM-safe default; configurable
    # Export formats always supported: markdown/txt/json. pdf/docx are rendered
    # through optional libraries and fall back to a clean HTML/printable file.
    export_formats: tuple = ("markdown", "txt", "json", "pdf", "docx", "html")

    # --- Phase 7c: local backup / restore + auto backups ---
    # All backups are local file copies of the SQLite DB (a consistent snapshot).
    # Auto-backup runs best-effort at startup and is local-only (no cloud, no
    # telemetry). Retention keeps the newest N backups; nothing user data is
    # ever deleted, and a restore always takes a pre-restore safety snapshot.
    backup_enabled: bool = True
    backup_interval_hours: int = 24
    backup_retention: int = 14

    # --- LLM (Phase 3) --- one already-running local OpenAI-compatible server
    # (e.g. llama.cpp /v1). We point at it; we never start a second LLM or
    # model server, and no network is used. Real analysis only runs when the
    # user triggers it; tests/dev use a mock (llm_mock or injected engine).
    llm_base_url: str = "http://127.0.0.1:8081/v1"
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    llm_model: str = "qwen3.8-27b-q4kxl"  # model id reported by the server /v1/models
    llm_mock: bool = False  # dev flag: force the deterministic MockLLM (no server call)
    llm_temperature: float = 0.2
    # The structured analysis is a nine-area JSON where every statement carries
    # one or more sources; a verbose answer can easily exceed ~1.5k tokens. A
    # cap that is too small makes the model stop at ``finish_reason=length`` and
    # emit *truncated* (invalid) JSON. 8192 leaves ample headroom while staying
    # comfortably inside a typical local model's context window.
    llm_max_tokens: int = 8192
    llm_timeout_s: float = 300.0  # inference can be slow on CPU
    llm_max_busy_retries: int = 10  # how often to wait/retry while the server is busy
    llm_busy_wait_s: float = 3.0  # pause between busy retries

    # --- Phase 3: model profiles (swappable, no hardcoding) ---
    # Each capability (role) can point at a different local model/endpoint. A role
    # maps to an optional dict; any key left unset inherits the main llm_* values
    # above (see `resolved_profile`). Roles:
    #   live        - fast, short live captions / interim labels
    #   offline     - local analysis when the main server is busy/unavailable
    #   sprecher    - speaker labelling / diarization post-processing
    #   analyse     - the main nine-area structured analysis (default)
    #   embeddings  - text relevance index
    #   reranking   - cross-encoder rerank of retrieved chunks
    model_profiles: dict = field(default_factory=dict)

    # --- privacy / network ---
    network_allowed: bool = False  # NO network by default
    downloads_require_confirmation: bool = True
    # Hard cap for a single uploaded file (direct + resumable). Prevents an
    # unbounded request body from exhausting memory/disk. 4 GiB covers large
    # lossless recordings while the browser still uploads them in small chunks.
    max_upload_bytes: int = 4 * 1024 * 1024 * 1024
    # Direct one-request uploads are kept below this memory-safe threshold;
    # larger files must use the resumable endpoint, which writes chunks to
    # disk and never buffers the complete body in RAM.
    max_direct_upload_bytes: int = 64 * 1024 * 1024
    # Hard cap for a *single resumable-upload chunk request*. A chunk above this
    # is rejected with HTTP 413 before its body is buffered, so one request can
    # never exhaust memory even if the overall file is within max_upload_bytes.
    max_upload_chunk_bytes: int = 256 * 1024 * 1024
    consent_text: str = (
        "Diese Anwendung nimmt Tonaufnahmen auf, speichert sie lokal und "
        "verarbeitet sie zu Transkripten und Analysen. Alle Daten bleiben "
        "standardmäßig auf diesem Gerät. Es erfolgt keine Übertragung in "
        "die Cloud."
    )

    # --- logging ---
    log_level: str = "INFO"
    log_file: str = "logs/core.log"

    # --- paths ---
    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def audio_dir(self) -> Path:
        return self.base_dir / "audio"

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def state_dir(self) -> Path:
        return self.base_dir / "state"

    @property
    def models_dir(self) -> Path:
        return self.base_dir / "models"

    @property
    def exports_dir(self) -> Path:
        return self.base_dir / "exports"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "meeting_assistant.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def log_path(self) -> Path:
        return self.base_dir / self.log_file

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "core.pid"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "core.lock"

    # --- lifecycle ---
    def ensure_dirs(self) -> None:
        for p in (self.base_dir, self.data_dir, self.audio_dir, self.logs_dir,
                  self.state_dir, self.models_dir, self.exports_dir):
            p.mkdir(parents=True, exist_ok=True)
        # All local stores can contain transcripts, recordings, model metadata
        # or credentials; keep them inaccessible to other local users.
        for p in (self.data_dir, self.state_dir, self.audio_dir, self.logs_dir,
                  self.models_dir, self.exports_dir):
            try:
                os.chmod(p, 0o700)
            except OSError:
                pass

    # --- persistence ---
    def config_file(self) -> Path:
        return self.base_dir / "config.json"

    def save(self) -> None:
        self.ensure_dirs()
        payload = {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(self).items() if k != "base_dir"}
        payload["base_dir"] = str(self.base_dir)
        target = self.config_file()
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass

    @classmethod
    def load(cls) -> "Config":
        base = default_base_dir()
        _load_dotenv(base)
        cfg = cls(base_dir=base)
        cfg_file = base / "config.json"
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text())
                # A syntactically valid JSON value is not necessarily a
                # configuration object. Treat arrays/scalars like a corrupt
                # config and continue with safe defaults instead of crashing
                # daemon startup on ``data.items()``.
                if isinstance(data, dict):
                    for key, value in data.items():
                        if hasattr(cfg, key) and key != "base_dir":
                            setattr(cfg, key, value)
            except (json.JSONDecodeError, OSError, UnicodeError):
                pass
        # environment overrides
        cfg.sample_rate = _env_int("SAMPLE_RATE", cfg.sample_rate)
        cfg.channels = _env_int("CHANNELS", cfg.channels)
        cfg.port = _env_int("PORT", cfg.port)
        cfg.host = _env_str("HOST", cfg.host)
        cfg.network_allowed = _env_bool("NETWORK_ALLOWED", cfg.network_allowed)
        cfg.max_upload_bytes = max(1, _env_int("MAX_UPLOAD_BYTES", cfg.max_upload_bytes))
        cfg.max_upload_chunk_bytes = max(
            1, _env_int("MAX_UPLOAD_CHUNK_BYTES", cfg.max_upload_chunk_bytes))
        cfg.log_level = _env_str("LOG_LEVEL", cfg.log_level)
        cfg.asr_model = _env_str("ASR_MODEL", cfg.asr_model)
        cfg.live_asr_model = _env_str("LIVE_ASR_MODEL", cfg.live_asr_model)
        cfg.live_fallback_asr_model = _env_str("LIVE_FALLBACK_ASR_MODEL", cfg.live_fallback_asr_model)
        cfg.quality_asr_model = _env_str("QUALITY_ASR_MODEL", cfg.quality_asr_model)
        cfg.asr_compute_type = _env_str("ASR_COMPUTE", cfg.asr_compute_type)
        cfg.asr_language = _env_str("ASR_LANGUAGE", cfg.asr_language or "") or None
        cfg.analysis_language = _env_str("ANALYSIS_LANGUAGE", cfg.analysis_language) or "wie_transkript"
        cfg.default_speaker_mode = _env_str("SPEAKER_MODE", cfg.default_speaker_mode)
        cfg.default_analysis_template = _env_str("ANALYSIS_TEMPLATE", cfg.default_analysis_template)
        cfg.default_summary_model = _env_str("SUMMARY_MODEL", cfg.default_summary_model)
        cfg.quality_analysis_model = _env_str("QUALITY_ANALYSIS_MODEL", cfg.quality_analysis_model)
        cfg.live_transcription = _env_bool("LIVE_TRANSCRIPTION", cfg.live_transcription)
        cfg.live_window_s = _env_float("LIVE_WINDOW_S", cfg.live_window_s)
        cfg.live_period_s = _env_float("LIVE_PERIOD_S", cfg.live_period_s)
        cfg.live_tail_s = _env_float("LIVE_TAIL_S", cfg.live_tail_s)
        cfg.speaker_diarization = _env_bool("SPEAKER_DIARIZATION", cfg.speaker_diarization)
        cfg.llm_base_url = _env_str("LLM_BASE_URL", cfg.llm_base_url)
        cfg.ollama_base_url = _env_str("OLLAMA_BASE_URL", cfg.ollama_base_url)
        cfg.llm_model = _env_str("LLM_MODEL", cfg.llm_model)
        raw_profiles = os.environ.get(f"{ENV_PREFIX}MODEL_PROFILES")
        if raw_profiles:
            try:
                parsed_profiles = json.loads(raw_profiles)
                if isinstance(parsed_profiles, dict):
                    cfg.model_profiles = parsed_profiles
            except (TypeError, json.JSONDecodeError):
                pass
        cfg.llm_mock = _env_bool("LLM_MOCK", cfg.llm_mock)
        cfg.llm_temperature = _env_float("LLM_TEMPERATURE", cfg.llm_temperature)
        cfg.llm_max_tokens = _env_int("LLM_MAX_TOKENS", cfg.llm_max_tokens)
        cfg.llm_timeout_s = _env_float("LLM_TIMEOUT", cfg.llm_timeout_s)
        cfg.llm_max_busy_retries = _env_int("LLM_MAX_BUSY_RETRIES", cfg.llm_max_busy_retries)
        cfg.llm_busy_wait_s = _env_float("LLM_BUSY_WAIT", cfg.llm_busy_wait_s)
        # Phase 2
        cfg.system_audio_enabled = _env_bool("SYSTEM_AUDIO", cfg.system_audio_enabled)
        cfg.mic_enhancement_enabled = _env_bool("MIC_ENHANCEMENT", cfg.mic_enhancement_enabled)
        # Phase 4
        cfg.diarization_backend = _env_str("DIARIZATION_BACKEND", cfg.diarization_backend)
        # Phase 5
        cfg.embeddings_enabled = _env_bool("EMBEDDINGS", cfg.embeddings_enabled)
        cfg.embedding_dim = _env_int("EMBEDDING_DIM", cfg.embedding_dim)
        cfg.rag_enabled = _env_bool("RAG", cfg.rag_enabled)
        cfg.rag_top_k = _env_int("RAG_TOP_K", cfg.rag_top_k)
        cfg.reranking_enabled = _env_bool("RERANKING", cfg.reranking_enabled)
        # Phase 6/7
        cfg.tasks_enabled = _env_bool("TASKS", cfg.tasks_enabled)
        cfg.auto_title_tags = _env_bool("AUTO_TITLE_TAGS", cfg.auto_title_tags)
        # P3: auto pipeline after stop
        cfg.auto_pipeline = _env_bool("AUTO_PIPELINE", cfg.auto_pipeline)
        cfg.auto_analyze = _env_bool("AUTO_ANALYZE", cfg.auto_analyze)
        cfg.pipeline_max_retries = _env_int("PIPELINE_MAX_RETRIES", cfg.pipeline_max_retries)
        cfg.pipeline_max_workers = max(1, _env_int("PIPELINE_MAX_WORKERS", cfg.pipeline_max_workers))
        # Config files are user-editable JSON.  Normalise safety-critical
        # resource limits even when a stale/hand-edited file contains strings,
        # negatives, or nulls; one malformed value must not crash the API or
        # disable its upload protections.
        defaults = cls()
        def bounded_int(name: str, low: int, high: int) -> None:
            try:
                value = int(getattr(cfg, name))
            except (TypeError, ValueError):
                value = int(getattr(defaults, name))
            setattr(cfg, name, max(low, min(high, value)))

        def bounded_float(name: str, low: float, high: float) -> None:
            try:
                value = float(getattr(cfg, name))
            except (TypeError, ValueError):
                value = float(getattr(defaults, name))
            if not math.isfinite(value):
                value = float(getattr(defaults, name))
            setattr(cfg, name, max(low, min(high, value)))

        # Values from config.json are user-editable and may be stale or
        # malformed. Normalize every value used in arithmetic or resource
        # allocation before the service starts, so one bad setting cannot turn
        # into a zero-sized audio buffer, invalid port, or worker crash.
        bounded_int("sample_rate", 8_000, 384_000)
        bounded_int("channels", 1, 8)
        bounded_int("port", 1, 65_535)
        bounded_int("embedding_dim", 1, 16_384)
        bounded_int("rag_top_k", 1, 1_000)
        bounded_int("backup_interval_hours", 1, 24 * 365)
        bounded_int("backup_retention", 1, 10_000)
        bounded_int("llm_max_tokens", 0, 1_000_000)
        bounded_int("llm_max_busy_retries", 0, 100)
        bounded_int("pipeline_max_workers", 1, 4)
        bounded_int("pipeline_max_retries", 0, 20)
        bounded_float("chunk_seconds", 0.05, 60.0)
        bounded_float("live_window_s", 6.0, 60.0)
        bounded_float("live_period_s", 0.2, 15.0)
        bounded_float("live_tail_s", 0.5, 10.0)
        bounded_float("llm_temperature", 0.0, 2.0)
        bounded_float("llm_timeout_s", 1.0, 86_400.0)
        bounded_float("llm_busy_wait_s", 0.0, 300.0)
        if cfg.live_tail_s >= cfg.live_window_s:
            cfg.live_tail_s = min(float(defaults.live_tail_s), cfg.live_window_s / 2.0)
        if not isinstance(cfg.capture_rate_fallback, (list, tuple)):
            cfg.capture_rate_fallback = list(defaults.capture_rate_fallback)
        else:
            rates: list[int] = []
            for rate in cfg.capture_rate_fallback:
                try:
                    value = int(rate)
                except (TypeError, ValueError):
                    continue
                if 8_000 <= value <= 384_000 and value not in rates:
                    rates.append(value)
            cfg.capture_rate_fallback = rates or list(defaults.capture_rate_fallback)
        if not isinstance(cfg.model_profiles, dict):
            cfg.model_profiles = {}
        if not isinstance(cfg.default_source, str) or cfg.default_source not in {"mic", "system", "both"}:
            cfg.default_source = defaults.default_source
        if not isinstance(cfg.mic_enhancement_enabled, bool):
            cfg.mic_enhancement_enabled = defaults.mic_enhancement_enabled
        from core.audio.enhancement import normalize_profiles
        cfg.audio_enhancement_profiles = normalize_profiles(cfg.audio_enhancement_profiles)
        if (not isinstance(cfg.audio_enhancement_profile, str)
                or cfg.audio_enhancement_profile.strip() not in cfg.audio_enhancement_profiles):
            cfg.audio_enhancement_profile = defaults.audio_enhancement_profile
        else:
            cfg.audio_enhancement_profile = cfg.audio_enhancement_profile.strip()
        for name in ("host", "log_level", "log_file", "asr_model",
                     "live_asr_model", "live_fallback_asr_model", "quality_asr_model",
                     "asr_compute_type", "default_summary_model",
                     "quality_analysis_model", "diarization_backend",
                     "pyannote_model", "llm_base_url", "ollama_base_url",
                     "llm_model"):
            if not isinstance(getattr(cfg, name), str):
                setattr(cfg, name, getattr(defaults, name))
        if cfg.input_device_name is not None and not isinstance(cfg.input_device_name, str):
            cfg.input_device_name = defaults.input_device_name
        if not isinstance(cfg.default_speaker_mode, str) or cfg.default_speaker_mode not in {"off", "live", "after"}:
            cfg.default_speaker_mode = defaults.default_speaker_mode
        if not isinstance(cfg.default_analysis_template, str) or cfg.default_analysis_template not in {"standard", "compact", "audit", "action_items"}:
            cfg.default_analysis_template = defaults.default_analysis_template
        if cfg.asr_language is not None and (
                not isinstance(cfg.asr_language, str)
                or cfg.asr_language not in {"", "auto", "de", "en"}):
            cfg.asr_language = defaults.asr_language
        if (not isinstance(cfg.analysis_language, str)
                or cfg.analysis_language not in {"wie_transkript", "de", "en"}):
            cfg.analysis_language = defaults.analysis_language
        if cfg.diarization_backend not in {"numpy", "pyannote"}:
            cfg.diarization_backend = defaults.diarization_backend
        if cfg.input_device_index is not None:
            try:
                cfg.input_device_index = max(0, int(cfg.input_device_index))
            except (TypeError, ValueError):
                cfg.input_device_index = defaults.input_device_index
        for name in ("max_upload_bytes", "max_direct_upload_bytes",
                     "max_upload_chunk_bytes"):
            try:
                setattr(cfg, name, max(1, int(getattr(cfg, name))))
            except (TypeError, ValueError):
                setattr(cfg, name, int(getattr(defaults, name)))
        # The specialised limits can never exceed the overall upload limit.
        cfg.max_direct_upload_bytes = min(cfg.max_direct_upload_bytes,
                                          cfg.max_upload_bytes)
        cfg.max_upload_chunk_bytes = min(cfg.max_upload_chunk_bytes,
                                         cfg.max_upload_bytes)
        try:
            cfg.pipeline_max_workers = max(1, min(4, int(cfg.pipeline_max_workers)))
        except (TypeError, ValueError):
            cfg.pipeline_max_workers = defaults.pipeline_max_workers
        try:
            cfg.pipeline_max_retries = max(0, min(20, int(cfg.pipeline_max_retries)))
        except (TypeError, ValueError):
            cfg.pipeline_max_retries = defaults.pipeline_max_retries
        for name in ("network_allowed", "live_transcription", "speaker_diarization",
                     "auto_pipeline", "auto_analyze", "embeddings_enabled",
                     "rag_enabled", "system_audio_enabled", "tasks_enabled",
                     "auto_title_tags", "backup_enabled", "llm_mock",
                     "reranking_enabled", "downloads_require_confirmation"):
            if not isinstance(getattr(cfg, name), bool):
                setattr(cfg, name, bool(getattr(defaults, name)))
        cfg.ensure_dirs()
        return cfg

    def ffmpeg_available(self) -> bool:
        return shutil.which("ffmpeg") is not None

    # Canonical model-profile roles (Phase 3). Kept as a class constant so the
    # service/API can validate a requested role without string-magic.
    MODEL_ROLES: tuple = ("live", "offline", "sprecher", "analyse",
                          "embeddings", "reranking")

    def resolved_profile(self, role: str) -> dict:
        """Resolve a model profile for `role`.

        Returns a dict with base_url / model / temperature / max_tokens / mock,
        where any key not set in ``model_profiles[role]`` falls back to the main
        ``llm_*`` settings. Nothing is hardcoded per role: the main LLM config is
        the single source of truth and profiles only override.
        """
        base = {
            "base_url": self.llm_base_url,
            "model": self.llm_model,
            "temperature": self.llm_temperature,
            "max_tokens": self.llm_max_tokens,
            "timeout_s": self.llm_timeout_s,
            "mock": self.llm_mock,
        }
        profiles = self.model_profiles if isinstance(self.model_profiles, dict) else {}
        override = profiles.get(role) or {}
        if isinstance(override, dict):
            value = override.get("base_url")
            if isinstance(value, str) and value.strip():
                base["base_url"] = value.strip()
            value = override.get("model")
            if isinstance(value, str) and value.strip() and len(value.strip()) <= 256:
                base["model"] = value.strip()
            try:
                value = float(override.get("temperature"))
                if math.isfinite(value):
                    base["temperature"] = max(0.0, min(2.0, value))
            except (TypeError, ValueError):
                pass
            try:
                value = int(override.get("max_tokens"))
                base["max_tokens"] = max(0, min(1_000_000, value))
            except (TypeError, ValueError):
                pass
            try:
                value = float(override.get("timeout_s"))
                if math.isfinite(value):
                    base["timeout_s"] = max(1.0, min(86_400.0, value))
            except (TypeError, ValueError):
                pass
            if isinstance(override.get("mock"), bool):
                base["mock"] = override["mock"]
        return base


_CONFIG: Config | None = None


def get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config.load()
    return _CONFIG


def set_config(cfg: Config) -> None:
    """For tests: replace the process-wide config."""
    global _CONFIG
    _CONFIG = cfg
