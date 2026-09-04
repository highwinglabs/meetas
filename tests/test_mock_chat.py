"""Deterministic mock-LLM chat support (RAG grounding path).

The default ``MockLLM`` answers RAG chat prompts (context blocks carrying
``seg:<id>`` markers) with a citation of the first real segment id, so the
grounded-answer / citation-validation pipeline is testable without any real
model. The analysis prompt behaviour (valid nine-section JSON) must stay
unchanged.
"""
from __future__ import annotations

import json

from core.llm import MockLLM
from core.search import rag as rag_mod

from tests.test_rag_sources import _build_meeting, _seg


def test_default_mock_grounded_chat_via_rag_answer():
    hits = [
        {"segment_id": "abc123", "meeting_id": "m1", "meeting_title": "Alpha",
         "meeting_date": "2026-04-02", "speaker_id": "Sprecher 1",
         "start_s": 0.0, "end_s": 2.0,
         "text": "Wir planen das Alpha Projekt fuer Q2.",
         "source_kind": "meeting"},
        {"segment_id": "def456", "meeting_id": "m1", "meeting_title": "Alpha",
         "meeting_date": "2026-04-02", "speaker_id": "Sprecher 1",
         "start_s": 2.5, "end_s": 4.5,
         "text": "Der Budgetrahmen bleibt unveraendert.",
         "source_kind": "meeting"},
    ]
    out = rag_mod.rag_answer("Was wird geplant?", hits, MockLLM())
    assert out["grounded"] is True
    assert out["citations"] == ["abc123"]
    assert len(out["sources"]) == 2
    assert out["sources"][0]["segment_id"] == "abc123"
    assert "Alpha Projekt" in out["answer"]
    assert "[seg:abc123]" in out["answer"]


def test_default_mock_document_hit_is_grounded():
    hits = [
        {"segment_id": "docabc", "meeting_id": None,
         "meeting_title": "notizen.txt", "meeting_date": None,
         "speaker_id": None, "start_s": None, "end_s": None,
         "text": "Q3-Budget: 42000 Euro fuer Lizenzen.",
         "source_kind": "document", "file_name": "notizen.txt",
         "locator": "Seite 1"},
    ]
    out = rag_mod.rag_answer("Wie hoch ist das Q3-Budget?", hits, MockLLM())
    assert out["grounded"] is True
    assert out["citations"] == ["docabc"]
    assert out["sources"][0]["source_kind"] == "document"
    assert "notizen.txt" in out["sources"][0]["file_name"]


def test_default_mock_declines_without_context():
    out = rag_mod.rag_answer("Was wird geplant?", [], MockLLM())
    assert out["grounded"] is False
    assert out["sources"] == []
    assert "Keine ausreichende Information" in out["answer"]


def test_default_mock_analysis_prompt_unchanged():
    prompt = (
        "Analysiere das Transkript und gib NUR das folgende JSON-Objekt aus.\n"
        "Transkript:\n[S1 | Sprecher: Anna | 00:00:00-00:00:04] Hallo."
    )
    res = MockLLM().complete(prompt)
    data = json.loads(res.text)
    assert len(data) == 9
    assert data["kurzfassung"][0]["quellen"][0]["segment_id"] == "S1"
    assert data["aufgaben"][0]["verantwortlich"] == "nicht angegeben"


def test_service_chat_grounded_with_default_mock(make_service):
    svc, mid, seg_ids = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Wir planen das Alpha Projekt fuer Q2."),
         _seg(2.5, 5.0, "Der Budgetrahmen bleibt unveraendert.")],
        title="Alpha",
        speaker_id="Sprecher 1",
    )
    out = svc.ask("Alpha Projekt Budget?", meeting_id=mid)
    assert out["grounded"] is True
    assert out["citations"] and all(c in seg_ids for c in out["citations"])
    assert out["sources"] and out["sources"][0]["segment_id"] in seg_ids
