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
from core.store.db import session_scope
from core.store.models import Meeting

NINE = ["kurzfassung", "themen", "entscheidungen", "aufgaben", "offene_fragen",
        "naechste_schritte", "risiken", "wichtige_fakten", "follow_ups"]


def _with_transcript(finalize_meeting, **kw):
    svc, mid = finalize_meeting(**kw)
    svc.transcribe(mid)  # mock ASR -> 2 deterministic segments
    return svc, mid


def _set_meeting_settings(svc, mid, **settings):
    """Merge per-meeting settings into a stored meeting (production read path)."""
    with session_scope() as s:
        m = s.get(Meeting, mid)
        existing = json.loads(m.settings_json or "{}")
        existing.update(settings)
        m.settings_json = json.dumps(existing, ensure_ascii=False)
        s.commit()


def test_analyze_stores_summary(config, finalize_meeting):
    svc, mid = _with_transcript(finalize_meeting)
    out = svc.analyze(mid)
    assert out["status"] == "done"
    assert out["kind"] == "summary"
    assert out["model"] == "mock-llm"
    # content is the validated structured JSON with all nine areas.
    data = json.loads(out["content"])
    assert set(NINE) <= set(data.keys())
    # every statement carries at least one fully resolved source
    for key in NINE:
        for entry in data[key]:
            assert entry["text"].strip()
            assert entry["quellen"], f"{key} entry without a source"
            for q in entry["quellen"]:
                assert q["sid"] in ("S1", "S2")
                assert q["segment_id"]  # real segment id resolved from the row
                assert q["sprecher"] and q["timestamp"]
                assert "snippet" in q
    # markdown rendering is available (UI/export)
    assert "Kurzfassung" in out["markdown"]

    detail = svc.get_meeting(mid)
    assert detail["status"] == "done"
    assert len(detail["analyses"]) == 1
    assert detail["analyses"][0]["model"] == "mock-llm"
    assert "Kurzfassung" in detail["analyses"][0]["markdown"]
    analyze_job = [j for j in detail["jobs"] if j["stage"] == "analyze"]
    assert analyze_job and analyze_job[0]["status"] == "done"


def test_analyze_applies_per_meeting_output_language(config, finalize_meeting):
    """The per-meeting analysis_language flows into the sent system prompt.

    The mock transcript is German, so a German meeting with "wie_transkript"
    writes in German, while analysis_language="en" switches the OUTPUT to
    English without changing how the transcript is described."""
    # German transcript + explicit English output -> English directive, German transcript.
    mock = MockLLM()
    svc, mid = _with_transcript(finalize_meeting, llm_engine=mock)
    _set_meeting_settings(svc, mid, analysis_language="en")
    out = svc.analyze(mid)
    assert out["status"] == "done"
    assert "auf Englisch aus" in mock.last_system
    assert "deutsches Transkript" in mock.last_system

    # "wie_transkript" resolves to the transcript language (German).
    mock2 = MockLLM()
    svc2, mid2 = _with_transcript(finalize_meeting, llm_engine=mock2)
    _set_meeting_settings(svc2, mid2, analysis_language="wie_transkript")
    svc2.analyze(mid2)
    assert "auf Deutsch aus" in mock2.last_system
    assert "deutsches Transkript" in mock2.last_system


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
    # all nine mandatory areas are demanded; sources are cited as S-id lists
    for key in NINE:
        assert key in user
    assert '"quellen": ["S1"]' in user  # example shows the S-id citation form
    assert "S-Nummern" in user  # rule explains the positional ids


def test_analyze_system_prompt_transcript_language(config):
    """The transcript-language descriptor is the ONLY prompt part that adapts.

    German (or absent/unknown) must stay byte-identical to the historical prompt
    (no migration, no behaviour change); a known foreign language swaps exactly
    the single phrase naming the transcript's language and nothing else.
    """
    base = schema.SYSTEM_PROMPT
    # Absent / empty / German / regional German / unknown all fall back to base.
    for lang in (None, "", "de", "de-DE", "xx-XX"):
        assert schema.system_prompt(lang) == base
    # English names the transcript "englisches" and drops the German descriptor.
    en = schema.system_prompt("en")
    assert "englisches Transkript" in en
    assert "deutsches Transkript" not in en
    # Reverting that single phrase restores the base prompt: proof that ONLY the
    # descriptor changed and the rest of the (German) prompt is untouched.
    assert en.replace("englisches Transkript", "deutsches Transkript") == base
    # The German sentinel is preserved: no output-language switch is implied.
    assert "nicht angegeben" in en
    # Template variants keep their emphasis suffix on top of the same base.
    audit_en = schema.system_prompt_for_template("audit", "en")
    assert "englisches Transkript" in audit_en
    assert audit_en.startswith(en)  # base (English) prompt first, suffix appended
    assert "Risiken" in audit_en


