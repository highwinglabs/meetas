"""Local LLM provider (Phase 3: meeting analysis).

One already-running, local, OpenAI-compatible server (e.g. llama.cpp ``/v1``).
We never load a second LLM or start another model server. Network is NOT used
for the LLM: it is a local endpoint. Real analysis only runs when the user
triggers it; tests/dev use ``MockLLM``.
"""
from core.llm.base import (
    LLMCancelledError, LLMEngine, LLMError, LLMResult, LLMUnavailableError,
    ServerBusyError,
)
from core.llm.manager import LLMProviderManager
from core.llm.mock import MockLLM
from core.llm.openai_compatible import OpenAICompatibleLLM

__all__ = [
    "LLMEngine", "LLMError", "LLMResult", "LLMUnavailableError", "ServerBusyError",
    "LLMProviderManager", "MockLLM", "OpenAICompatibleLLM",
]
