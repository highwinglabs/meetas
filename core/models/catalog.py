"""A small, honest model catalog for the UI.

The catalog is metadata only. It never downloads or loads a model. Readiness is
reported from local files/configuration so users can choose models explicitly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import httpx

from core.config import Config
from core.security.secrets import is_loopback_endpoint


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    kind: str  # asr | llm | diarization
    purpose: str
    size: str
    runtime: str
    languages: str = ""
    installed: bool = False
    selectable: bool = True
    note: str = ""


ASR_SPECS = (
    ModelSpec("parakeet-tdt-0.6b-v3-int8", "Parakeet TDT 0.6B v3 (int8)", "asr",
              "live", "ca. 600 MB", "ONNX / lokaler Adapter", "DE, EN + 23", note="Optionaler Adapter"),
    ModelSpec("tiny", "Whisper tiny", "asr", "live/fast", "ca. 75 MB", "faster-whisper CPU", "viele"),
    ModelSpec("base", "Whisper base", "asr", "live/fast", "ca. 145 MB", "faster-whisper CPU", "viele"),
    ModelSpec("small", "Whisper small", "asr", "live/ausgewogen", "ca. 485 MB", "faster-whisper CPU", "viele"),
    ModelSpec("medium", "Whisper medium", "asr", "qualität", "ca. 1.5 GB", "faster-whisper CPU", "viele"),
    ModelSpec("large-v2", "Whisper large-v2", "asr", "qualität", "ca. 3 GB", "faster-whisper CPU", "viele"),
    ModelSpec("large-v3", "Whisper large-v3", "asr", "maximale Qualität", "ca. 3 GB", "faster-whisper CPU", "viele"),
    ModelSpec("large-v3-turbo", "Whisper large-v3-turbo", "asr", "schnelle Qualität", "ca. 1.6 GB", "faster-whisper CPU", "viele"),
    ModelSpec("distil-large-v3", "Distil-Whisper large-v3", "asr", "schnelle Qualität", "ca. 1.5 GB", "faster-whisper CPU", "viele"),
)


def _whisper_ready(config: Config, model_id: str) -> bool:
    try:
        from core.providers.faster_whisper import FasterWhisperEngine
        return FasterWhisperEngine(model_name=model_id, config=config).is_model_ready()
    except Exception:
        return False


def _parakeet_ready(config: Config, model_id: str) -> bool:
    try:
        from core.providers.parakeet import ParakeetEngine
        return ParakeetEngine(model_name=model_id, config=config).is_model_ready()
    except Exception:
        return False


def _ollama_models(config: Config) -> dict[str, dict]:
    """Read the already-running local Ollama catalog, without downloading."""
    base = str(getattr(config, "ollama_base_url", "") or "").rstrip("/")
    if not is_loopback_endpoint(base) and not config.network_allowed:
        return {}
    if base.endswith("/v1"):
        base = base[:-3]
    if not base:
        return {}
    try:
        response = httpx.get(base + "/api/tags", timeout=0.8)
        response.raise_for_status()
        rows = response.json().get("models", [])
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {}
    result: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("name"):
            result[str(row["name"])] = row
    return result


def _openai_compatible_models(config: Config) -> dict[str, dict]:
    """Discover models from the configured local llama.cpp endpoint."""
    base = str(getattr(config, "llm_base_url", "") or "").rstrip("/")
    if not is_loopback_endpoint(base) and not config.network_allowed:
        return {}
    if not base or not base.endswith("/v1"):
        return {}
    try:
        response = httpx.get(base + "/models", timeout=0.8)
        response.raise_for_status()
        rows = response.json().get("data", [])
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {}
    return {str(row["id"]): row for row in rows
            if isinstance(row, dict) and row.get("id")}


def _llm_models(config: Config) -> list[ModelSpec]:
    # These are choices, not downloads. The local endpoint may expose a subset;
    # the model test reports whether a selected id is accepted by that endpoint.
    ids: list[tuple[str, str, str]] = [
        (config.default_summary_model, "Qwen3.5 4B", "schnelle Analyse"),
        (config.quality_analysis_model, "Qwen 27B (vorhanden)", "maximale Analysequalität"),
        ("gemma4:e4b", "Gemma 4 E4B", "kompakte Analyse"),
        ("gemma4:9b", "Gemma 4 9B", "ausgewogene Analyse"),
        ("gemma4:26b", "Gemma 4 26B", "qualitative Analyse"),
    ]
    installed = _ollama_models(config)
    compatible = _openai_compatible_models(config)
    for mid, row in compatible.items():
        installed.setdefault(mid, {**row, "provider": "llama.cpp"})
    out: list[ModelSpec] = []
    seen: set[str] = set()
    for mid, name, purpose in ids:
        mid = str(mid or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        row = installed.get(mid, {})
        size = _format_size(row.get("size")) or "endpointabhängig"
        out.append(ModelSpec(mid, name, "llm", purpose, size,
                             "Ollama / llama.cpp", "DE, EN", installed=(mid in installed),
                             note="Ollama" if mid in installed else "Nicht im lokalen Ollama-Katalog"))
    # Surface every existing Ollama model, including custom names, so the UI
    # does not require hardcoded knowledge of a user's local model collection.
    for mid, row in installed.items():
        if mid in seen:
            continue
        seen.add(mid)
        details = row.get("details") if isinstance(row, dict) else {}
        parameter_size = details.get("parameter_size") if isinstance(details, dict) else None
        provider = row.get("provider", "Ollama") if isinstance(row, dict) else "Ollama"
        label = f"{provider}: {mid}"
        purpose = f"vorhanden{f' · {parameter_size}' if parameter_size else ''}"
        out.append(ModelSpec(mid, label, "llm", purpose,
                             _format_size(row.get("size")) or "lokal vorhanden",
                             str(provider), "mehrsprachig", installed=True, note=str(provider)))
    return out


def _format_size(value: object) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    return f"{size:.1f} {units[idx]}"


def list_catalog(config: Config) -> list[dict]:
    out: list[ModelSpec] = []
    for spec in ASR_SPECS:
        ready = _parakeet_ready(config, spec.id) if spec.id.startswith("parakeet") else _whisper_ready(config, spec.id)
        out.append(ModelSpec(**{**asdict(spec), "installed": ready}))
    out.extend(_llm_models(config))
    out.append(ModelSpec("numpy", "Lokaler Basis-Diarizer", "diarization", "Sprecher",
                         "ohne Modell", "CPU", "", installed=True,
                         note="schnell, aber weniger genau"))
    return [asdict(x) for x in out]