def test_resolve_output_lang():
    """wie_transkript/empty/auto follow the transcript; a concrete code wins."""
    # "match the transcript": concrete transcript language is used.
    assert schema.resolve_output_lang(None, "de") == "de"
    assert schema.resolve_output_lang("", "en") == "en"
    assert schema.resolve_output_lang("auto", "fr") == "fr"
    assert schema.resolve_output_lang("wie_transkript", "de-DE") == "de-DE"
    # "match the transcript" but transcript unknown -> no concrete output language.
    assert schema.resolve_output_lang("wie_transkript", None) is None
    assert schema.resolve_output_lang("wie_transkript", "") is None
    # A fixed choice overrides the transcript language entirely.
    assert schema.resolve_output_lang("de", None) == "de"
    assert schema.resolve_output_lang("en", "de") == "en"
    # Unknown/blank choice degrades to the transcript language (or None).
    assert schema.resolve_output_lang("  ", None) is None


def test_analyze_system_prompt_output_language():
    """The explicit output-language directive is the only other prompt part that
    adapts, and it appears ONLY when a concrete output language is resolved."""
    base = schema.SYSTEM_PROMPT
    # No output language -> byte-identical to the historical prompt.
    assert schema.system_prompt("de", None) == base
    assert schema.system_prompt("en", None) == schema.system_prompt("en")
    # "wie_transkript" + German transcript -> explicit German output.
    de = schema.system_prompt("de", "de")
    assert de != base
    assert "auf Deutsch aus" in de
    # "wie_transkript" + English transcript -> explicit English output.
    assert "auf Englisch aus" in schema.system_prompt("en", "en")
    # A concrete output choice is independent of the transcript language: an
    # English transcript can still be summarised in German (and vice versa).
    cross = schema.system_prompt("en", "de")
    assert "englisches Transkript" in cross
    assert "auf Deutsch aus" in cross
    assert "auf Englisch aus" not in cross
    # The no-invention contract survives the directive.
    assert "nicht angegeben" in de
    # Template variants keep their emphasis suffix on top of the same prompt.
    audit = schema.system_prompt_for_template("audit", "de", "en")
    assert audit.startswith(schema.system_prompt("de", "en"))
    assert "Risiken" in audit


def _sample_analysis():
    """A minimal normalised analysis to exercise the localised renderer."""
    return {
        "kurzfassung": [],  # empty -> the "not specified" placeholder is shown
        "themen": [{"text": "T", "quellen": [
            {"segment_id": "S1", "sprecher": "A", "timestamp": "0:00"}]}],
        "entscheidungen": [],
        "aufgaben": [{"text": "A", "verantwortlich": "nicht angegeben",
                      "deadline": "nicht angegeben", "quellen": [
            {"segment_id": "S2", "sprecher": "B", "timestamp": "0:00"}]}],
        "offene_fragen": [],
        "naechste_schritte": [],
        "risiken": [],
        "wichtige_fakten": [],
        "follow_ups": [],
    }


def test_render_markdown_default_is_german():
    """Omitting lang reproduces the historical German output exactly."""
    md = schema.render_markdown(_sample_analysis())
    assert "## Kurzfassung" in md
    assert "## Aufgaben / Action Items" in md
    assert "- nicht angegeben" in md
    assert "Verantwortlich: nicht angegeben" in md
    # sources are rendered as global [n] references + a Quellen list
    assert "- T [1]" in md
    assert "- A [2]" in md
    assert "## Quellen" in md
    assert "1. A \u00b7 0:00" in md
    # explicit "de", a German region tag and an unknown code stay byte-identical.
    assert schema.render_markdown(_sample_analysis(), lang="de") == md
    assert schema.render_markdown(_sample_analysis(), lang="de-DE") == md
    assert schema.render_markdown(_sample_analysis(), lang="xx") == md


def test_render_markdown_without_sources_has_no_quellen_section():
    data = _sample_analysis()
    for key in data:
        for e in data[key]:
            e["quellen"] = []
    md = schema.render_markdown(data)
    assert "## Quellen" not in md
    assert "[1]" not in md


