"""Phase 7a: markers, auditable segment edits (revisions), tags, auto title/tags."""
from __future__ import annotations

import json

from core.store.db import session_scope
from core.store.models import Analysis, Meeting, TranscriptSegment


def _finalize(make_service):
    svc = make_service()
    mid = svc.start_meeting(title="Neues Meeting", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    with session_scope() as s:
        for seg in s.query(TranscriptSegment).filter_by(meeting_id=mid):
            s.delete(seg)
        s.commit()
    with session_scope() as s:
        s.add(TranscriptSegment(id="seg0", meeting_id=mid, start_s=0.0, end_s=2.0,
                                text="Wir besprechen das Q2 Budget.", raw_text="x",
                                speaker_id="Sprecher 1", status="bestaetigt"))
        s.add(TranscriptSegment(id="seg1", meeting_id=mid, start_s=2.0, end_s=4.0,
                                text="Risiken im Budget nennen.", raw_text="x",
                                speaker_id="Sprecher 1", status="bestaetigt"))
        s.commit()
    return svc, mid


def test_markers(make_service):
    svc, mid = _finalize(make_service)
    m1 = svc.add_marker(mid, 1.5, "Budget-Anfrage")
    svc.add_marker(mid, 3.0, "Risiko")
    marks = svc.list_markers(mid)
    assert len(marks) == 2
    assert marks[0]["text"] == "Budget-Anfrage" and marks[0]["at_s"] == 1.5
    svc.delete_marker(m1["id"])
    assert len(svc.list_markers(mid)) == 1


def test_edit_creates_revision(make_service):
    svc, mid = _finalize(make_service)
    out = svc.edit_segment(mid, "seg0", "Wir besprechen das Q2-Budget.",
                           speaker_id="Ben")
    assert out["text"].startswith("Wir besprechen das Q2-Budget")
    revs = svc.list_revisions(mid)
    # text + speaker changes -> 2 revisions
    assert len(revs) == 2
    fields = {r["field"] for r in revs}
    assert fields == {"text", "speaker_id"}
    text_rev = next(r for r in revs if r["field"] == "text")
    assert text_rev["original"] == "Wir besprechen das Q2 Budget."
    assert text_rev["updated"] == "Wir besprechen das Q2-Budget."
    with session_scope() as s:
        seg = s.get(TranscriptSegment, "seg0")
        assert seg.speaker_id == "Ben"


def test_edit_noop_no_revision(make_service):
    svc, mid = _finalize(make_service)
    svc.edit_segment(mid, "seg0", "Wir besprechen das Q2 Budget.")  # unchanged
    assert svc.list_revisions(mid) == []


def test_tags(make_service):
    svc, mid = _finalize(make_service)
    tags = svc.add_tag(mid, "budget")
    assert tags == ["budget"]
    tags = svc.add_tag(mid, "Budget")  # case-sensitive distinct
    assert sorted(tags) == ["Budget", "budget"]
    assert svc.add_tag(mid, "budget") == ["Budget", "budget"]  # idempotent
    tags = svc.remove_tag(mid, "budget")
    assert tags == ["Budget"]
    try:
        svc.add_tag(mid, "  ")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_auto_title_tags(make_service, config):
    svc, mid = _finalize(make_service)
    analysis = json.dumps({
        "kurzfassung": [{"text": "Q2-Budget wird besprochen und Risiken werden genannt.",
                         "quellen": []}],
        "themen": [{"text": "Q2 Budget planen", "quellen": []}],
        "entscheidungen": [{"text": "Budget freigegeben", "quellen": []}],
        "risiken": [{"text": "Budget ueberschreitung moeglich", "quellen": []}],
        "aufgaben": [], "offene_fragen": [],
    }, ensure_ascii=False)
    with session_scope() as s:
        s.add(Analysis(meeting_id=mid, kind="summary", model="mock",
                       content=analysis))
        s.commit()

    out = svc.apply_auto_title_tags(mid)
    assert out["applied"] is True and out["title_changed"] is True
    assert out["title"].startswith("Q2-Budget")
    # "budget" should surface as a tag (content word, lowercased)
    assert "budget" in out["tags"]
    # M1: "planen" occurs only in the themen section, so it proves the
    # themen section is actually read (the old code read a nonexistent
    # "agenda" key and silently dropped it).
    assert "planen" in out["tags"]
    # Idempotent: a second run adds nothing new.
    again = svc.apply_auto_title_tags(mid)
    assert again["tags_added"] == 0

    # A manual title is protected from auto overwrite.
    with session_scope() as s:
        m = s.get(Meeting, mid)
        m.title = "Hand edited"
        m.title_status = "manual"
        s.commit()
    out2 = svc.apply_auto_title_tags(mid)
    assert out2["title"] == "Hand edited" and out2["title_changed"] is False
