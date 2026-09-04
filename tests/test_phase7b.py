"""Phase 7b: export selection (html + pdf/docx fallback), compare, multi-summary,
recurring topics, timeline. All offline / deterministic."""
from __future__ import annotations

import json

from core.store.db import session_scope
from core.store.models import Analysis, MeetingTag, TranscriptSegment


def _mk_analysis(kf, themen):
    return json.dumps({
        "kurzfassung": [{"text": kf, "quellen": []}],
        "themen": [{"text": themen, "quellen": []}],
        "entscheidungen": [{"text": "Budget freigegeben", "quellen": []}],
        "aufgaben": [{"text": "Bericht schreiben", "verantwortlich": "Alex",
                      "deadline": "nicht angegeben", "quellen": []}],
        "offene_fragen": [{"text": "Wann Review?", "quellen": []}],
        "naechste_schritte": [], "risiken": [], "wichtige_fakten": [], "follow_ups": [],
    }, ensure_ascii=False)


def _finalize_two(make_service):
    svc = make_service()
    mids = []
    for title, kf, themen in (("Sprint Q2", "Budget Q2 freigegeben", "Budget"),
                              ("Retro Juli", "Retro abgeschlossen", "Retro")):
        mid = svc.start_meeting(title=title, source="mic")
        svc._sessions[mid].wait_done(timeout=10)
        svc.stop(mid)
        with session_scope() as s:
            s.add(TranscriptSegment(id=f"sg-{mid[:4]}", meeting_id=mid, start_s=0,
                                    end_s=1, text="hallo", raw_text="x",
                                    speaker_id="S1", status="bestaetigt"))
            s.add(MeetingTag(meeting_id=mid, tag="budget"))
            s.add(Analysis(meeting_id=mid, kind="summary", model="mock",
                           content=_mk_analysis(kf, themen)))
            s.commit()
        mids.append(mid)
    return svc, mids


def test_compare(make_service):
    svc, (m1, m2) = _finalize_two(make_service)
    out = svc.compare_meetings([m1, m2])
    assert out["n_meetings"] == 2
    r0 = out["meetings"][0]
    assert r0["title"] == "Sprint Q2" and r0["n_segments"] == 1 and r0["n_tasks"] == 0
    assert "budget" in r0["tags"]
    assert r0["kurzfassung"] == ["Budget Q2 freigegeben"]


def test_multi_summary(make_service):
    svc, (m1, m2) = _finalize_two(make_service)
    out = svc.multi_summary([m1, m2])
    assert out["n_meetings"] == 2
    assert len(out["digest"]["kurzfassung"]) == 2
    titles = {e["meeting"] for e in out["digest"]["kurzfassung"]}
    assert titles == {"Sprint Q2", "Retro Juli"}
    assert all(e["verantwortlich"] == "Alex" for e in out["digest"]["aufgaben"])


def test_multi_summary_date_range(make_service):
    svc, (m1, m2) = _finalize_two(make_service)
    # Both meetings are "now", so a wide window returns both; a past one returns none.
    wide = svc.multi_summary(since="2000-01-01", until="2100-01-01")
    assert wide["n_meetings"] == 2
    empty = svc.multi_summary(since="2100-01-01", until="2200-01-01")
    assert empty["n_meetings"] == 0


def test_recurring_topics(make_service):
    svc, (m1, m2) = _finalize_two(make_service)
    out = svc.recurring_topics(min_occurrences=2)
    topics = {t["topic"] for t in out["topics"]}
    # "budget" tag is on both meetings -> recurring
    assert "budget" in topics
    # "Budget" themen (lowercased) also on both
    assert "budget" in topics


def test_timeline(make_service):
    svc, (m1, m2) = _finalize_two(make_service)
    day = svc.timeline("day")
    assert day["granularity"] == "day"
    assert sum(b["n_meetings"] for b in day["buckets"]) == 2
    week = svc.timeline("week")
    assert any("-W" in b["key"] for b in week["buckets"])


def test_export_html(make_service):
    svc, (m1, _) = _finalize_two(make_service)
    out = svc.export_meeting(m1, "html")
    assert out["format"] == "html" and out["fallback"] is None
    assert out["content"].startswith("<!doctype html>")
    assert "Sprint Q2" in out["content"]


def _pdf_tooling_available() -> bool:
    try:
        from core.export import _pandoc_available, _pdf_engine
        return _pandoc_available() and _pdf_engine() is not None
    except Exception:
        return False


def test_export_pdf_uses_pandoc_or_falls_back(make_service):
    svc, (m1, _) = _finalize_two(make_service)
    out = svc.export_meeting(m1, "pdf")
    if _pdf_tooling_available():
        # pandoc + PDF engine present -> a real, binary PDF
        assert out["fallback"] is None
        assert out["format"] == "pdf"
        assert out["is_binary"] is True
    else:
        # tooling absent -> clean fallback to html with an explicit note
        assert out["fallback"] == "html"
        assert out["format"] == "html"
        assert out["note"] and "HTML" in out["note"]


def test_export_docx_uses_local_tooling_or_falls_back(make_service):
    svc, (m1, _) = _finalize_two(make_service)
    out = svc.export_meeting(m1, "docx")
    try:
        import docx  # noqa: F401
    except ImportError:
        assert out["fallback"] == "markdown" and out["format"] == "markdown"
        assert out["note"] and "Markdown" in out["note"]
    else:
        assert out["fallback"] is None and out["format"] == "docx"


def test_export_section_selection(make_service):
    svc, (m1, _) = _finalize_two(make_service)
    out = svc.export_meeting(m1, "markdown", section_keys=["entscheidungen"])
    assert "Entscheidungen" in out["content"]
    assert "Budget freigegeben" in out["content"]
    # selected only -> other sections omitted
    assert "Offene Fragen" not in out["content"]


def test_export_unknown_format(make_service):
    svc, (m1, _) = _finalize_two(make_service)
    try:
        svc.export_meeting(m1, "pdfx")
        assert False, "expected ValueError"
    except ValueError:
        pass