def test_render_markdown_localised_english():
    md = schema.render_markdown(_sample_analysis(), lang="en")
    assert "## Summary" in md
    assert "## Action Items" in md
    assert "- not specified" in md
    assert "Owner: not specified" in md
    assert "Deadline: not specified" in md
    assert "Source:" not in md
    assert "## Sources" in md  # localised Quellen heading
    # the German headings / labels / sentinel must be gone
    assert "Kurzfassung" not in md
    assert "Verantwortlich" not in md
    assert "nicht angegeben" not in md
    # content produced by the model is preserved verbatim (not translated)
    assert "- T" in md and "- A" in md


def test_missing_detection_and_localised_labels():
    # the English sentinels are recognised as missing (no invented values)
    assert schema.is_missing("not specified") is True
    assert schema.is_missing("not given") is True
    assert schema.is_missing("Ben") is False
    # localised display helpers fall back to German for anything unsupported
    assert schema.missing_text(None) == "nicht angegeben"
    assert schema.missing_text("en") == "not specified"
    assert schema.missing_text("xx") == "nicht angegeben"
    assert schema.section_title("kurzfassung", "en") == "Summary"
    assert schema.section_title("kurzfassung", None) == "Kurzfassung"
    assert schema.analysis_label("verantwortlich", "en") == "Owner"
    assert schema.analysis_label("quelle", "en") == "Source"
    # unknown names fall back to the key itself, in either language
    assert schema.analysis_label("frist", "de") == "frist"
    assert schema.analysis_label("frist", "en") == "frist"


def test_analyze_stores_output_lang(config, finalize_meeting):
    """The resolved output language is persisted on the analysis row and returned.

    It drives the localised markdown (headings + placeholder), while the model's
    text content is stored verbatim."""
    # Default: German transcript + "wie_transkript" -> "de".
    svc, mid = _with_transcript(finalize_meeting)
    out = svc.analyze(mid)
    assert out["output_lang"] == "de"
    assert svc.get_meeting(mid)["analyses"][0]["output_lang"] == "de"
    assert "## Kurzfassung" in out["markdown"]

    # Explicit English output -> stored "en" and a localised markdown.
    svc2, mid2 = _with_transcript(finalize_meeting, llm_engine=MockLLM())
    _set_meeting_settings(svc2, mid2, analysis_language="en")
    out2 = svc2.analyze(mid2)
    assert out2["output_lang"] == "en"
    a2 = svc2.get_meeting(mid2)["analyses"][0]
    assert a2["output_lang"] == "en"
    assert "## Summary" in out2["markdown"]
    assert "Owner: not specified" in out2["markdown"]
    assert "## Summary" in a2["markdown"]  # detail markdown follows stored lang


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


def _rows():
    # rows with real segment ids, as the processor builds them
    return schema.to_seg_rows([
        (0.0, 1.0, "A", "Erster Satz aus dem Transkript.", "real-uuid-1"),
        (65.0, 67.0, "B", "Zweiter Satz aus dem Transkript.", "real-uuid-2"),
    ])


def test_validate_analysis_resolves_sources_from_rows():
    # a missing area is still reported
    normalised, errors = schema.validate_analysis(
        {k: [] for k in NINE[:-1]},  # drop "follow_ups" on purpose
        _rows(),
    )
    assert normalised is None
    assert any("follow_ups" in e for e in errors)
    # sources are optional: a statement without sources is valid, and a
    # phantom source (S99) is dropped silently instead of failing the output
    bad = {k: [] for k in NINE}
    bad["themen"] = [
        {"text": "X", "quellen": ["S99"]},  # phantom S-id (string form)
        {"text": "Y"},  # no sources at all (small-model style output)
    ]
    normalised2, errors2 = schema.validate_analysis(bad, _rows())
    assert errors2 == []
    assert normalised2 is not None
    assert normalised2["themen"][0]["quellen"] == []
    assert normalised2["themen"][1]["quellen"] == []
    # the new string form is resolved fully from the row (never model-invented)
    good = {k: [] for k in NINE}
    good["themen"] = [{"text": "Z", "quellen": ["S2", "S1", "S1"]}]  # dup dropped
    normalised3, errors3 = schema.validate_analysis(good, _rows())
    assert errors3 == []
    assert normalised3 is not None
    assert normalised3["themen"][0]["quellen"] == [
        {"sid": "S2", "segment_id": "real-uuid-2", "sprecher": "B",
         "timestamp": "00:01:05", "snippet": "Zweiter Satz aus dem Transkript."},
        {"sid": "S1", "segment_id": "real-uuid-1", "sprecher": "A",
         "timestamp": "00:00:00", "snippet": "Erster Satz aus dem Transkript."},
    ]
    # the legacy dict form (older stored data / model output) still validates
    legacy = {k: [] for k in NINE}
    legacy["themen"] = [{"text": "L", "quellen": [
        {"segment_id": "S1", "sprecher": "ignored", "timestamp": "ignored"}]}]
    normalised4, errors4 = schema.validate_analysis(legacy, _rows())
    assert errors4 == []
    assert normalised4["themen"][0]["quellen"][0]["sid"] == "S1"
    assert normalised4["themen"][0]["quellen"][0]["sprecher"] == "A"  # from the row


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
    # sources were resolved against the real segments
    for entry in data["aufgaben"]:
        for src in entry["quellen"]:
            assert src["sid"] in ("S1", "S2")
            assert src["segment_id"]  # real segment id from the transcript row


