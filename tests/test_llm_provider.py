"""LLM provider tests.

The OpenAI-compatible client is exercised with ``httpx.MockTransport`` -- NO
real network call and NO real LLM request is ever made. Busy-wait/retry,
unavailable (down/timeout/5xx), and answer-parsing errors are all covered.
"""
from __future__ import annotations

import json

import httpx
import pytest

from core.llm import (
    LLMContextOverflowError, LLMError, LLMUnavailableError, MockLLM,
    OpenAICompatibleLLM, ServerBusyError,
)

BASE_URL = "http://testserver/v1"


def _cfg(config):
    # Make the busy-wait fast and bounded for tests.
    config.llm_busy_wait_s = 0.01
    config.llm_max_busy_retries = 3
    config.llm_timeout_s = 5.0
    return config


def _engine(config, handler, sleep_calls: list | None = None):
    sleeps = sleep_calls if sleep_calls is not None else []
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    eng = OpenAICompatibleLLM(
        base_url=BASE_URL, model="test-model", config=_cfg(config),
        http_client=http, sleep_fn=sleeps.append,
    )
    return eng, sleeps


def _ok_body(content="Hallo"):
    return {"choices": [{"message": {"content": content}}], "model": "test-model",
            "usage": {"total_tokens": 7}}


def test_success_parses_content(config):
    eng, sleeps = _engine(config, lambda r: httpx.Response(200, json=_ok_body("Entscheidung: X")))
    out = eng.complete("prompt", system="sys")
    assert out.text == "Entscheidung: X"
    assert out.model == "test-model"
    assert out.usage["total_tokens"] == 7
    assert sleeps == []  # no busy wait on success


def test_builds_chat_payload(config):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=_ok_body())

    eng, _ = _engine(config, handler)
    eng.complete("Frage", system="Rolle")
    assert seen["url"].endswith("/v1/chat/completions")
    msgs = seen["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "Rolle"}
    assert msgs[1] == {"role": "user", "content": "Frage"}
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["stream"] is False


def test_busy_then_success_waits_and_retries(config):
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, json={"error": "slot is busy"})
        return httpx.Response(200, json=_ok_body("fertig"))

    sleeps = []
    eng, _ = _engine(config, handler, sleep_calls=sleeps)
    out = eng.complete("p")
    assert out.text == "fertig"
    assert state["n"] == 2
    assert len(sleeps) == 1 and sleeps[0] == pytest.approx(0.01)


def test_busy_forever_raises_server_busy(config):
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        return httpx.Response(503, text="busy")

    eng, _ = _engine(config, handler)
    with pytest.raises(ServerBusyError) as exc:
        eng.complete("p")
    assert "belegt" in str(exc.value)
    # 3 retries + 1 initial attempt
    assert state["n"] == 4


def test_busy_budget_stops_retry_loop_before_retries_exhausted(config):
    """M4: even with a large retry count, the busy-retry loop must give up
    after the overall wall-clock budget instead of blocking the worker for
    hours (huge retries*wait combinations used to allow that)."""
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        return httpx.Response(503, text="busy")

    config.llm_busy_wait_s = 0.1          # larger than the budget
    config.llm_max_busy_retries = 100
    config.llm_busy_budget_s = 0.05
    transport = httpx.MockTransport(handler)
    import time as _time

    start = _time.monotonic()
    eng = OpenAICompatibleLLM(
        base_url=BASE_URL, model="test-model", config=config,
        http_client=httpx.Client(transport=transport),
    )
    with pytest.raises(ServerBusyError) as exc:
        eng.complete("p")
    elapsed = _time.monotonic() - start
    assert "Wartebudget" in str(exc.value)
    # Only a handful of attempts, far fewer than the 101 configured;
    # total blocking time stays in the neighbourhood of the budget.
    assert state["n"] <= 5
    assert elapsed < 2.0


def test_429_also_treated_as_busy(config):
    state = {"n": 0}

    def handler(req):
        state["n"] += 1
        return httpx.Response(429, text="slow down") if state["n"] < 3 else httpx.Response(200, json=_ok_body("ok"))

    eng, _ = _engine(config, handler)
    assert eng.complete("p").text == "ok"
    assert state["n"] == 3


def test_connect_error_is_unavailable(config):
    def handler(req):
        raise httpx.ConnectError("connection refused")

    eng, _ = _engine(config, handler)
    with pytest.raises(LLMUnavailableError) as exc:
        eng.complete("p")
    assert BASE_URL in str(exc.value)


def test_timeout_is_unavailable(config):
    def handler(req):
        raise httpx.ReadTimeout("timed out")

    eng, _ = _engine(config, handler)
    with pytest.raises(LLMUnavailableError) as exc:
        eng.complete("p")
    assert "Zeitüberschreitung" in str(exc.value)


