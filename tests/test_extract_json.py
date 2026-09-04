"""Unit tests for robust JSON recognition in the analysis pipeline.

The local Qwen model can wrap its answer in ``...`` chain-of-thought blocks,
Markdown ```json fences, or explanatory prose. :func:`core.analysis.schema.
extract_json` must recognise the analysis object in all of these shapes (and
still return ``None`` for a genuinely non-JSON answer). These are pure
parsing tests -- no DB, no LLM.
"""
from __future__ import annotations

import json

from core.analysis import schema


def _valid():
    """A minimal, schema-complete analysis (nine keys, one sourced statement)."""
    return {
        "kurzfassung": [{"text": "Kurze Zusammenfassung.",
                         "quellen": [{"segment_id": "S1", "sprecher": "Anna",
                                      "timestamp": "00:00:00-00:00:04"}]}],
        "themen": [{"text": "Thema A.",
                    "quellen": [{"segment_id": "S1", "sprecher": "Anna",
                                 "timestamp": "00:00:00-00:00:04"}]}],
        "entscheidungen": [],
        "aufgaben": [{"text": "Aufgabe X.", "verantwortlich": "nicht angegeben",
                      "deadline": "nicht angegeben",
                      "quellen": [{"segment_id": "S1", "sprecher": "Anna",
                                   "timestamp": "00:00:00-00:00:04"}]}],
        "offene_fragen": [],
        "naechste_schritte": [],
        "risiken": [],
        "wichtige_fakten": [],
        "follow_ups": [],
    }


def _json_str():
    return json.dumps(_valid(), ensure_ascii=False, indent=2)


def test_pure_json():
    data = schema.extract_json(_json_str())
    assert isinstance(data, dict)
    assert set(schema.SECTION_KEYS) <= set(data.keys())


def test_json_in_markdown_fence():
    text = "```json\n" + _json_str() + "\n```"
    data = schema.extract_json(text)
    assert isinstance(data, dict)
    assert data["kurzfassung"][0]["text"] == "Kurze Zusammenfassung."


def test_json_with_thinking_block():
    # Thinking block that itself contains braces and even a tiny JSON object --
    # the extractor must skip it and return the real analysis object.
    text = (
        "think\n"
        "Ich sollte zuerst die Struktur aufbauen, z. B. {\"kurzfassung\": []} "
        "und dann die Quellen zuweisen.\n"
        "/think\n"
        + _json_str()
    )
    data = schema.extract_json(text)
    assert isinstance(data, dict)
    # The spurious tiny object from the thinking block must NOT win.
    assert "themen" in data and "aufgaben" in data
    assert set(schema.SECTION_KEYS) <= set(data.keys())


def test_text_before_and_after_json():
    text = ("Hier ist die angeforderte Analyse:\n\n"
            + _json_str()
            + "\n\nFertig. Alle Quellen sind aus dem Transkript.")
    data = schema.extract_json(text)
    assert isinstance(data, dict)
    assert data["aufgaben"][0]["text"] == "Aufgabe X."


def test_surrounding_whitespace_and_newlines():
    text = "\n   \n\t" + _json_str() + "\n\n  "
    data = schema.extract_json(text)
    assert isinstance(data, dict)


def test_braces_in_string_values_do_not_break():
    # A string value containing literal braces must not truncate the object.
    obj = _valid()
    obj["wichtige_fakten"] = [
        {"text": 'Formel a { b } c in Anführungszeichen',
         "quellen": [{"segment_id": "S1", "sprecher": "Anna",
                      "timestamp": "00:00:00-00:00:04"}]}]
    data = schema.extract_json(json.dumps(obj, ensure_ascii=False))
    assert isinstance(data, dict)
    assert data["wichtige_fakten"][0]["text"].count("{") == 1


def test_unescaped_quotes_inside_text_value():
    # The model embeds straight quotes in a text field without escaping them.
    # The premature quote ends the JSON string early and, left alone, desyncs
    # the balanced-scan so the top-level object is never found. The quote
    # repair must recover it and keep the literal quotes inside the value.
    raw = """{
  "kurzfassung": [
    {
      "text": "Die Frage "Wie geht's?" wurde gestellt.",
      "quellen": [{"segment_id": "S1", "sprecher": "Anna", "timestamp": "00:00:00-00:00:03"}]
    }
  ],
  "themen": [], "entscheidungen": [], "aufgaben": [], "offene_fragen": [],
  "naechste_schritte": [], "risiken": [], "wichtige_fakten": [], "follow_ups": []
}"""
    obj = schema.extract_json(raw)
    assert isinstance(obj, dict) and "kurzfassung" in obj
    assert obj["kurzfassung"][0]["text"] == 'Die Frage "Wie geht\'s?" wurde gestellt.'
    norm, errors = schema.validate_analysis(obj, {"S1"})
    assert errors == [] and norm is not None
    # A genuinely valid object with escaped quotes must pass through unchanged.
    valid = json.dumps(_valid(), ensure_ascii=False)
    assert schema._repair_unescaped_quotes(valid) == valid


def test_invalid_answer_returns_none():
    assert schema.extract_json(
        "Leider kann ich das Transkript nicht analysieren, es ist zu kurz.") is None
    assert schema.extract_json("") is None
    assert schema.extract_json(None) is None
    # Only an unclosed / non-object payload.
    assert schema.extract_json("ein Wert: [1, 2, 3]") is None


def test_multiple_objects_picks_the_analysis():
    # A small, non-analysis object appears before the real one in prose.
    text = ("Notiz: {\"status\": \"ok\"}\n\n" + _json_str())
    data = schema.extract_json(text)
    assert isinstance(data, dict)
    assert set(schema.SECTION_KEYS) <= set(data.keys())  # the analysis, not {status}


def test_source_checking_still_enforced():
    # Recognition is robust, but validation (sources) is NOT relaxed: a
    # recognised object that cites a phantom segment must still be rejected.
    obj = _valid()
    obj["themen"][0]["quellen"][0]["segment_id"] = "S99"  # does not exist
    data = schema.extract_json(json.dumps(obj, ensure_ascii=False))
    normalised, errors = schema.validate_analysis(data, valid_segment_ids={"S1"})
    assert normalised is None
    assert any("S99" in e for e in errors)
