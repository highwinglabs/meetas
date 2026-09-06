"""Hybrid search: FTS5 (lexical) + vector (near-lexical) fused via RRF (Phase 5).

Reciprocal Rank Fusion is robust to the two sub-scores having different scales
(bm25 vs cosine): each result is scored by the sum of ``1 / (k + rank)`` over the
rankings it appears in. When embeddings are disabled (or no index exists yet) we
fall back to plain FTS5 so search always works.
"""
from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from datetime import timezone
from zoneinfo import ZoneInfo

from core.config import Config
from core.search.embeddings import EmbeddingEngine
from core.search.vectorstore import vector_search
from core.services._common import UnknownMeetingError
from core.store import fts
from core.store.models import Meeting, Project, TranscriptSegment

RRF_K = 60


def _rrf(ranked_lists: list[list[str]], limit: int) -> tuple[list[str], list[float]]:
    """Fuse several ranked id-lists. Returns (ids, scores) sorted by score."""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, sid in enumerate(lst, start=1):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (RRF_K + rank)
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [sid for sid, _ in ranked[:limit]], [round(sc, 6) for _, sc in ranked[:limit]]


def hybrid_search(session: Session, query: str, engine: EmbeddingEngine,
                  config: Config, limit: int = 25,
                  meeting_id: str | None = None,
                  project_id: str | None = None,
                  meeting_ids: list[str] | None = None) -> list[dict]:
    """Return ranked, enriched results for `query`.

    Each result carries a traceable source (segment_id + meeting_id + time range),
    the fused RRF score, and the individual FTS / vector ranks.
    """
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 25
    if meeting_id is not None:
        meeting = session.get(Meeting, meeting_id)
        if meeting is None or meeting.deleted_at is not None:
            raise KeyError(meeting_id)

    allowed_meetings: set[str] | None = None
    # The REST client uses an empty list to mean "no meeting filter"; a
    # non-empty list is the explicit selection.
    if meeting_ids:
        requested = set(meeting_ids)
        existing = set(session.scalars(select(Meeting.id).where(
            Meeting.id.in_(requested), Meeting.deleted_at.is_(None))).all())
        if not existing:
            # L11: none of the requested meetings exist -> raise the specific
            # unknown-meeting error instead of a bare KeyError / empty result.
            raise UnknownMeetingError(next(iter(meeting_ids)))
        # Filter out unknown/deleted ids so one bad id does not reject the
        # whole query; the search proceeds over the ids that do exist.
        allowed_meetings = existing
    if project_id is not None:
        project = session.get(Project, project_id)
        if project is None or project.deleted_at is not None:
            raise KeyError(project_id)
        project_meetings = set(session.scalars(
            select(Meeting.id).where(Meeting.project_id == project_id,
                                     Meeting.deleted_at.is_(None))
        ).all())
        allowed_meetings = project_meetings if allowed_meetings is None else allowed_meetings & project_meetings
    if meeting_id is not None:
        if allowed_meetings is not None and meeting_id not in allowed_meetings:
            return []
        allowed_meetings = {meeting_id}

    # Both sub-searches honour the meeting scope so a scoped search never
    # leaks hits from other meetings (FTS and vector alike).
    fts_hits = fts.search(session, query, limit=limit, meeting_id=meeting_id,
                          meeting_ids=allowed_meetings if meeting_id is None else None)
    fts_ids = [h["segment_id"] for h in fts_hits]
    fts_by_id = {h["segment_id"]: h for h in fts_hits}

    # Embeddings are an optional acceleration layer. Disabling them must not
    # leave stale vectors active; FTS remains the documented fallback.
    vec_hits = [] if not config.embeddings_enabled else vector_search(
        session, query, engine, limit=limit, meeting_id=meeting_id,
        meeting_ids=allowed_meetings if meeting_id is None else None)
    vec_ids = [h["seg_id"] for h in vec_hits]
    vec_by_id = {h["seg_id"]: h for h in vec_hits}

    results: list[dict]
    if not vec_ids:
        # No vector index yet -> pure FTS, already enriched by fts.search.
        results = [{**h, "rrf": None, "fts_rank": i + 1, "vec_rank": None,
                    "vec_score": None, "mode": "fts"}
                   for i, h in enumerate(fts_hits)]
        return _attach_meeting_date(session, results)

    fused_ids, rrf_scores = _rrf([fts_ids, vec_ids], limit=limit)

    # Enrich in one pass: fetch segment + meeting title for all fused ids.
    seg_rows = session.execute(
        select(TranscriptSegment).where(TranscriptSegment.id.in_(fused_ids))
    ).scalars().all()
    seg_by_id = {s.id: s for s in seg_rows}
    meeting_ids = {s.meeting_id for s in seg_rows}
    title_map = {}
    if meeting_ids:
        t_rows = session.execute(
            select(Meeting.id, Meeting.title).where(Meeting.id.in_(meeting_ids))
        ).all()
        title_map = {r.id: r.title for r in t_rows}

    fts_rank = {sid: i + 1 for i, sid in enumerate(fts_ids)}
    vec_rank = {sid: i + 1 for i, sid in enumerate(vec_ids)}

    results = []
    for sid, score in zip(fused_ids, rrf_scores):
        seg = seg_by_id.get(sid)
        fh = fts_by_id.get(sid)
        vh = vec_by_id.get(sid)
        results.append({
            "segment_id": sid,
            "meeting_id": seg.meeting_id if seg else None,
            "start_s": seg.start_s if seg else None,
            "end_s": seg.end_s if seg else None,
            "text": seg.text if seg else None,
            "speaker_id": seg.speaker_id if seg else None,
            "meeting_title": title_map.get(seg.meeting_id) if seg else None,
            "snippet": (fh["snippet"] if fh and fh.get("snippet") else
                        (seg.text[:120] if seg else "")),
            "rrf": score,
            "fts_rank": fts_rank.get(sid),
            "vec_rank": vec_rank.get(sid),
            "vec_score": round(vh["score"], 4) if vh else None,
            "mode": "hybrid",
        })
    return _attach_meeting_date(session, results)


def _attach_meeting_date(session: Session, results: list[dict]) -> list[dict]:
    """Add a human-usable `meeting_date` (ISO date) to each hit so sources can
    carry Meeting/Datum/Sprecher/Zeitstempel. Best-effort: missing meetings get
    `None`."""
    meeting_ids = {r.get("meeting_id") for r in results if r.get("meeting_id")}
    date_map: dict[str, str] = {}
    if meeting_ids:
        rows = session.execute(
            select(Meeting.id, Meeting.start_at).where(
                Meeting.id.in_(meeting_ids))
        ).all()
        date_map = {r.id: (
                        (r.start_at if r.start_at.tzinfo is not None else
                         r.start_at.replace(tzinfo=timezone.utc))
                        .astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
                        if r.start_at else None)
                    for r in rows}
    for r in results:
        r["meeting_date"] = date_map.get(r.get("meeting_id"))
    return results
