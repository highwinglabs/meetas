"""OpenAI-compatible chat-completions client (llama.cpp ``/v1`` and friends).

Uses ``httpx`` (already a project dependency) directly -- no ``openai`` SDK.
Only one request is made per analysis; when the server reports it is busy
(HTTP 429/503) we *wait and retry* for a bounded time, then surface a clear,
understandable ``ServerBusyError``. A down server / timeout / bad answer yields
an ``LLMUnavailableError``.

The ``http_client`` can be injected (tests pass an ``httpx.Client`` backed by
``httpx.MockTransport``) so no real network call is ever made in tests.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.config import Config
from core.logging_setup import get_logger
from core.security.secrets import require_endpoint_allowed

from core.llm.base import (
    LLMCancelledError, LLMEngine, LLMError, LLMResult, LLMUnavailableError,
    ServerBusyError,
)

log = get_logger("ma.llm")

# llama.cpp reports a busy slot with 503 (some builds/versions use 429).
_BUSY_STATUS = (429, 503)


class OpenAICompatibleLLM(LLMEngine):
    """Client for a local OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        config: Config,
        http_client: Optional[httpx.Client] = None,
        sleep_fn=time.sleep,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.model_name = model
        self.config = config
        self.timeout_s = float(getattr(config, "llm_timeout_s", 300.0) or 300.0)
        self.max_busy_retries = int(getattr(config, "llm_max_busy_retries", 10) or 0)
        self.busy_wait_s = float(getattr(config, "llm_busy_wait_s", 3.0) or 0.0)
        self.busy_budget_s = float(getattr(config, "llm_busy_budget_s", 600.0) or 600.0)
        self.temperature = float(getattr(config, "llm_temperature", 0.2) or 0.2)
        self.max_tokens = int(getattr(config, "llm_max_tokens", 0) or 0)
        self._http = http_client
        self._sleep = sleep_fn

    # -- client lifecycle ---------------------------------------------------
    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(self.timeout_s, connect=10.0))
        return self._http

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            finally:
                self._http = None

    def is_available(self) -> bool:
        # Configured and local; reachability is discovered on the first real
        # request (we intentionally do NOT ping the server here).
        return bool(self.base_url)

    # -- request building ---------------------------------------------------
    def _chat_url(self) -> str:
        return self.base_url + "/chat/completions"

    def _display_endpoint(self) -> str:
        """Return an endpoint safe to include in user-facing errors."""
        try:
            parsed = urlsplit(self.base_url)
            host = parsed.hostname or ""
            if parsed.port:
                host += f":{parsed.port}"
            return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
        except ValueError:
            return "<ungültiger Endpunkt>"

    def _build_payload(
        self, prompt: str, system: Optional[str],
        temperature: Optional[float], max_tokens: Optional[int],
    ) -> Dict[str, Any]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": self.temperature if temperature is None else temperature,
        }
        tokens = self.max_tokens if max_tokens is None else max_tokens
        if tokens:
            payload["max_tokens"] = int(tokens)
        return payload

    def _parse(self, data: Dict[str, Any]) -> LLMResult:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unerwartete Antwortstruktur vom LLM-Server: {exc}")
        return LLMResult(
            text=(content or "").strip(),
            model=data.get("model") or self.model,
            usage=data.get("usage") or {},
            raw=data,
        )

    # -- main entry point ---------------------------------------------------
    def complete(
        self, prompt: str, system: Optional[str] = None,
        temperature: Optional[float] = None, max_tokens: Optional[int] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> LLMResult:
        if cancel_event is not None and cancel_event.is_set():
            raise LLMCancelledError("Analyse gestoppt.")
        # A supplied MockTransport is used only by tests. Real clients are
        # checked before the first request, so network_allowed=False still
        # permits local Ollama/llama.cpp but blocks accidental cloud calls.
        if self._http is None:
            require_endpoint_allowed(self.base_url, self.config)
        client = self._client()
        url = self._chat_url()
        payload = self._build_payload(prompt, system, temperature, max_tokens)
        attempts = self.max_busy_retries + 1
        last_busy = ""
        budget_started: Optional[float] = None
        for attempt in range(1, attempts + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise LLMCancelledError("Analyse gestoppt.")
            try:
                resp = client.post(url, json=payload)
            except httpx.ConnectError as exc:
                raise LLMUnavailableError(
                    f"LLM-Server nicht erreichbar unter {self._display_endpoint()} "
                    f"(läuft er?). Details: {exc}"
                )
            except httpx.TimeoutException as exc:
                raise LLMUnavailableError(
                    f"Zeitüberschreitung beim LLM-Server {self._display_endpoint()} "
                    f"nach {self.timeout_s:.0f}s: {exc}"
                )
            except httpx.HTTPError as exc:
                raise LLMUnavailableError(f"LLM-Anfrage fehlgeschlagen ({self._display_endpoint()}): {exc}")

            if resp.status_code in _BUSY_STATUS:
                last_busy = resp.text[:200]
                if attempt < attempts:
                    if budget_started is None:
                        budget_started = time.monotonic()
                    elapsed = time.monotonic() - budget_started
                    remaining = self.busy_budget_s - elapsed
                    if remaining <= 0:
                        raise ServerBusyError(
                            f"LLM-Server ist (noch) belegt; das Wartebudget von "
                            f"{self.busy_budget_s:.0f}s ist nach {elapsed:.0f}s "
                            f"ersch\u00f6pft. Bitte in wenigen Augenblicken erneut "
                            f"versuchen. [{self._display_endpoint()}]"
                        )
                    wait = min(self.busy_wait_s, remaining)
                    log.info(
                        "llm_busy status=%s attempt=%d/%d wait=%.1fs budget_left=%.0fs",
                        resp.status_code, attempt, attempts, wait,
                        remaining - wait,
                    )
                    self._sleep(wait)
                    continue
                raise ServerBusyError(
                    f"LLM-Server ist (noch) belegt und hat nach {attempts} Versuchen "
                    f"({self.busy_wait_s:.0f}s Wartezeit pro Versuch) nicht antworten "
                    f"können. Bitte in wenigen Augenblicken erneut versuchen. "
                    f"[{self._display_endpoint()}]"
                )
            if resp.status_code >= 500:
                raise LLMUnavailableError(
                    f"LLM-Server-Fehler HTTP {resp.status_code}: {resp.text[:300]}"
                )
            if resp.status_code >= 400:
                raise LLMError(f"LLM-Anfrage abgelehnt HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                data = resp.json()
            except ValueError:
                raise LLMError(f"Unerwartete Antwort (kein JSON) vom LLM-Server: {resp.text[:200]}")
            return self._parse(data)
        # Unreachable, but keep the loop total-safe.
        raise ServerBusyError(f"LLM-Server ist belegt. Letzter Hinweis: {last_busy}")
