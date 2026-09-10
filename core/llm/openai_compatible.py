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

import json
import threading
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx

from core.config import Config
from core.logging_setup import get_logger
from core.security.secrets import require_endpoint_allowed

from core.llm.base import (
    LLMCancelledError, LLMContextOverflowError, LLMEngine, LLMError, LLMResult,
    LLMUnavailableError, ServerBusyError,
)

log = get_logger("ma.llm")

# llama.cpp reports a busy slot with 503 (some builds/versions use 429).
_BUSY_STATUS = (429, 503)


def _parse_context_overflow(body: str) -> Optional[Tuple[int, int]]:
    """Detect a llama.cpp-style context-overflow rejection (HTTP 400).

    Returns ``(n_prompt_tokens, n_ctx)`` for bodies shaped like
    ``{"error": {"type": "exceed_context_size_error",
    "message": "request (N tokens) exceeds the available context size ...",
    "n_prompt_tokens": N, "n_ctx": M}}``; ``None`` for anything else, so
    other 4xx errors keep their usual ``LLMError`` handling.
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return None
    err = data.get("error")
    if not isinstance(err, dict):
        return None
    err_type = str(err.get("type") or "").lower()
    message = str(err.get("message") or "").lower()
    if "exceed_context_size" not in err_type \
            and "exceeds the available context size" not in message:
        return None
    prompt_tokens = err.get("n_prompt_tokens", data.get("n_prompt_tokens"))
    ctx_tokens = err.get("n_ctx", data.get("n_ctx"))
    if not isinstance(prompt_tokens, int) or isinstance(prompt_tokens, bool) \
            or prompt_tokens <= 0:
        return None
    if not isinstance(ctx_tokens, int) or isinstance(ctx_tokens, bool) \
            or ctx_tokens <= 0:
        return None
    return prompt_tokens, ctx_tokens


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
        # Set by LLMProviderManager when this engine is routed to Ollama.
        self.ollama = False
        self._http = http_client
        self._sleep = sleep_fn
        # Context-window detection cache: None = not detected yet.
        self._ctx_window: Optional[int] = None
        self._ctx_lock = threading.Lock()

    # -- context window detection ------------------------------------------
    # Where different OpenAI-compatible servers report the model context
    # (tokens). Checked on the model entry from GET /models, on its top level
    # and in the nested server-specific metadata objects.
    _CTX_KEYS = ("context_length", "max_context_length", "context_window",
                 "max_model_len", "max_position_embeddings", "n_ctx")

    def get_context_window(self) -> Optional[int]:
        """Best-effort detection of the model's context window (tokens).

        Queries ``GET /models`` once (the result is cached for the engine's
        lifetime) and reads the window from the locations the common local
        servers use it: ``meta.n_ctx`` (llama.cpp), ``details.context_length``
        (Ollama) or a conventional top-level field. Never raises -- any
        failure (server down, unexpected shape, no field) yields ``None`` and
        the caller falls back to its safe default.
        """
        if self._ctx_window is not None:
            return self._ctx_window
        with self._ctx_lock:
            if self._ctx_window is not None:
                return self._ctx_window
            window = self._detect_context_window()
            # Cache the outcome (including None): one detection per engine.
            self._ctx_window = window
            if window:
                log.info("llm_context_window model=%s tokens=%d",
                         self.model, window)
            return window

    def _detect_context_window(self) -> Optional[int]:
        try:
            if self._http is None:
                require_endpoint_allowed(self.base_url, self.config)
            resp = self._client().get(self.base_url + "/models", timeout=5.0)
            if resp.status_code >= 400:
                return None
            data = resp.json()
        except Exception:  # noqa: BLE001 - best-effort by contract
            return None
        if not isinstance(data, dict):
            return None
        entries = data.get("data")
        if not isinstance(entries, list) or not entries:
            return None
        entry = next((e for e in entries
                      if isinstance(e, dict) and e.get("id") == self.model),
                     None)
        if entry is None:
            entry = entries[0] if len(entries) == 1 and isinstance(entries[0], dict) else None
        if not isinstance(entry, dict):
            return None
        containers = [entry]
        for nested in ("meta", "details"):
            if isinstance(entry.get(nested), dict):
                containers.append(entry[nested])
        for container in containers:
            for key in self._CTX_KEYS:
                value = container.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    return value
        return None

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
        if self.ollama:
            # Ollama models with built-in thinking (z. B. Qwen3) can spend the
            # whole max_tokens budget on hidden reasoning, leaving the content
            # field empty for structured JSON analysis. Structured extraction
            # runs faster and more reliably without thinking.
            payload["reasoning_effort"] = "none"
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
                overflow = _parse_context_overflow(resp.text)
                if overflow is not None:
                    raise LLMContextOverflowError(
                        f"LLM-Kontextfenster zu klein: die Anfrage ben\u00f6tigt "
                        f"{overflow[0]:,} Tokens, das Modell bietet "
                        f"{overflow[1]:,}. [{self._display_endpoint()}]",
                        prompt_tokens=overflow[0], ctx_tokens=overflow[1])
                raise LLMError(f"LLM-Anfrage abgelehnt HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                data = resp.json()
            except ValueError:
                raise LLMError(f"Unerwartete Antwort (kein JSON) vom LLM-Server: {resp.text[:200]}")
            return self._parse(data)
        # Unreachable, but keep the loop total-safe.
        raise ServerBusyError(f"LLM-Server ist belegt. Letzter Hinweis: {last_busy}")
