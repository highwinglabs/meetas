"""FTS5 full-text search over transcript segments (with source references)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from core.store.db import get_engine, make_engine, session_scope
from core.store.fts import (
    ensure_fts, fts_available, fts_needs_rebuild, reindex_all,
    reindex_meeting, search,
)
from core.store.models import Base, Meeting, TranscriptSegment


@pytest.fixture(autouse=True)
def _db(config):
    """These tests hit the DB directly; init the schema for the isolated config."""
    make_engine(config)
    Base.metadata.create_all(get_engine())
    yield


def _add_segments(meeting_id: str, rows) -> None:
    with session_scope() as s:
        for i, (start, end, text, speaker) in enumerate(rows):
            s.add(TranscriptSegment(
                meeting_id=meeting_id, start_s=start, end_s=end, text=text,
                raw_text=text, speaker_id=speaker, status="vorlaeufig"))
        s.commit()


def _meeting(title: str) -> str:
    with session_scope() as s:
        m = Meeting(title=title, status="done")
        s.add(m)
        s.commit()
        return m.id


def test_fts_available(config):
    with session_scope() as s:
        ensure_fts(s)
        assert fts_available(s) is True


def test_fts_probe_runs_once_per_process(config, monkeypatch):
    # Regression (L1): the FTS5 DDL probe is a property of the SQLite build,
    # so it must run at most once per process; later availability checks reuse
    # the memoised result instead of re-running CREATE/DROP on every call.
    import core.store.fts as fts
    monkeypatch.setattr(fts, "_fts_probe_result", None)
    calls = {"n": 0}
    original = fts._probe_fts5

    def counting(session):
        calls["n"] += 1
        return original(session)

    monkeypatch.setattr(fts, "_probe_fts5", counting)
    with session_scope() as s:
        assert fts.fts_available(s) is True
        assert fts.fts_available(s) is True
        assert fts.fts_available(s) is True
    assert calls["n"] == 1


def test_fts_needs_rebuild_tracks_row_count_drift(config):
    # Regression (L2): startup rebuilds the FTS index only when its row count
    # has drifted from transcript_segment (a crash signature); a current index
    # is left alone instead of being rebuilt on every startup.
    mid = _meeting("M")
    _add_segments(mid, [(0.0, 1.0, "Erstes Segment", "Sprecher 1")])
    with session_scope() as s:
        ensure_fts(s)
        # Fresh, unpopulated index -> must rebuild.
        assert fts_needs_rebuild(s) is True
        n = reindex_all(s)
        assert n == 1
        # Index now matches the single segment -> no rebuild needed.
        assert fts_needs_rebuild(s) is False
    # Drift: a new segment without a reindex -> rebuild required again.
    _add_segments(mid, [(2.0, 3.0, "Zweites Segment", "Sprecher 2")])
    with session_scope() as s:
        assert fts_needs_rebuild(s) is True


def test_search_finds_phrase_with_snippet(config):
    mid = _meeting("M")
    _add_segments(mid, [
        (0.0, 1.0, "Willkommen zur wöchentlichen Besprechung", "Sprecher 1"),
        (2.0, 4.0, "Das Q2-Budget muss noch genehmigt werden", "Sprecher 2"),
    ])
    with session_scope() as s:
        ensure_fts(s)
        reindex_meeting(s, mid)
        results = search(s, "Budget")

    assert len(results) == 1
    assert results[0]["segment_id"]
    assert results[0]["meeting_id"] == mid
    assert "Budget" in results[0]["snippet"]
    assert results[0]["start_s"] == 2.0
    assert results[0]["speaker_id"] == "Sprecher 2"


def test_search_or_combines_tokens(config):
    mid = _meeting("M")
    _add_segments(mid, [
        (0.0, 1.0, "Deadline ist Freitag", "S1"),
        (2.0, 3.0, "Budget reicht nicht", "S2"),
    ])
    with session_scope() as s:
        ensure_fts(s)
        reindex_meeting(s, mid)
        results = search(s, "Deadline Budget")
    assert len(results) == 2


def test_search_no_match_and_empty(config):
    mid = _meeting("M")
    _add_segments(mid, [(0.0, 1.0, "Hallo Welt", "S1")])
    with session_scope() as s:
        ensure_fts(s)
        reindex_meeting(s, mid)
        assert search(s, "komplettnichtda") == []
        assert search(s, "   ") == []


def test_reindex_is_idempotent(config):
    mid = _meeting("M")
    _add_segments(mid, [(0.0, 1.0, "einmalige Zeile", "S1")])
    with session_scope() as s:
        ensure_fts(s)
        reindex_meeting(s, mid)
        reindex_meeting(s, mid)  # no duplication
        assert len(search(s, "einmalige")) == 1


def test_reindex_all_across_meetings(config):
    mid1 = _meeting("A")
    mid2 = _meeting("B")
    _add_segments(mid1, [(0.0, 1.0, "alpha keyword", "S1")])
    _add_segments(mid2, [(0.0, 1.0, "beta keyword", "S1")])
    with session_scope() as s:
        ensure_fts(s)
        reindex_all(s)
    results = search(s, "keyword")
    assert {r["meeting_id"] for r in results} == {mid1, mid2}


def test_reindex_participates_in_caller_transaction(config):
    mid = _meeting("atomic")
    try:
        with session_scope() as s:
            s.add(TranscriptSegment(meeting_id=mid, start_s=0, end_s=1,
                                    text="rollback me", raw_text="rollback me",
                                    status="vorlaeufig"))
            reindex_meeting(s, mid)
            raise RuntimeError("simulate caller failure")
    except RuntimeError:
        pass
    with session_scope() as s:
        assert search(s, "rollback") == []
