"""Exports, meeting comparisons, multi-summary, recurring topics, timeline. (part of MeetingService)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from core.analysis import schema
from core.export import build_export, export_meeting
from core.store.db import session_scope
from core.store.models import Analysis, Meeting, MeetingTag, Task, TranscriptSegment
from core.logging_setup import get_logger
from core.services._common import utc_iso, berlin_date, UnknownMeetingError

log = get_logger("ma.service")


class AnalyticsMixin:

    def export_meeting(self, meeting_id: str, fmt: str = "markdown",
                       include_analysis: bool = True,
                       include_transcript: bool = True,
                       section_keys: list | None = None) -> dict:
        out = build_export(
            meeting_id, fmt=fmt, config=self.config,
            include_analysis=include_analysis, include_transcript=include_transcript,
            section_keys=section_keys)
        return {
            "meeting_id": meeting_id,
            "requested_format": fmt,
            "format": out["format"],          # effective format after fallback
            "fallback": out["fallback"],      # e.g. "html" when pdf tooling missing
            "note": out["note"],
            "path": out["path"],
            "size": out["size"],
            "is_binary": out["is_binary"],
            "content": out["content"],        # None for binary exports
        }

    # --- Phase 7b: compare, multi-summary, recurring topics, timeline ---
    @staticmethod
    def _parse_dt(value) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            return (parsed.astimezone(timezone.utc).replace(tzinfo=None)
                    if parsed.tzinfo is not None else parsed)
        except (ValueError, TypeError):
            return None

    def _analysis_sections(self, s: Session, meeting_id: str) -> dict:
        a = s.scalar(select(Analysis).where(
            Analysis.meeting_id == meeting_id, Analysis.kind == "summary"))
        obj = schema.extract_json(a.content) if (a and a.content) else None
        return obj or {}

    def compare_meetings(self, meeting_ids: list) -> dict:
        ids = [m for m in (meeting_ids or []) if m]
        if not ids:
            raise ValueError("mindestens ein Meeting erforderlich")
        with session_scope() as s:
            rows = []
            unknown_ids = []
            for mid in ids:
                m = s.get(Meeting, mid)
                if m is None or m.deleted_at is not None:
                    unknown_ids.append(mid)
                    continue
                n_seg = s.scalar(select(func.count(TranscriptSegment.id)).where(
                    TranscriptSegment.meeting_id == mid)) or 0
                n_spk = s.scalar(select(func.count(func.distinct(
                    TranscriptSegment.speaker_id))).where(
                    TranscriptSegment.meeting_id == mid,
                    TranscriptSegment.speaker_id.is_not(None))) or 0
                n_tasks = s.scalar(select(func.count(Task.id)).where(
                    Task.meeting_id == mid)) or 0
                tags = [t.tag for t in s.scalars(select(MeetingTag).where(
                    MeetingTag.meeting_id == mid).order_by(MeetingTag.tag)).all()]
                kf = self._analysis_sections(s, mid).get("kurzfassung", []) or []
                rows.append({
                    "id": m.id, "title": m.title, "status": m.status,
                    "start_at": utc_iso(m.start_at),
                    "duration_s": m.duration_s,
                    "n_segments": n_seg, "n_speakers": n_spk, "n_tasks": n_tasks,
                    "tags": tags,
                    "kurzfassung": [e.get("text") if isinstance(e, dict) else e
                                    for e in kf],
                })
        # L24: an all-unknown selection used to silently return an empty
        # comparison; surface the bad id explicitly instead.
        if not rows:
            first = next(iter(dict.fromkeys(ids)))
            raise UnknownMeetingError(first)
        return {"meetings": rows, "n_meetings": len(rows), "unknown_ids": unknown_ids}

    def multi_summary(self, meeting_ids: list | None = None,
                      since=None, until=None) -> dict:
        """Offline cross-meeting digest (no LLM / no network).

        Pass explicit ``meeting_ids``, or omit them and give a ``since``/``until``
        ISO date range for daily/weekly rollups. Unavailable fields stay empty.
        """
        since_dt = self._parse_dt(since)
        until_dt = self._parse_dt(until)
        with session_scope() as s:
            if meeting_ids:
                query = select(Meeting).where(Meeting.id.in_([m for m in meeting_ids if m]),
                                               Meeting.deleted_at.is_(None))
            else:
                query = select(Meeting).where(Meeting.deleted_at.is_(None))
                if since_dt:
                    query = query.where(Meeting.start_at >= since_dt)
                if until_dt:
                    query = query.where(Meeting.start_at <= until_dt)
                query = query.order_by(Meeting.start_at.desc())
            meetings = list(s.scalars(query).all())
            combined = {"kurzfassung": [], "entscheidungen": [], "aufgaben": [],
                        "offene_fragen": []}
            per = []
            for m in meetings:
                n_seg = s.scalar(select(func.count(TranscriptSegment.id)).where(
                    TranscriptSegment.meeting_id == m.id)) or 0
                n_tasks = s.scalar(select(func.count(Task.id)).where(
                    Task.meeting_id == m.id)) or 0
                obj = self._analysis_sections(s, m.id)
                per.append({"id": m.id, "title": m.title,
                            "start_at": utc_iso(m.start_at),
                            "n_segments": n_seg, "n_tasks": n_tasks})
                for key in combined:
                    for e in obj.get(key, []) or []:
                        text = e.get("text") if isinstance(e, dict) else e
                        if not text:
                            continue
                        entry = {"meeting": m.title, "text": text}
                        if key == "aufgaben" and isinstance(e, dict):
                            entry["verantwortlich"] = e.get("verantwortlich", schema.NOT_GIVEN)
                            entry["deadline"] = e.get("deadline", schema.NOT_GIVEN)
                        combined[key].append(entry)
        return {
            "period": {"since": since, "until": until},
            "n_meetings": len(meetings),
            "meetings": per,
            "digest": combined,
        }

    def recurring_topics(self, min_occurrences: int = 2) -> dict:
        """Topics/tags that appear in at least ``min_occurrences`` meetings."""
        with session_scope() as s:
            meetings = list(s.scalars(select(Meeting).where(Meeting.deleted_at.is_(None)).order_by(Meeting.start_at.desc())).all())
            counts: dict = {}
            for m in meetings:
                topics = set()
                for t in s.scalars(select(MeetingTag).where(MeetingTag.meeting_id == m.id)).all():
                    topics.add(t.tag.strip().lower())
                obj = self._analysis_sections(s, m.id)
                for key in ("themen", "kurzfassung"):
                    for e in obj.get(key, []) or []:
                        text = (e.get("text") if isinstance(e, dict) else e) or ""
                        text = text.strip().lower()
                        if text:
                            topics.add(text)
                for t in topics:
                    rec = counts.setdefault(t, {"topic": t, "meetings": 0, "in_meetings": []})
                    rec["meetings"] += 1
                    rec["in_meetings"].append(m.title)
        recs = [r for r in counts.values() if r["meetings"] >= max(1, min_occurrences)]
        recs.sort(key=lambda r: (-r["meetings"], r["topic"]))
        for rec in recs:
            rec["in_meetings"] = sorted(rec["in_meetings"])
        return {"min_occurrences": min_occurrences, "topics": recs}

    def timeline(self, granularity: str = "day", limit: int = 50) -> dict:
        """Group meetings into day or ISO-week buckets."""
        if granularity not in ("day", "week"):
            raise ValueError("granularitaet muss 'day' oder 'week' sein")
        with session_scope() as s:
            meetings = list(s.scalars(
                select(Meeting).where(Meeting.deleted_at.is_(None)).order_by(
                    Meeting.start_at.desc(), Meeting.id)).all())
        buckets: dict = {}
        for m in meetings:
            if not m.start_at:
                continue
            if granularity == "day":
                key = berlin_date(m.start_at)
            else:
                aware = (m.start_at if m.start_at.tzinfo is not None
                          else m.start_at.replace(tzinfo=timezone.utc))
                local = aware.astimezone(ZoneInfo("Europe/Berlin"))
                iso = local.isocalendar()
                key = f"{iso[0]}-W{iso[1]:02d}"
            rec = buckets.setdefault(key, {"key": key, "n_meetings": 0, "meetings": []})
            rec["n_meetings"] += 1
            rec["meetings"].append({"id": m.id, "title": m.title})
        out = sorted(buckets.values(), key=lambda r: r["key"], reverse=True)
        return {"granularity": granularity, "buckets": out[:max(1, limit)]}
