"""Markers, segment revisions, tags and auto title/tags. (part of MeetingService)."""
from __future__ import annotations

import re
from sqlalchemy import delete, select
from core.store.db import session_scope
from core.store.fts import reindex_meeting
from core.store.models import Analysis, Meeting, MeetingTag, Marker, Revision, TranscriptSegment
from core import meta as _meta
from core.logging_setup import get_logger
from core.services._common import UnknownMeetingError

log = get_logger("ma.service")


class MarkersMixin:

    # --- Phase 7a: markers, revisions (auditable edits), tags, auto title/tags ---
    def add_marker(self, meeting_id: str, at_s: float, text: str) -> dict:
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            m = Marker(meeting_id=meeting_id, at_s=float(at_s), text=(text or "").strip())
            s.add(m)
            s.commit()
            return {"id": m.id, "meeting_id": meeting_id, "at_s": m.at_s, "text": m.text}

    def list_markers(self, meeting_id: str) -> list[dict]:
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            rows = s.scalars(select(Marker).where(Marker.meeting_id == meeting_id)
                             .order_by(Marker.at_s)).all()
            return [{"id": m.id, "at_s": m.at_s, "text": m.text} for m in rows]

    def delete_marker(self, marker_id: str) -> dict:
        with session_scope() as s:
            m = s.get(Marker, marker_id)
            if m is None:
                raise KeyError(marker_id)
            meeting = s.get(Meeting, m.meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(m.meeting_id)
            s.delete(m)
            s.commit()
            return {"id": marker_id, "deleted": True}

    def edit_segment(self, meeting_id: str, segment_id: str, text: str,
                     speaker_id: str | None = None) -> dict:
        """Edit a transcript segment with an auditable revision (Phase 7).

        The original text is preserved in a ``Revision`` row; the segment is
        updated in place and the FTS index + vector (if present) are refreshed."""
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            seg = s.get(TranscriptSegment, segment_id)
            if seg is None or seg.meeting_id != meeting_id:
                raise KeyError(segment_id)
            new_text = (text or "").strip()
            if new_text != (seg.text or "").strip():
                s.add(Revision(meeting_id=meeting_id, segment_id=segment_id,
                               field="text", original=seg.text or "",
                               updated=new_text))
                seg.text = new_text
            if speaker_id is not None and speaker_id != seg.speaker_id:
                s.add(Revision(meeting_id=meeting_id, segment_id=segment_id,
                               field="speaker_id", original=seg.speaker_id or "",
                               updated=speaker_id))
                seg.speaker_id = speaker_id
            reindex_meeting(s, meeting_id)
            actual_speaker_id = seg.speaker_id
            s.commit()
        # Refresh the vector for this segment if an index exists (best effort).
        # A refresh failure must not break the edit, but it must not be silently
        # swallowed either -- log it so a stuck/drifted vector index is visible.
        try:
            if self._embed_manager.is_enabled():
                engine = self._embed_manager.engine()
                with session_scope() as s:
                    from core.search.vectorstore import upsert_segment_embedding
                    upsert_segment_embedding(s, meeting_id, segment_id, engine)
                    s.commit()
        except Exception as exc:  # embedding refresh is best-effort but observable
            log.warning("segment_embedding_refresh_failed meeting=%s segment=%s error=%s",
                        meeting_id[:8], segment_id, exc, exc_info=True)
            # Never leave a vector for the old text active after an edit.  If a
            # refresh fails, removing that one stale row makes search fall back
            # to FTS for the segment until the meeting is explicitly re-indexed.
            try:
                from core.store.models import SegmentEmbedding
                with session_scope() as s:
                    s.execute(delete(SegmentEmbedding).where(
                        SegmentEmbedding.meeting_id == meeting_id,
                        SegmentEmbedding.seg_id == segment_id))
                    s.commit()
            except Exception:
                log.debug("segment_embedding_stale_cleanup_failed meeting=%s segment=%s",
                          meeting_id[:8], segment_id, exc_info=True)
        return {"id": segment_id, "text": new_text,
                "speaker_id": actual_speaker_id}

    def list_revisions(self, meeting_id: str) -> list[dict]:
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            rows = s.scalars(select(Revision).where(Revision.meeting_id == meeting_id)
                             .order_by(Revision.created_at)).all()
            return [{"id": r.id, "segment_id": r.segment_id, "field": r.field,
                     "original": r.original, "updated": r.updated} for r in rows]

    def add_tag(self, meeting_id: str, tag: str) -> list[dict]:
        tag = (tag or "").strip()
        if not tag:
            raise ValueError("Tag darf nicht leer sein.")
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            exists = s.scalar(select(MeetingTag).where(
                MeetingTag.meeting_id == meeting_id, MeetingTag.tag == tag))
            if exists is None:
                s.add(MeetingTag(meeting_id=meeting_id, tag=tag))
                s.commit()
            return [t.tag for t in s.scalars(select(MeetingTag).where(
                MeetingTag.meeting_id == meeting_id).order_by(MeetingTag.tag)).all()]

    def remove_tag(self, meeting_id: str, tag: str) -> list[dict]:
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            t = s.scalar(select(MeetingTag).where(
                MeetingTag.meeting_id == meeting_id, MeetingTag.tag == tag))
            if t is not None:
                s.delete(t)
                s.commit()
            return [r.tag for r in s.scalars(select(MeetingTag).where(
                MeetingTag.meeting_id == meeting_id).order_by(MeetingTag.tag)).all()]

    def list_tags(self, meeting_id: str) -> list[dict]:
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            rows = s.scalars(select(MeetingTag).where(
                MeetingTag.meeting_id == meeting_id).order_by(MeetingTag.tag)).all()
            return [{"tag": t.tag} for t in rows]

    def apply_auto_title_tags(self, meeting_id: str,
                              force: bool = False) -> dict:
        """Derive an auto title + tags from the stored analysis (local only).

        The title is only overwritten when it is still the default/``auto`` (or
        ``force``); auto tags are merged with existing tags (idempotent)."""
        if not self.config.auto_title_tags:
            return {"applied": False, "reason": "auto_title_tags deaktiviert"}
        with session_scope() as s:
            m = s.get(Meeting, meeting_id)
            if m is None or m.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            analysis = s.scalar(select(Analysis).where(
                Analysis.meeting_id == meeting_id, Analysis.kind == "summary"))
            if analysis is None or not analysis.content:
                return {"applied": False, "reason": "keine Analyse vorhanden"}
            data = _meta.analysis_from_content(analysis.content)
            if data is None:
                return {"applied": False, "reason": "Analyse nicht lesbar"}
            changed_title = False
            title = _meta.auto_title(data)
            if title and (force or m.title_status == "auto"
                          or m.title in ("", "Neues Meeting")):
                if title != m.title:
                    m.title = title
                    m.title_status = "auto"
                    changed_title = True
            tags = _meta.auto_tags(data)
            existing = {t.tag for t in s.scalars(
                select(MeetingTag).where(MeetingTag.meeting_id == meeting_id)).all()}
            added = 0
            for tg in tags:
                if tg not in existing:
                    s.add(MeetingTag(meeting_id=meeting_id, tag=tg))
                    existing.add(tg)
                    added += 1
            s.commit()
            return {
                "applied": changed_title or added > 0,
                "title": m.title, "title_status": m.title_status,
                "title_changed": changed_title, "tags_added": added,
                "tags": sorted(existing),
            }
