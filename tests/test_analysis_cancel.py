"""User-initiated stop of the LLM analysis.

Covers the neutral ``cancelled`` job state (deliberately *not* an error), the
pre-set cancel event short-circuit (no LLM call at all), re-triggering after a
stop, ``cancel_analysis`` on a running job, and the pipeline refusing to
auto-resume a user-stopped stage. MockLLM/httpx.MockTransport only -- no real
LLM request.
"""
from __future__ import annotations

import threading

import httpx
import pytest

from core.analysis.processor import AnalysisProcessor
from core.jobs.queue import JobQueue
from core.llm import LLMCancelledError, MockLLM, OpenAICompatibleLLM
from core.service import UnknownMeetingError
from core.store.db import session_scope
from core.store.models import Meeting


def _with_transcript(finalize_meeting):
    svc, mid = finalize_meeting()
    svc.transcribe(mid)  # mock ASR -> deterministic segments
    return svc, mid


def _analyze_job(detail):
    return next(j for j in detail["jobs"] if j["stage"] == "analyze")


def test_cancel_event_preset_short_circuits_without_llm_call(config, finalize_meeting):
    svc, mid = _with_transcript(finalize_meeting)
    mock = MockLLM()
    event = threading.Event()
    event.set()
    out = AnalysisProcessor(svc.config).process(
        mid, mock, cancel_event=event, still_current=lambda: True)
    assert out["status"] == "cancelled"
    assert mock.calls == 0  # no LLM request at all

    detail = svc.get_meeting(mid)
    job = _analyze_job(detail)
    assert job["status"] == "cancelled"
    assert job["error"] is None  # a stop is not an error
    assert detail["status"] == "ready"
    assert detail["analyses"] == []  # nothing stored


def test_cancelled_analysis_can_be_restarted(config, finalize_meeting):
    svc, mid = _with_transcript(finalize_meeting)
    event = threading.Event()
    event.set()
    AnalysisProcessor(svc.config).process(mid, MockLLM(), cancel_event=event)
    assert _analyze_job(svc.get_meeting(mid))["status"] == "cancelled"

    # Explicit re-trigger resets the cancelled job to running/done.
    out = svc.analyze(mid)
    assert out["status"] == "done"
    assert len(svc.get_meeting(mid)["analyses"]) == 1


def test_cancel_analysis_on_running_job(config, finalize_meeting):
    svc, mid = _with_transcript(finalize_meeting)
    # Simulate the state the processor leaves during the (slow) LLM call.
    with session_scope() as s:
        job = JobQueue.get_or_create(s, mid, "analyze")
        JobQueue.mark_running(s, job)
        s.get(Meeting, mid).status = "analyzing"
        s.commit()

    out = svc.cancel_analysis(mid)
    assert out["status"] == "cancelled"
    detail = svc.get_meeting(mid)
    assert _analyze_job(detail)["status"] == "cancelled"
    assert detail["status"] == "ready"

    # Idempotent: cancelling again changes nothing.
    assert svc.cancel_analysis(mid)["status"] == "cancelled"
    assert _analyze_job(svc.get_meeting(mid))["status"] == "cancelled"


def test_cancel_analysis_unknown_meeting(config, finalize_meeting):
    svc, _ = _with_transcript(finalize_meeting)
    with pytest.raises(UnknownMeetingError):
        svc.cancel_analysis("does-not-exist")


def test_pipeline_does_not_resume_cancelled_analyze(config, finalize_meeting):
    svc, mid = _with_transcript(finalize_meeting)
    with session_scope() as s:
        JobQueue.mark_cancelled(s, JobQueue.get_or_create(s, mid, "analyze"))
        # State as left by a pipeline that stopped at the user-stopped stage.
        s.get(Meeting, mid).status = "ready"
        s.commit()
    svc.config.auto_analyze = True
    assert svc.resume_pending_pipelines() == 0  # user stop is terminal
    assert svc.get_meeting(mid)["analyses"] == []


def test_complete_honours_set_cancel_event_without_request(config):
    calls: list = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    eng = OpenAICompatibleLLM(
        base_url="http://testserver/v1", model="m", config=config,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    event = threading.Event()
    event.set()
    with pytest.raises(LLMCancelledError):
        eng.complete("prompt", cancel_event=event)
    assert calls == []  # request never sent


def test_busy_retry_stops_on_cancel_event(config):
    config.llm_busy_wait_s = 0.01
    config.llm_max_busy_retries = 5
    state = {"n": 0, "event": threading.Event()}

    def handler(req):
        state["n"] += 1
        if state["n"] >= 2:
            state["event"].set()
        return httpx.Response(503, text="busy")

    eng = OpenAICompatibleLLM(
        base_url="http://testserver/v1", model="m", config=config,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep_fn=lambda _s: None,
    )
    with pytest.raises(LLMCancelledError):
        eng.complete("prompt", cancel_event=state["event"])
    assert state["n"] == 2  # stopped after the retry check, not after all 5
