"""LLM provider abstraction (Phase 3: meeting analysis).

The assistant talks to a *single, already-running* local LLM server that speaks
the OpenAI-compatible chat-completions API (e.g. llama.cpp ``/v1``). We never
load a second LLM or start another model server: the server is an external
process, exactly like ffmpeg or the ASR model, and we only point at it.

Error model
-----------
``LLMError``            -- base error for any analysis failure.
``ServerBusyError``     -- the server was busy and could not take the request
                           even after waiting/retrying (clear, user-facing msg).
``LLMUnavailableError`` -- the server is unreachable / returned a hard error
                           (connection refused, timeout, HTTP 5xx, bad JSON).

During development and tests a ``MockLLM`` (``core/llm/mock.py``) is injected
so that **no real LLM request is ever executed**. The real engine is only used
when the user actually triggers a meeting analysis.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class LLMError(RuntimeError):
    """Base error for LLM analysis failures."""


class ServerBusyError(LLMError):
    """The LLM server was busy; request could not be served in time."""


class LLMUnavailableError(LLMError):
    """The LLM server is unreachable or returned a hard error."""


class LLMContextOverflowError(LLMError):
    """The request exceeded the model's context window (server HTTP 400).

    Carries the server-reported token counts (``prompt_tokens`` / ``ctx_tokens``)
    so the caller can shrink the prompt by the reported ratio and retry.
    """

    def __init__(self, message: str, prompt_tokens: int = 0,
                 ctx_tokens: int = 0) -> None:
        super().__init__(message)
        self.prompt_tokens = int(prompt_tokens or 0)
        self.ctx_tokens = int(ctx_tokens or 0)


class LLMCancelledError(LLMError):
    """The user stopped the analysis while it was running."""


@dataclass
class LLMResult:
    """A completed analysis response."""
    text: str
    model: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


class LLMEngine(ABC):
    """One local LLM engine. Subclasses implement a concrete backend."""
    model_name: str = "llm"

    def is_available(self) -> bool:
        """Lightweight "ready to be called" check (no inference, no side effects)."""
        return True

    def get_context_window(self) -> Optional[int]:
        """The model's context window in tokens, or ``None`` when unknown.

        Best-effort hint used to size prompts (e.g. how much transcript the
        analysis can carry). Implementations MUST NOT raise here -- any
        detection failure simply yields ``None`` and callers fall back to a
        safe default.
        """
        return None

    @abstractmethod
    def complete(self, prompt: str, system: Optional[str] = None, **opts: Any) -> LLMResult:
        """Run the analysis and return the model's text answer.

        Must raise a ``LLMError`` subclass on failure. Implementations that
        talk to a busy server are expected to wait (retry) before raising
        ``ServerBusyError``.
        """
        raise NotImplementedError
