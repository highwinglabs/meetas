"""FTS5 full-text search over transcript segments.

A standalone (content-filled) FTS5 table mirrors the searchable columns of
`transcript_segment`. It is kept in sync by `reindex_meeting` / `reindex_all`,
which are transactional and idempotent (delete-then-reinsert per meeting).
This keeps search robust without relying on FTS external-content triggers.
"""
from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.logging_setup import get_logger

log = get_logger("ma.fts")

FTS_TABLE = "transcript_fts"

_FTS_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
    f"meeting_id UNINDEXED, seg_id UNINDEXED, speaker_id UNINDEXED, text, "
    f"tokenize='unicode61')"
)


def fts_available(session: Session) -> bool:
    """Probe whether the SQLite runtime supports FTS5."""
    try:
        # Keep the probe isolated.  Rolling back the whole SQLAlchemy session
        # on an unsupported SQLite build would silently discard pending
        # transcript edits made by the caller before reindexing.
        with session.begin_nested():
            session.execute(text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS ma_fts_probe USING fts5(x)"))
            session.execute(text("DROP TABLE IF EXISTS ma_fts_probe"))
        return True
    except Exception:
        return False


def ensure_fts(session: Session) -> None:
    if not fts_available(session):
        log.warning("FTS5 not available in this SQLite build; search falls back to LIKE")
        return
    session.execute(text(_FTS_DDL))


def reindex_meeting(session: Session, meeting_id: str) -> int:
    """Rebuild the FTS rows for one meeting. Returns the number of rows."""
    ensure_fts(session)
    if not fts_available(session):
        return 0
    session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE meeting_id = :m"),
                    {"m": meeting_id})
    res = session.execute(text(
        f"INSERT INTO {FTS_TABLE} (meeting_id, seg_id, speaker_id, text) "
        f"SELECT meeting_id, id, COALESCE(speaker_id,''), COALESCE(text,'') "
        f"FROM transcript_segment WHERE meeting_id = :m"), {"m": meeting_id})
    return res.rowcount or 0


def remove_meeting_fts(session: Session, meeting_id: str) -> int:
    """Drop all FTS rows for one meeting (used on permanent deletion so a
    removed meeting can never surface in search results again)."""
    ensure_fts(session)
    if not fts_available(session):
        return 0
    res = session.execute(
        text(f"DELETE FROM {FTS_TABLE} WHERE meeting_id = :m"), {"m": meeting_id})
    return res.rowcount or 0


def reindex_all(session: Session) -> int:
    """Rebuild the whole FTS index from transcript_segment (crash safety)."""
    ensure_fts(session)
    if not fts_available(session):
        return 0
    session.execute(text(f"DELETE FROM {FTS_TABLE}"))
    res = session.execute(text(
        f"INSERT INTO {FTS_TABLE} (meeting_id, seg_id, speaker_id, text) "
        f"SELECT meeting_id, id, COALESCE(speaker_id,''), COALESCE(text,'') "
        f"FROM transcript_segment"))
    return res.rowcount or 0


def _build_match(query: str) -> str | None:
    tokens = [t for t in re.split(r"\s+", query.strip()) if t]
    if not tokens:
        return None
    # Each token becomes a quoted phrase; tokens are OR-combined and ranked by bm25.
    parts = ['"' + t.replace('"', '""') + '"' for t in tokens]
    return " OR ".join(parts)


def _like_search(session: Session, query: str, limit: int,
                 meeting_id: str | None = None,
                 meeting_ids: set[str] | None = None) -> list[dict]:
    """LIKE-based fallback when FTS5 is unavailable."""
    like = "%" + query.strip() + "%"
    where = "s.text LIKE :q"
    params: dict = {"q": like, "l": limit}
    if meeting_id is not None:
        where += " AND s.meeting_id = :m"
        params["m"] = meeting_id
    elif meeting_ids is not None:
        if not meeting_ids:
            return []
        names = []
        for i, value in enumerate(sorted(meeting_ids)):
            name = f"m{i}"
            names.append(f":{name}")
            params[name] = value
        where += f" AND s.meeting_id IN ({', '.join(names)})"
    rows = session.execute(text(
        "SELECT s.id, s.meeting_id, s.start_s, s.end_s, s.text, s.speaker_id, "
        "m.title AS meeting_title "
            f"FROM transcript_segment s JOIN meeting m ON m.id = s.meeting_id "
            f"WHERE m.deleted_at IS NULL AND {where} "
            f"ORDER BY m.start_at DESC, s.start_s, s.id LIMIT :l"), params).fetchall()
    return [{"segment_id": r.id, "meeting_id": r.meeting_id,
             "start_s": r.start_s, "end_s": r.end_s, "text": r.text,
             "speaker_id": r.speaker_id, "meeting_title": r.meeting_title,
             "snippet": r.text, "rank": None} for r in rows]


def search(session: Session, query: str, limit: int = 25,
           meeting_id: str | None = None,
           meeting_ids: set[str] | None = None) -> list[dict]:
    """Search transcript segments. Returns ranked results with a snippet and
    a structured source reference (segment_id + meeting_id + time range).

    When `meeting_id` is given the search is **restricted to that meeting**:
    hits from other meetings are never returned (applies to the FTS5 path and
    the LIKE fallback alike)."""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 25
    match = _build_match(query)
    if match is None:
        return []
    if not fts_available(session):
        return _like_search(session, query, limit, meeting_id=meeting_id,
                            meeting_ids=meeting_ids)
    where = f"{FTS_TABLE} MATCH :q"
    params: dict = {"q": match, "l": limit}
    if meeting_id is not None:
        where += " AND f.meeting_id = :m"
        params["m"] = meeting_id
    elif meeting_ids is not None:
        if not meeting_ids:
            return []
        names = []
        for i, value in enumerate(sorted(meeting_ids)):
            name = f"m{i}"
            names.append(f":{name}")
            params[name] = value
        where += f" AND f.meeting_id IN ({', '.join(names)})"
    try:
        rows = session.execute(text(
            f"SELECT f.seg_id AS seg_id, f.meeting_id AS meeting_id, "
            f"s.start_s AS start_s, s.end_s AS end_s, s.speaker_id AS speaker_id, "
            f"s.text AS text, m.title AS meeting_title, "
            f"snippet({FTS_TABLE}, 3, '«', '»', ' … ', 16) AS snip, "
            f"bm25({FTS_TABLE}) AS rank "
            f"FROM {FTS_TABLE} f JOIN meeting m ON m.id = f.meeting_id "
            f"JOIN transcript_segment s ON s.id = f.seg_id AND s.meeting_id = f.meeting_id "
            f"WHERE m.deleted_at IS NULL AND {where} "
            f"ORDER BY rank, f.seg_id LIMIT :l"), params).fetchall()
    except Exception:
        # malformed FTS query (e.g. operator-like chars) -> degrade to LIKE
        return _like_search(session, query, limit, meeting_id=meeting_id,
                            meeting_ids=meeting_ids)
    results = []
    for r in rows:
        results.append({
            "segment_id": r.seg_id,
            "meeting_id": r.meeting_id,
            "start_s": r.start_s,
            "end_s": r.end_s,
            "text": r.text,
            "speaker_id": r.speaker_id,
            "meeting_title": r.meeting_title,
            "snippet": r.snip,
            "rank": round(r.rank, 4),
        })
    return results