def test_http_500_is_unavailable(config):
    eng, _ = _engine(config, lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(LLMUnavailableError) as exc:
        eng.complete("p")
    assert "500" in str(exc.value)


def test_http_400_context_overflow_raises_overflow_error(config):
    body = {"error": {"code": 400, "type": "exceed_context_size_error",
                     "message": ("request (143936 tokens) exceeds the "
                                 "available context size (131072 tokens)"),
                     "n_prompt_tokens": 143936, "n_ctx": 131072}}
    eng, _ = _engine(config, lambda r: httpx.Response(400, json=body))
    with pytest.raises(LLMContextOverflowError) as exc:
        eng.complete("p")
    assert exc.value.prompt_tokens == 143936
    assert exc.value.ctx_tokens == 131072
    assert "Kontextfenster zu klein" in str(exc.value)


def test_http_400_context_overflow_missing_counts_not_overflow(config):
    # Same error type but without usable token counts -> plain LLMError
    # (the processor cannot shrink without the reported numbers).
    body = {"error": {"type": "exceed_context_size_error",
                     "message": "request exceeds the available context size"}}
    eng, _ = _engine(config, lambda r: httpx.Response(400, json=body))
    with pytest.raises(LLMError) as exc:
        eng.complete("p")
    assert not isinstance(exc.value, LLMContextOverflowError)


def test_http_400_is_llm_error(config):
    eng, _ = _engine(config, lambda r: httpx.Response(400, text="bad request"))
    with pytest.raises(LLMError) as exc:
        eng.complete("p")
    assert not isinstance(exc.value, LLMContextOverflowError)


def test_bad_json_is_llm_error(config):
    eng, _ = _engine(config, lambda r: httpx.Response(200, text="<html>nope</html>"))
    with pytest.raises(LLMError) as exc:
        eng.complete("p")
    assert "JSON" in str(exc.value)


def test_bad_structure_is_llm_error(config):
    eng, _ = _engine(config, lambda r: httpx.Response(200, json={"foo": 1}))
    with pytest.raises(LLMError):
        eng.complete("p")


def test_mock_llm_deterministic_and_safe(config):
    m = MockLLM()
    a = m.complete("abc")
    b = m.complete("abc")
    assert a.text == b.text
    assert a.model == "mock-llm"
    assert m.calls == 2
    assert m.last_prompt == "abc"


def test_mock_llm_busy_and_fail(config):
    with pytest.raises(ServerBusyError):
        MockLLM(busy=True).complete("p")
    with pytest.raises(LLMError):
        MockLLM(fail=True).complete("p")


def test_manager_caches_engine_and_closes_client_on_clear(config):
    # L10: the per-role engine is cached (one build), and clear_cache closes the
    # replaced engine's httpx client instead of leaking it.
    from core.llm.manager import LLMProviderManager
    manager = LLMProviderManager(config)
    first = manager.engine("analyse")
    assert manager.engine("analyse") is first, "engine was rebuilt instead of cached"
    client = first._client()  # materialize a real httpx.Client on the engine
    assert isinstance(client, httpx.Client) and client.is_closed is False
    manager.clear_cache()
    assert client.is_closed is True, "clear_cache did not close the httpx client"
    assert manager._cache == {}
    assert manager._default is None
    assert manager.engine("analyse") is not first, "a fresh engine must be built after clear"


def test_manager_engine_for_model_is_cached(config):
    from core.llm.manager import LLMProviderManager
    manager = LLMProviderManager(config)
    assert manager.engine_for_model("m1") is manager.engine_for_model("m1")
    # A different model gets its own cached engine.
    assert manager.engine_for_model("m2") is not manager.engine_for_model("m1")
    manager.clear_cache()
    assert manager._cache == {}


def test_payload_disables_ollama_thinking(config):
    eng, _ = _engine(config, lambda r: httpx.Response(200, json=_ok_body()))
    payload = eng._build_payload("p", None, None, None)
    assert "reasoning_effort" not in payload, "llama.cpp payloads must stay unchanged"
    eng.ollama = True
    assert eng._build_payload("p", None, None, None)["reasoning_effort"] == "none"


def test_manager_marks_ollama_engines(config):
    from core.llm.manager import LLMProviderManager
    config.ollama_base_url = "http://127.0.0.1:11434/v1"
    manager = LLMProviderManager(config)
    # Ollama-style id (contains a tag) is routed to Ollama -> thinking off.
    ollama_eng = manager.engine_for_model("qwen3.5:4b")
    assert ollama_eng.ollama is True
    # A plain llama.cpp model stays on the configured endpoint without the flag.
    assert manager.engine_for_model("llama-3-8b").ollama is False
    manager.clear_cache()


# --- Context window detection (GET /v1/models, best-effort) --------------


def test_context_window_llamacpp_meta_n_ctx(config):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={
                "data": [{"id": "qwen3.8-27b", "meta": {"n_ctx": 131072},
                          "details": {"context_length": 262144}}],
            })
        return httpx.Response(404)
    eng, _ = _engine(config, handler)
    assert eng.get_context_window() == 131072


def test_context_window_ollama_details(config):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={
                "data": [{"id": "qwen3.5:4b",
                          "details": {"context_length": 32768}}],
            })
        return httpx.Response(404)
    eng, _ = _engine(config, handler)
    assert eng.get_context_window() == 32768


def test_context_window_missing_field_is_none(config):
    eng, _ = _engine(config, lambda r: httpx.Response(200, json={
        "data": [{"id": "m", "object": "model"}]}))
    assert eng.get_context_window() is None


def test_context_window_unreachable_is_none(config):
    eng, _ = _engine(config, lambda r: httpx.Response(500))
    assert eng.get_context_window() is None


def test_context_window_result_is_cached(config):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            calls.append(1)
            return httpx.Response(200, json={
                "data": [{"id": "m", "meta": {"n_ctx": 40960}}]})
        return httpx.Response(404)
    eng, _ = _engine(config, handler)
    assert eng.get_context_window() == 40960
    assert eng.get_context_window() == 40960
    assert len(calls) == 1  # detected exactly once


def test_budget_and_mock_use_window(config):
    """End-to-end: a detected 131k window lifts the transcript budget above
    the 24k default, so a 100k-char transcript is no longer trimmed."""
    from core.analysis import transcript_char_budget, build_prompt
    assert transcript_char_budget(config, None) == 24000
    big = transcript_char_budget(config, 131072)
    assert big > 24000
    segs = [(float(i), float(i) + 0.5, "A", "x" * 900) for i in range(120)]
    _sys, user_default = build_prompt("t", segs)
    _sys, user_big = build_prompt("t", segs, max_chars=big)
    assert len(user_default) < len(user_big)  # default trims, big window doesn't
