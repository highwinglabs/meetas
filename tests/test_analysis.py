"""Phase 3 analysis pipeline tests (service/processor level, MockLLM only).

No real LLM request: every service here uses the injected ``MockLLM`` (via the
``finalize_meeting``/``make_service`` fixtures). The ASR engine is the mock too,
so we can get a deterministic 2-segment transcript to analyse.
"""
from __future__ import annotations

import json

import pytest

from core.analysis import build_prompt
from core.analysis import schema
from core.llm import LLMError, MockLLM, ServerBusyError
from core.service import UnknownMeetingError

NINE = ["kurzfassung", "themen", "entscheidungen", "aufgaben", "offene_fragen",
        "naechste_schritte", "risiken", "wichtige_fakten", "follow_ups"]


def _with_transcript(finalize_meeting, **kw):
    svc, mid = finalize_meeting(**kw)
    svc.transcribe(mid)  # mock ASR -> 2 deterministic segments
    return svc, mid


def test_analyze_stores_summary(config, finalize_meeting):
    svc, mid = _with_transcript(finalize_meeting)
    out = svc.analyze(mid)
    assert out["status"] == "done"
    assert out["kind"] == "summary"
    assert out["model"] == "mock-llm"
    # content is the validated structured JSON with all nine areas.
    data = json.loads(out["content"])
    assert set(NINE) <= set(data.keys())
    # every statement carries at least one source
    for key in NINE:
        for entry in data[key]:
            assert entry["text"].strip()
            assert entry["quellen"], f"{key} entry without a source"
            for q in entry["quellen"]:
                assert q["segment_id"] in ("S1", "S2")
                assert q["sprecher"] and q["timestamp"]
    # markdown rendering is available (UI/export)
    assert "Kurzfassung" in out["markdown"]

    detail = svc.get_meeting(mid)
    assert detail["status"] == "done"
    assert len(detail["analyses"]) == 1
    assert detail["analyses"][0]["model"] == "mock-llm"
    assert "Kurzfassung" in detail["analyses"][0]["markdown"]
    analyze_job = [j for j in detail["jobs"] if j["stage"] == "analyze"]
    assert analyze_job and analyze_job[0]["status"] == "done"


def test_analyze_prompt_is_structured(config):
    system, user = build_prompt(
        "Sprint-Planung",
        [(0.0, 1.5, "Sprecher 1", "Wir starten am Montag"), (1.5, 3.0, None, "OK, super")],
        "de",
    )
    # system: strict JSON contract, no invented facts, "nicht angegeben" sentinel
    assert "JSON" in system
    assert "nicht angegeben" in system
    # user: meeting + transcript with positional, citable segment ids
    assert "Sprint-Planung" in user
    assert "Wir starten am Montag" in user
    assert "[S1 | Sprecher: Sprecher 1" in user
    assert "[S2 |" in user
    # all nine mandatory areas are demanded, with the source fields
    for key in NINE:
        assert key in user
    assert "segment_id" in user and "sprecher" in user and "timestamp" in user


def test_analyze_idempotent_updates_single_row(config, finalize_meeting):
    svc, mid = _with_transcript(finalize_meeting)
    svc.analyze(mid)
    svc.analyze(mid)
    detail = svc.get_meeting(mid)
    assert len(detail["analyses"]) == 1  # upsert, never duplicated


def test_analyze_invalid_json_autofixes_once(config, finalize_meeting):
    mock = MockLLM(invalid_once=True)  # first answer broken, self-correction valid
    svc, mid = _with_transcript(finalize_meeting, llm_engine=mock)
    out = svc.analyze(mid)
    assert mock.calls == 2  # one automatic correction, no more
    assert out["status"] == "done"
    data = json.loads(out["content"])
    assert set(NINE) <= set(data.keys())
    # the correction was asked with the concrete errors + the real segments
    assert "Korrigiere" in mock.last_prompt
    assert "S1" in mock.last_prompt


def test_analyze_always_invalid_fails_and_stores_nothing(config, finalize_meeting):
    mock = MockLLM(always_invalid=True)  # even the one fix does not help
    svc, mid = _with_transcript(finalize_meeting, llm_engine=mock)
    with pytest.raises(LLMError) as exc:
        svc.analyze(mid)
    assert mock.calls == 2  # initial + exactly one fix, then give up
    assert "JSON" in str(exc.value)
    detail = svc.get_meeting(mid)
    assert detail["status"] == "failed"
    assert detail["analyses"] == []  # nothing invented / stored
    analyze_job = [j for j in detail["jobs"] if j["stage"] == "analyze"]
    assert analyze_job and analyze_job[0]["status"] == "failed"


def test_validate_analysis_flags_bad_source_and_missing_section():
    # a cited segment that does not exist + a missing area -> both reported
    normalised, errors = schema.validate_analysis(
        {k: [] for k in NINE[:-1]},  # drop "follow_ups" on purpose
        valid_segment_ids={"S1", "S2"},
    )
    assert normalised is None
    assert any("follow_ups" in e for e in errors)
    # a statement citing a phantom segment is rejected
    bad = {k: [] for k in NINE}
    bad["themen"] = [{"text": "X", "quellen": [
        {"segment_id": "S99", "sprecher": "A", "timestamp": "00:00:00-00:00:01"}]}]
    normalised2, errors2 = schema.validate_analysis(bad, {"S1", "S2"})
    assert normalised2 is None
    assert any("S99" in e for e in errors2)


