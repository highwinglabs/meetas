"""Central task overview (Phase 6).

Tasks are extracted from the stored analysis ``aufgaben`` section. Extraction is
**idempotent**: each task carries a stable ``dedup_key`` (hash of meeting + text),
so re-running an analysis never duplicates tasks. Owners and deadlines come from
the (validated) analysis -- never invented. The ``S<n>`` source citations in the
analysis are mapped back to real segment ids via playback order.
"""
from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import func, select

from core.analysis.schema import extract_json
from core.logging_setup import get_logger
from core.store.models import Analysis, Meeting, Task, TranscriptSegment

log = get_logger("ma.tasks")

_S_RE = re.compile(r"^S(\d+)$")

# German status vocabulary for the task board.
STATUSES = ("offen", "laeuft", "erledigt")


def _dedup_key(meeting_id: str, text: str) -> str:
    norm = " ".join((text or "").split()).strip().lower()
    return hashlib.sha256(f"{meeting_id}|{norm}".encode("utf-8")).hexdigest()[:40]


def _map_sid_to_segment(rows: list, sid: str) -> dict | None:
    """Map an analysis ``S<n>`` citation to the matching playback-order row."""
    m = _S_RE.match((sid or "").strip())
    if not m:
        return None
    idx = int(m.group(1)) - 1
    if idx < 0 or idx >= len(rows):
        return None
    return rows[idx]


def extract_tasks(session, meeting_id: str) -> dict:
    """Create tasks from the meeting's stored ``summary`` analysis.

    Returns ``{"created": n, "total": m}``. No-op when no analysis or no tasks.
    """
    analysis = session.scalar(
        select(Analysis).where(Analysis.meeting_id == meeting_id,
                               Analysis.kind == "summary")
    )
    if analysis is None or not analysis.content:
        return {"created": 0, "total": 0}
    meeting = session.get(Meeting, meeting_id)
    if meeting is None or meeting.deleted_at is not None:
        return {"created": 0, "total": 0}
    project_id = meeting.project_id
    data = extract_json(analysis.content)
    if not isinstance(data, dict):
        return {"created": 0, "total": 0}

    # Playback-order segments so S<n> maps to a real segment id.
    segs = session.scalars(
        select(TranscriptSegment).where(TranscriptSegment.meeting_id == meeting_id)
        .order_by(TranscriptSegment.start_s)
    ).all()
    rows = [
        {"id": sg.id, "speaker": sg.speaker_id, "start_s": sg.start_s}
        for sg in segs
    ]

    aufgaben = data.get("aufgaben") or []
    created = 0
    for order, item in enumerate(aufgaben):
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        responsible = (item.get("verantwortlich") or "").strip() or "nicht angegeben"
        due_date = (item.get("deadline") or "").strip() or "nicht angegeben"
        if due_date in {"n/a", "-"}:
            due_date = "nicht angegeben"

        # First valid S<n> citation -> real source segment (best effort).
        source_segment = None
        for q in (item.get("quellen") or []):
            if isinstance(q, dict):
                row = _map_sid_to_segment(rows, q.get("segment_id", ""))
                if row is not None:
                    source_segment = row["id"]
                    break

        key = _dedup_key(meeting_id, text)
        existing = session.scalar(select(Task).where(Task.dedup_key == key))
        if existing is None:
            session.add(Task(
                meeting_id=meeting_id, text=text, responsible=responsible,
                due_date=due_date, status="offen",
                project_id=project_id,
                source_segment_id=source_segment,
                source_analysis_id=analysis.id, dedup_key=key, sort_order=order,
            ))
            created += 1
        elif existing.source_analysis_id and existing.project_id is None and project_id:
            # Backfill the optional project link for tasks extracted before the
            # project-aware task board was introduced.
            existing.project_id = project_id

    session.flush()
    total = session.scalar(
        select(func.count()).select_from(Task).where(Task.meeting_id == meeting_id)
    ) or 0
    if created:
        log.info("tasks_extracted meeting=%s created=%d total=%d",
                 meeting_id[:8], created, total)
    return {"created": created, "total": total}
