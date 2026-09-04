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
    LLMError, LLMUnavailableError, MockLLM, OpenAICompatibleLLM, ServerBusyError,
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


def test_http_400_is_llm_error(config):
    eng, _ = _engine(config, lambda r: httpx.Response(400, text="bad request"))
    with pytest.raises(LLMError):
        eng.complete("p")


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