def test_analyze_wrapped_json_drops_phantom_sources(config, finalize_meeting):
    """Sources are optional: a wrapped answer citing only phantom segments is
    stored successfully, with the invented sources dropped (no correction round)."""
    bad = _valid_analysis_text()
    bad = bad.replace('"S1"', '"S99"').replace('"S2"', '"S99"')
    text = "think\nnachdenken.\n/think\n```json\n" + bad + "\n```"
    mock = MockLLM(text=text)
    svc, mid = _with_transcript(finalize_meeting, llm_engine=mock)
    out = svc.analyze(mid)
    assert out["status"] == "done"
    assert mock.calls == 1  # phantom sources are dropped, not rejected
    data = json.loads(out["content"])
    for key in NINE:
        for entry in data[key]:
            assert entry["quellen"] == []


def test_analyze_accepts_analysis_without_sources(config, finalize_meeting):
    """Regression: small models (e.g. Gemma) often omit the sources entirely.
    A complete analysis without any 'quellen' is a VALID summary: it must be
    stored on the first try, without a correction round and without failure."""
    data = {
        "kurzfassung": [{"text": "Austausch ueber das Projekt."}],
        "themen": [{"text": "Roadmap"}, {"text": "Budget"}],
        "entscheidungen": [{"text": "Budget freigegeben"}],
        "aufgaben": [{"text": "Konzept schreiben",
                      "verantwortlich": "nicht angegeben",
                      "deadline": "nicht angegeben"}],
        "offene_fragen": [{"text": "Wann geht es weiter?"}],
        "naechste_schritte": [],
        "risiken": [],
        "wichtige_fakten": [],
        "follow_ups": [],
    }
    mock = MockLLM(text=json.dumps(data, ensure_ascii=False))
    svc, mid = _with_transcript(finalize_meeting, llm_engine=mock)
    out = svc.analyze(mid)
    assert out["status"] == "done"
    assert mock.calls == 1  # no correction round needed
    parsed = json.loads(out["content"])
    assert parsed["kurzfassung"] == [{"text": "Austausch ueber das Projekt.",
                                      "quellen": []}]
    assert parsed["aufgaben"][0]["verantwortlich"] == "nicht angegeben"
    # sources are not rendered into the markdown either
    assert "Quelle:" not in out["markdown"]
    assert svc.get_meeting(mid)["status"] == "done"


def test_transcript_char_budget_follows_context_window():
    """The transcript budget derives from the model's context window.

    Unknown/too-small windows keep the safe default; a 131k window fits all of
    the user's real meetings (largest transcript ~177k chars)."""
    from core.analysis import transcript_char_budget
    cfg = type("C", (), {"llm_max_tokens": 8192})()
    # unknown / tiny / bogus windows -> the historical safe default
    assert transcript_char_budget(cfg, None) == 24000
    assert transcript_char_budget(cfg, 0) == 24000
    assert transcript_char_budget(cfg, 16384) == 24000
    assert transcript_char_budget(cfg, "bogus") == 24000
    # 131072 - max(8192, 131072//8) - 4096 = 110592 tokens * 3 = 331776 chars
    assert transcript_char_budget(cfg, 131072) == 331776
    # a larger configured answer budget shrinks the transcript room
    cfg2 = type("C", (), {"llm_max_tokens": 32000})()
    assert transcript_char_budget(cfg2, 131072) < 331776


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