def _valid_analysis_text():
    """A valid, schema-complete analysis citing the two mock segments (S1, S2)."""
    def q(sid):
        return {"segment_id": sid, "sprecher": "Sprecher 1",
                "timestamp": "00:00:00-00:00:02"}
    data = {
        "kurzfassung": [{"text": "Testzusammenfassung.", "quellen": [q("S1")]}],
        "themen": [{"text": "Testthema.", "quellen": [q("S1")]}],
        "entscheidungen": [],
        "aufgaben": [{"text": "Testaufgabe.", "verantwortlich": "nicht angegeben",
                      "deadline": "nicht angegeben", "quellen": [q("S2")]}],
        "offene_fragen": [],
        "naechste_schritte": [],
        "risiken": [],
        "wichtige_fakten": [],
        "follow_ups": [],
    }
    return json.dumps(data, ensure_ascii=False)


@pytest.mark.parametrize("wrap", ["thinking", "fence", "prose", "fenced_thinking"])
def test_analyze_parses_wrapped_json(config, finalize_meeting, wrap):
    """The robust extractor must recognise the analysis (and still validate
    sources) when Qwen wraps it in thinking blocks, fences, or prose."""
    body = _valid_analysis_text()
    if wrap == "thinking":
        text = "think\nich strukturiere die Antwort erst einmal.\n/think\n" + body
    elif wrap == "fence":
        text = "```json\n" + body + "\n```"
    elif wrap == "prose":
        text = ("Hier ist die Analyse wie gewünscht:\n" + body
                + "\nAlle Angaben stammen aus dem Transkript.")
    else:  # fenced_thinking
        text = ("think\nplanung: {struktur} zuerst.\n/think\n```json\n"
                + body + "\n```\nFertig.")

    mock = MockLLM(text=text)
    svc, mid = _with_transcript(finalize_meeting, llm_engine=mock)
    out = svc.analyze(mid)
    assert out["status"] == "done"
    assert mock.calls == 1  # recognised on the first try, no correction needed
    data = json.loads(out["content"])
    assert set(NINE) <= set(data.keys())
    # sources were validated against the real segments
    for entry in data["aufgaben"]:
        for src in entry["quellen"]:
            assert src["segment_id"] in ("S1", "S2")


def test_analyze_wrapped_json_still_enforces_sources(config, finalize_meeting):
    """Recognition is robust, but a wrapped answer that cites a phantom segment
    must still fail (no invented sources), even though the object is found."""
    bad = _valid_analysis_text()
    bad = bad.replace('"S1"', '"S99"')
    text = "think\nnachdenken.\n/think\n```json\n" + bad + "\n```"
    mock = MockLLM(text=text)
    svc, mid = _with_transcript(finalize_meeting, llm_engine=mock)
    with pytest.raises(LLMError):
        svc.analyze(mid)
    assert mock.calls == 2  # one correction attempted, then a clear failure
    assert svc.get_meeting(mid)["analyses"] == []


def test_analyze_no_transcript_raises_without_status_flip(config, finalize_meeting):
    svc, mid = finalize_meeting()  # captured but NOT transcribed
    with pytest.raises(LLMError):
        svc.analyze(mid)
    assert svc.get_meeting(mid)["status"] == "ready"  # precondition, not failure


def test_analyze_unknown_meeting_404(config, finalize_meeting):
    svc, _ = finalize_meeting()
    with pytest.raises(UnknownMeetingError):
        svc.analyze("nope")


def test_analyze_failure_marks_job_and_meeting_failed(config, finalize_meeting):
    svc, mid = _with_transcript(finalize_meeting, llm_engine=MockLLM(fail=True))
    with pytest.raises(LLMError):
        svc.analyze(mid)
    detail = svc.get_meeting(mid)
    assert detail["status"] == "failed"
    analyze_job = [j for j in detail["jobs"] if j["stage"] == "analyze"]
    assert analyze_job and analyze_job[0]["status"] == "failed"


def test_analyze_busy_raises_server_busy(config, finalize_meeting):
    svc, mid = _with_transcript(finalize_meeting, llm_engine=MockLLM(busy=True))
    with pytest.raises(ServerBusyError):
        svc.analyze(mid)
    # A busy server is not a hard failure of the analysis content; the job is
    # recorded as failed with the understandable message.
    analyze_job = [j for j in svc.get_meeting(mid)["jobs"] if j["stage"] == "analyze"]
    assert analyze_job and analyze_job[0]["status"] == "failed"


def test_llm_status_reports_config_and_no_network(config, finalize_meeting):
    svc, _ = finalize_meeting()
    st = svc.llm_status()
    assert st["mock"] is True  # default test engine is the mock
    assert st["local"] is True
    assert st["network_used"] is False
    assert st["model"] == "qwen3.8-27b-q4kxl"
