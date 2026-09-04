"""Build LLM engines from configuration, per *role* (Phase 3).

Mirrors the ASR ``ProviderManager`` pattern, but a role (``live``, ``offline``,
``sprecher``, ``analyse``) can point at a different local model/endpoint via the
``model_profiles`` config. The default role is ``analyse`` so existing callers
(``engine()`` with no argument) behave exactly as before. Tests bypass this by
injecting an engine directly into the service (``MeetingService(...,
llm_engine=MockLLM())``), so the real client is never constructed or called in
tests.
"""
from __future__ import annotations

from typing import Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from core.config import Config
from core.llm.base import LLMEngine
from core.llm.mock import MockLLM
from core.llm.openai_compatible import OpenAICompatibleLLM


class LLMProviderManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._cache: Dict[str, LLMEngine] = {}
        self._default: Optional[LLMEngine] = None

    def _build(self, role: str) -> LLMEngine:
        profile = self.config.resolved_profile(role)
        if profile.get("mock"):
            return MockLLM()
        engine = OpenAICompatibleLLM(
            base_url=self._endpoint_for_model(profile["base_url"], profile["model"]),
            model=profile["model"],
            config=self.config,
        )
        # Per-role tuning overrides the (role-agnostic) config-derived values.
        engine.temperature = float(profile.get("temperature", engine.temperature) or 0.2)
        engine.max_tokens = int(profile.get("max_tokens", engine.max_tokens) or 0)
        return engine

    def _endpoint_for_model(self, base_url: str, model: str) -> str:
        """Route Ollama-style model ids to Ollama's local OpenAI API.

        llama.cpp models are kept on the configured endpoint. Ollama model
        names conventionally contain a tag (``name:tag``), so this remains
        deterministic and never silently switches a plain llama.cpp model.
        """
        value = (model or "").strip()
        if ":" in value and getattr(self.config, "ollama_base_url", ""):
            return self.config.ollama_base_url
        return base_url

    def engine(self, role: str = "analyse") -> LLMEngine:
        """Return (and cache) the engine for `role`. Unknown roles fall back to
        ``analyse`` so a typo never breaks a request."""
        role = role if role in self.config.MODEL_ROLES else "analyse"
        if role not in self._cache:
            self._cache[role] = self._build(role)
        if role == "analyse":
            self._default = self._cache[role]
        return self._cache[role]

    def status(self, role: str = "analyse") -> dict:
        """Non-network description of the profile that would serve `role`."""
        profile = self.config.resolved_profile(role)
        # Show the endpoint that will actually receive a request. Ollama-style
        # model ids are routed to the configured Ollama OpenAI endpoint even
        # when the generic profile points at llama.cpp.
        endpoint = self._endpoint_for_model(profile["base_url"], profile["model"])
        try:
            parsed = urlsplit(endpoint or "")
            host = parsed.hostname or ""
            if parsed.port:
                host += f":{parsed.port}"
            endpoint = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            endpoint = "<ungültiger Endpunkt>"
        return {
            "role": role,
            "base_url": endpoint,
            "model": profile["model"],
            "temperature": profile["temperature"],
            "max_tokens": profile["max_tokens"],
            "mock": bool(profile.get("mock")),
            "local": True,
            "network_used": False,
        }

    def engine_for_model(self, model: str, role: str = "analyse") -> LLMEngine:
        """Build/cache an engine for a UI-selected local endpoint model."""
        model = (model or "").strip()
        if not model:
            return self.engine(role)
        key = f"{role}:{model}"
        if key not in self._cache:
            profile = self.config.resolved_profile(role)
            self._cache[key] = OpenAICompatibleLLM(
                base_url=self._endpoint_for_model(profile["base_url"], model),
                model=model, config=self.config)
            engine = self._cache[key]
            engine.temperature = float(profile.get("temperature", engine.temperature) or 0.2)
            engine.max_tokens = int(profile.get("max_tokens", engine.max_tokens) or 0)
        return self._cache[key]

    def clear_cache(self) -> None:
        self._cache.clear()
        self._default = None
