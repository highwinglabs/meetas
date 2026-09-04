"""Provider/LLM status and LLM connectivity tests. (part of MeetingService)."""
from __future__ import annotations

import time as time_module
from core.config import Config
from core.logging_setup import get_logger
from core.services._common import redact_endpoint

log = get_logger("ma.service")


class SystemMixin:

    # --- Phase 8: local provider selection (visibility) ---
    def provider_status(self) -> dict:
        """Non-network view of every capability role and the local provider it
        would use. Selection is done via the ``model_profiles`` config (env or
        config file); this endpoint surfaces the resolved choice per role so the
        UI can show/switch it. No network, no side effects."""
        roles = {}
        for role in self.config.MODEL_ROLES:
            roles[role] = self._llm_providers.status(role)
        return {
            "roles": roles,
            "diarization": {"backend": self._diar_engine().name,
                            "config": self.config.diarization_backend},
            "embeddings": self._embed_manager.backend_status(),
            "local": True,
            "network_allowed": bool(self.config.network_allowed),
            "select_how": "set model_profiles[<role>] in config or env "
                          "MA_MODEL_PROFILES (JSON). Roles: "
                          + ", ".join(self.config.MODEL_ROLES),
        }

    # --- Phase 3: LLM analysis ---
    def llm_status(self) -> dict:
        """Config + mock flag. Deliberately makes NO network call (reachability
        is only discovered when an analysis is actually requested)."""
        cfg = self.config
        return {
            "provider": "openai-compatible (local)",
            "base_url": redact_endpoint(cfg.llm_base_url),
            "model": cfg.llm_model,
            "mock": bool(self._llm_override or cfg.llm_mock),
            "local": True,
            "network_used": False,
            "busy_policy": (
                f"wartet bis zu {cfg.llm_max_busy_retries}x "
                f"{cfg.llm_busy_wait_s:.1f}s, dann Fehlermeldung"
            ),
        }

    def test_llm(self, model_name: str | None = None) -> dict:
        """Send a tiny explicit test request to the selected local LLM."""
        engine = (self._llm_providers.engine_for_model(model_name)
                  if model_name else self._llm_engine())
        started = time_module.perf_counter()
        result = engine.complete(
            "Antworte ausschließlich mit dem Wort OK.",
            system="Dies ist ein Verbindungstest. Keine Meetingdaten verwenden.",
            temperature=0.0, max_tokens=8)
        elapsed = round(time_module.perf_counter() - started, 3)
        return {"ok": True, "model": result.model or getattr(engine, "model_name", model_name),
                "seconds": elapsed, "response": result.text}
