"""Phase-8 regression tests for RAG source validation (P1) and the meeting
scope filter (P2).

P1 contract:
- An answer is ``grounded`` only when it cites >=1 segment id that exists in the
  retrieved hits. No valid citation  ->  not grounded + explicit no-evidence
  message, and ``sources`` is empty.
- Every returned source is a real retrieved segment carrying meeting, date,
  speaker, timestamp and a jump target.

P2 contract:
- A scoped search (``meeting_id=...``) must never return hits from other
  meetings, in BOTH the FTS branch and the vector/hybrid branch.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from core.llm import MockLLM
from core.providers.base import ASRSegment
from core.search import hybrid as hybrid_mod
from core.search import rag as rag_mod
from core.store import fts
from core.store.db import session_scope
from core.store.models import Meeting, TranscriptSegment

from tests.conftest import MockASREngine


def _seg(start: float, end: float, text: str) -> ASRSegment:
    return ASRSegment(start_s=start, end_s=end, text=text, language="de",
                      confidence=0.9)


def _build_meeting(make_service, segments, title, llm_text=None,
                   start_at=None, embed: bool = True, speaker_id: str | None = None):
    """Create, capture, transcribe (and optionally embed) a meeting.

    ``speaker_id`` (optional) labels every segment with that speaker (stands in
    for diarisation, which needs a real model). Returns
    ``(service, meeting_id, [segment_ids in order])``."""
    asr = MockASREngine(segments=segments)
    llm = MockLLM(text=llm_text)
    svc = make_service(asr_engine=asr, llm_engine=llm)
    mid = svc.start_meeting(title=title, source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    svc.transcribe(mid)
    if start_at is not None or speaker_id is not None:
        with session_scope() as s:
            if start_at is not None:
                s.get(Meeting, mid).start_at = start_at
            if speaker_id is not None:
                for seg in s.execute(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.meeting_id == mid)).scalars():
                    seg.speaker_id = speaker_id
            s.commit()
    if embed:
        svc.embed_meeting(mid)
    with session_scope() as s:
        seg_ids = [r.id for r in s.execute(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == mid)
            .order_by(TranscriptSegment.start_s)).scalars().all()]
    return svc, mid, seg_ids


# --------------------------------------------------------------------------- #
# P1: RAG source validation
# --------------------------------------------------------------------------- #

def test_valid_citation_is_grounded_and_enriched(make_service):
    svc, mid, seg_ids = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Wir planen das Alpha Projekt fuer Q2."),
         _seg(2.5, 5.0, "Der Budgetrahmen bleibt unveraendert.")],
        title="Alpha",
        start_at=datetime(2026, 4, 2, 9, 0),
        speaker_id="Sprecher 1",
    )
    svc._llm_override = MockLLM(
        text="Ja, das Alpha-Projekt ist geplant. [seg:%s]" % seg_ids[0])
    out = svc.ask("Alpha Projekt Q2", meeting_id=mid)

    assert out["grounded"] is True
    assert "[seg:%s]" % seg_ids[0] in out["answer"]
    assert out["citations"] == [seg_ids[0]]

    src = out["sources"][0]
    assert src["segment_id"] == seg_ids[0]
    assert src["meeting_id"] == mid
    assert src["meeting_title"] == "Alpha"
    assert src["meeting_date"] == "2026-04-02"          # Datum present
    assert src["timestamp"] == "00:00"                  # mm:ss of start_s
    assert src["start_s"] == 0.0 and src["end_s"] == 2.0
    assert src["speaker_id"] == "Sprecher 1"           # Sprecher present
    assert src["jump_target"] == f"/meetings/{mid}#seg-{seg_ids[0]}"
    assert "Alpha Projekt" in src["snippet"]


def test_every_source_is_a_real_retrieved_segment(make_service):
    svc, mid, seg_ids = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Alpha Projekt Plan."),
         _seg(2.5, 4.5, "Alpha Projekt Budget."),
         _seg(5.0, 7.0, "Alpha Projekt Termin.")],
        title="Alpha",
    )
    svc._llm_override = MockLLM(
        text="Plan [seg:%s], Budget [seg:%s]." % (seg_ids[0], seg_ids[1]))
    out = svc.ask("Alpha Projekt", meeting_id=mid)
    assert out["grounded"] is True
    # sources are a subset of the real hit ids (no invented ids).
    assert all(s["segment_id"] in set(seg_ids) for s in out["sources"])
    # cited sources come first, in citation order.
    assert [s["segment_id"] for s in out["sources"]][:2] == [seg_ids[0], seg_ids[1]]


def test_answer_without_citation_is_not_grounded(make_service):
    svc, mid, _ = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Alpha Projekt Plan.")],
        title="Alpha",
        llm_text="Ja, das ist sicher so.",   # fluent but cites nothing
    )
    out = svc.ask("Alpha Projekt?", meeting_id=mid)
    assert out["grounded"] is False
    assert out["answer"] == rag_mod._NO_EVIDENCE
    assert out["sources"] == []
    assert out["citations"] == []


def test_citation_of_missing_segment_is_not_grounded(make_service):
    svc, mid, seg_ids = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Alpha Projekt Plan.")],
        title="Alpha",
        llm_text="Genau. [seg:INVENTEDID000000000000]",
    )
    out = svc.ask("Alpha Projekt?", meeting_id=mid)
    assert out["grounded"] is False
    assert out["answer"] == rag_mod._NO_EVIDENCE
    assert out["sources"] == []
    assert "INVENTEDID000000000000" in out.get("invalid_citations", [])


def test_model_sentinel_is_not_grounded(make_service):
    svc, mid, _ = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Alpha Projekt Plan.")],
        title="Alpha",
        llm_text="Nicht im Transkript beantwortet.",
    )
    out = svc.ask("Vollstaendige Details?", meeting_id=mid)
    assert out["grounded"] is False
    assert out["answer"] == rag_mod._NO_EVIDENCE
    assert out["sources"] == []


def test_no_hits_returns_no_evidence(make_service):
    svc, mid, _ = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Alpha Projekt Plan.")],
        title="Alpha",
    )
    # A nonsense query with no lexical/semantic overlap -> no hits.
    out = svc.ask("zzzqqqxxx ungueltiger token", meeting_id=mid)
    assert out["grounded"] is False
    assert out["hits"] == 0
    assert "Stelle im Transkript" in out["answer"] or "ausreichende Information" in out["answer"]


def test_rag_disabled_is_reported(make_service, config):
    config.rag_enabled = False
    svc, mid, _ = _build_meeting(
        make_service, [_seg(0.0, 2.0, "Alpha Projekt Plan.")], title="Alpha")
    out = svc.ask("Alpha Projekt?", meeting_id=mid)
    assert out.get("answer") is None
    assert "deaktiviert" in out.get("error", "")


# --------------------------------------------------------------------------- #
# P2: meeting scope filter (FTS + vector)
# --------------------------------------------------------------------------- #

def test_hybrid_scoped_excludes_other_meeting(make_service):
    # Both meetings are embedded -> exercises the vector/hybrid branch.
    _svc_a, mid_a, seg_a = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Alpha Alpha Alpha Projekt"),
         _seg(2.5, 4.5, "Alpha Alpha Alpha Plan")],
        title="A")
    _svc_b, mid_b, _ = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Beta Projekt und Budget"),
         _seg(2.5, 4.5, "Beta Ziel und Plan")],
        title="B")

    # 'Alpha' exists only in A. Unscoped it retrieves A's segments; the SAME
    # query scoped to B must never leak any A segment (empty is the correct,
    # leak-free outcome because B does not discuss 'Alpha').
    unscoped = _svc_a.search_hybrid("Alpha")
    assert any(h["segment_id"] in set(seg_a) for h in unscoped), \
        "precondition: unscoped search should retrieve A's 'Alpha' segments"

    scoped_b = _svc_b.search_hybrid("Alpha", meeting_id=mid_b)
    assert all(h["meeting_id"] == mid_b for h in scoped_b)
    assert not (set(h["segment_id"] for h in scoped_b) & set(seg_a)), \
        "scoped search must not leak segments from another meeting"


def test_fts_branch_scoped_excludes_other_meeting(make_service):
    # B is NOT embedded -> a scoped search on B has no vector hits and must
    # fall back to the FTS-only branch. 'Alpha' only exists in A, so FTS
    # scoped to B finds nothing (no leak), but B's own word still resolves.
    _svc_a, mid_a, _ = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Alpha Alpha Alpha Projekt"),
         _seg(2.5, 4.5, "Alpha Alpha Alpha Plan")],
        title="A")
    svc_b, mid_b, _ = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Beta Projekt und Budget"),
         _seg(2.5, 4.5, "Beta Ziel und Plan")],
        title="B", embed=False)

    # Scoped to B for B's own word -> only B segments (FTS branch).
    hits = svc_b.search_hybrid("Budget", meeting_id=mid_b)
    assert hits
    assert all(h["meeting_id"] == mid_b for h in hits)
    assert all(h.get("mode") == "fts" for h in hits)

    # Scoped to B for A-only word -> no A leak.
    hits_alpha = svc_b.search_hybrid("Alpha", meeting_id=mid_b)
    assert all(h["meeting_id"] == mid_b for h in hits_alpha)


def test_fts_direct_meeting_filter(make_service):
    _svc_a, mid_a, seg_a = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Gemeinsam Projekt Plan"),
         _seg(2.5, 4.5, "Gemeinsam Projekt Budget")],
        title="A")
    _svc_b, mid_b, seg_b = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Gemeinsam Projekt Ziel"),
         _seg(2.5, 4.5, "Gemeinsam Projekt Termin")],
        title="B")

    with session_scope() as s:
        fts.ensure_fts(s)
        all_hits = fts.search(s, "Gemeinsam Projekt")
        scoped_b = fts.search(s, "Gemeinsam Projekt", meeting_id=mid_b)

    assert {h["meeting_id"] for h in all_hits} == {mid_a, mid_b}
    assert scoped_b, "scoped FTS should return B's matching segments"
    assert all(h["meeting_id"] == mid_b for h in scoped_b)
    assert set(h["segment_id"] for h in scoped_b) <= set(seg_b)
    assert not (set(h["segment_id"] for h in scoped_b) & set(seg_a))


def test_rag_scoped_sources_only_from_scoped_meeting(make_service):
    # A and B both talk about 'Projekt'. Scoped RAG to B must ground only on
    # B segments; a citation to an A segment is invalid and drops grounding.
    _svc_a, mid_a, seg_a = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Projekt Alpha Strategie"),
         _seg(2.5, 4.5, "Projekt Alpha Roadmap")],
        title="A")
    svc_b, mid_b, seg_b = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Projekt Beta Strategie"),
         _seg(2.5, 4.5, "Projekt Beta Roadmap")],
        title="B")

    # Valid B citation -> grounded, all sources from B. (The LLM is pointed at
    # a real B segment; _llm_engine() reads _llm_override at call time.)
    svc_b._llm_override = MockLLM(text="Genau. [seg:%s]" % seg_b[0])
    out = svc_b.ask("Projekt Strategie?", meeting_id=mid_b)
    assert out["grounded"] is True
    assert all(s["meeting_id"] == mid_b for s in out["sources"])
    assert all(s["segment_id"] in set(seg_b) for s in out["sources"])

    # Re-run with an LLM that cites an A-only segment id -> NOT grounded,
    # proving a cross-meeting citation can never pass as a valid source.
    svc2, mid_b2, seg_b2 = _build_meeting(
        make_service,
        [_seg(0.0, 2.0, "Projekt Beta Strategie")],
        title="B2",
        llm_text="So. [seg:%s]" % seg_a[0],   # cite A's id from within B2 scope
    )
    # The query still resolves B2's own segment so hits is non-empty; the cited
    # A id is not among the scoped hits, so grounding must fail.
    out2 = svc2.ask("Projekt Strategie?", meeting_id=mid_b2)
    assert out2["grounded"] is False
    assert out2["answer"] == rag_mod._NO_EVIDENCE
    assert out2["sources"] == []
    assert seg_a[0] in out2.get("invalid_citations", [])
