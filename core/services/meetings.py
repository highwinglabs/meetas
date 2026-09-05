"""Meeting queries, trash/restore, transcript versions. (part of MeetingService)."""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from sqlalchemy import delete, func, select
from core.jobs.queue import JobQueue
from core.store.db import session_scope
from core.store.fts import reindex_meeting, remove_meeting_fts
from core.transcribe.processor import TranscriptionProcessor
from core.store.models import Analysis, Meeting, ProcessingJob, Recording, TranscriptSegment, TranscriptVersion, Project, ProjectFile, new_id, utcnow
from core.logging_setup import get_logger
from core.services._common import utc_iso, _analysis_markdown, ActiveMeetingError, UnknownMeetingError

log = get_logger("ma.service")


class MeetingsMixin:

    def list_meetings(self, project_id: str | None = None) -> list[dict]:
        with session_scope() as s:
            query = select(Meeting).where(Meeting.deleted_at.is_(None)).order_by(
                Meeting.start_at.desc(), Meeting.id)
            if project_id:
                query = query.where(Meeting.project_id == project_id)
            meetings = s.scalars(query).all()
            out = []
            for m in meetings:
                n_seg = s.scalar(
                    select(func.count()).select_from(TranscriptSegment)
                    .where(TranscriptSegment.meeting_id == m.id)
                ) or 0
                recording = s.scalar(select(Recording).where(Recording.meeting_id == m.id))
                out.append({
                    "id": m.id,
                    "title": m.title,
                    "project_id": m.project_id,
                    "status": m.status,
                    "start_at": utc_iso(m.start_at),
                    "end_at": utc_iso(m.end_at),
                    "duration_s": m.duration_s,
                    "lang": m.lang,
                    "segments": n_seg,
                    "original_path": recording.original_path if recording else None,
                })
            return out

    def update_meeting(self, meeting_id: str, title: str | None = None,
                       project_id: str | None = None,
                       project_specified: bool = False) -> dict:
        """Edit user-facing meeting metadata without touching audio/transcript."""
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            if title is not None:
                if not isinstance(title, str):
                    raise ValueError("Der Meetingtitel muss Text sein.")
                new_title = title.strip()
                if not new_title:
                    raise ValueError("Der Meetingtitel darf nicht leer sein.")
                meeting.title = new_title
                meeting.title_status = "manual"
            if project_specified:
                if project_id:
                    project = s.get(Project, project_id)
                    if project is None or project.deleted_at is not None:
                        raise KeyError(project_id)
                meeting.project_id = project_id
            s.commit()
            return {"id": meeting.id, "title": meeting.title,
                    "title_status": meeting.title_status,
                    "project_id": meeting.project_id}

    def trash_meeting(self, meeting_id: str) -> dict:
        with self._state_lock:
            if meeting_id in self._sessions:
                raise ActiveMeetingError("Aktive Aufnahmen können nicht gelöscht werden.")
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None:
                raise UnknownMeetingError(meeting_id)
            if meeting.status in {"recording", "paused"}:
                # The in-memory session map can be briefly empty during a
                # crash/recovery race; the durable status is a second guard.
                raise ActiveMeetingError("Aktive Aufnahmen können nicht gelöscht werden.")
            if meeting.deleted_at is not None:
                return {"id": meeting.id, "deleted_at": utc_iso(meeting.deleted_at)}
            meeting.deleted_at = utcnow()
            s.commit()
            return {"id": meeting.id, "deleted_at": utc_iso(meeting.deleted_at)}

    def restore_meeting(self, meeting_id: str) -> dict:
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is None:
                raise ValueError("Meeting liegt nicht im Papierkorb.")
            meeting.deleted_at = None
            s.commit()
            return {"id": meeting.id, "restored": True}

    def list_trash(self) -> dict:
        with session_scope() as s:
            meetings = s.scalars(select(Meeting).where(Meeting.deleted_at.is_not(None)).order_by(
                Meeting.deleted_at.desc(), Meeting.id)).all()
            projects = s.scalars(select(Project).where(Project.deleted_at.is_not(None)).order_by(
                Project.deleted_at.desc(), Project.id)).all()
            files = s.execute(select(ProjectFile, Project).join(
                Project, Project.id == ProjectFile.project_id).where(
                ProjectFile.deleted_at.is_not(None)).order_by(
                    ProjectFile.deleted_at.desc(), ProjectFile.id)).all()
            return {
                "meetings": [{"id": m.id, "title": m.title, "deleted_at": utc_iso(m.deleted_at),
                              "start_at": utc_iso(m.start_at)} for m in meetings],
                "projects": [{"id": p.id, "name": p.name, "deleted_at": utc_iso(p.deleted_at)}
                             for p in projects],
                "files": [{"id": file.id, "name": file.original_name,
                            "project_id": project.id, "project_name": project.name,
                            "deleted_at": utc_iso(file.deleted_at)}
                           for file, project in files],
            }

    def permanently_delete_meeting(self, meeting_id: str) -> dict:
        with self._state_lock:
            if meeting_id in self._sessions:
                raise ActiveMeetingError("Aktive Aufnahmen können nicht gelöscht werden.")
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is None:
                raise ValueError("Ein Meeting muss zuerst in den Papierkorb verschoben werden.")
            if meeting.status in {"recording", "paused"}:
                raise ActiveMeetingError("Aktive Aufnahmen können nicht gelöscht werden.")
            rec = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
            # Imported audio/video is represented by both Recording and a
            # ProjectFile row. Delete that metadata explicitly: the meeting FK
            # uses SET NULL, so relying on the meeting cascade would otherwise
            # leave a broken project-file entry behind.
            assets = s.scalars(select(ProjectFile).where(
                ProjectFile.meeting_id == meeting_id)).all()
            raw_paths = [rec.original_path] if rec and rec.original_path else []
            raw_paths.extend(asset.path for asset in assets if asset.path)
            for asset in assets:
                s.delete(asset)
            s.delete(meeting)
            # Keep the FTS index consistent: a permanently removed meeting must
            # no longer surface in full-text search results.
            remove_meeting_fts(s, meeting_id)
            s.commit()
        removed_audio = False
        audio_root = self.config.audio_dir.resolve()
        project_root = (self.config.base_dir / "project_files").resolve()
        folders: set[Path] = set()
        for raw_path in dict.fromkeys(raw_paths):
            path = Path(raw_path).resolve()
            if audio_root in path.parents and path.parent != audio_root:
                folders.add(path.parent)
            elif project_root in path.parents and path.is_file():
                try:
                    path.unlink()
                except OSError as exc:
                    log.warning("project_file_delete_failed meeting=%s error=%s",
                                meeting_id[:8], exc)
        for folder in folders:
            try:
                shutil.rmtree(folder)
                removed_audio = True
            except OSError as exc:
                log.warning("audio_delete_failed meeting=%s error=%s", meeting_id[:8], exc)
        return {"id": meeting_id, "deleted": True, "audio_removed": removed_audio}

    def get_meeting(self, meeting_id: str) -> dict:
        with session_scope() as s:
            m = s.get(Meeting, meeting_id)
            if m is None or m.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            recording = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
            segments = s.scalars(
                select(TranscriptSegment).where(TranscriptSegment.meeting_id == meeting_id)
                .order_by(TranscriptSegment.start_s, TranscriptSegment.id)
            ).all()
            jobs = s.scalars(
                select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
            ).all()
            analyses = s.scalars(
                select(Analysis).where(Analysis.meeting_id == meeting_id)
                .order_by(Analysis.created_at, Analysis.id)
            ).all()
            transcript_versions = s.scalars(
                select(TranscriptVersion).where(TranscriptVersion.meeting_id == meeting_id)
                .order_by(TranscriptVersion.version_no, TranscriptVersion.id)
            ).all()
            return {
                "id": m.id,
                "title": m.title,
                "title_status": m.title_status,
                "project_id": m.project_id,
                "settings": self._decode_settings(m.settings_json),
                "status": m.status,
                "start_at": utc_iso(m.start_at),
                "end_at": utc_iso(m.end_at),
                "duration_s": m.duration_s,
                "lang": m.lang,
                "recording": {
                    "source": recording.source,
                    "device": recording.device,
                    "sample_rate": recording.sample_rate,
                    "original_path": recording.original_path,
                    "status": recording.status,
                    "enhancement": self._audio_enhancement_info(
                        m.id, recording.original_path, recording.source),
                } if recording else None,
                "segments": [
                    {
                        "id": seg.id, "start_s": seg.start_s, "end_s": seg.end_s,
                        "text": seg.text, "raw_text": seg.raw_text,
                        "language": seg.language, "confidence": seg.confidence,
                        "speaker_id": seg.speaker_id, "status": seg.status,
                    } for seg in segments
                ],
                "jobs": [
                    {"stage": j.stage, "status": j.status, "progress": j.progress,
                     "error": j.error} for j in jobs
                ],
                "analyses": [
                    {
                        "kind": a.kind, "model": a.model,
                        "output_lang": a.output_lang, "content": a.content,
                        "markdown": _analysis_markdown(a.content, lang=a.output_lang),
                        "created_at": utc_iso(a.created_at),
                    }
                    for a in analyses
                ],
                "transcript_versions": [
                    {"id": v.id, "version_no": v.version_no, "model": v.model,
                     "language": v.language, "is_current": v.is_current,
                     "created_at": utc_iso(v.created_at)}
                    for v in transcript_versions
                ],
            }

    def get_transcript_version(self, meeting_id: str, version_id: str) -> dict:
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            version = s.get(TranscriptVersion, version_id)
            if version is None or version.meeting_id != meeting_id:
                raise KeyError(version_id)
            try:
                segments = json.loads(version.segments_json or "[]")
            except json.JSONDecodeError:
                segments = []
            return {"id": version.id, "meeting_id": meeting_id,
                    "version_no": version.version_no, "model": version.model,
                    "language": version.language, "is_current": version.is_current,
                    "created_at": utc_iso(version.created_at),
                    "segments": segments if isinstance(segments, list) else []}

    def restore_transcript_version(self, meeting_id: str, version_id: str) -> dict:
        """Restore a saved transcript snapshot without touching the audio."""
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            version = s.get(TranscriptVersion, version_id)
            if version is None or version.meeting_id != meeting_id:
                raise KeyError(version_id)
            try:
                payload = json.loads(version.segments_json or "[]")
            except json.JSONDecodeError as exc:
                raise ValueError("Die gespeicherte Transkriptversion ist beschädigt.") from exc
            if not isinstance(payload, list):
                raise ValueError("Die gespeicherte Transkriptversion ist ungültig.")
            # Normalize legacy snapshots once.  Older versions did not store
            # segment IDs; generating an ID during validation and a different
            # one during insertion made the restored payload disagree with the
            # actual rows and orphaned source references.
            normalized_payload: list[dict] = []
            seen_ids: set[str] = set()
            for item in payload:
                if not isinstance(item, dict) or not str(item.get("text", "")).strip():
                    continue
                raw_id = item.get("id")
                seg_id = str(raw_id) if raw_id else new_id()
                if seg_id in seen_ids:
                    raise ValueError("Die gespeicherte Transkriptversion enthält doppelte Segment-IDs.")
                seen_ids.add(seg_id)
                try:
                    start_s = float(item.get("start_s", 0.0))
                    end_s = float(item.get("end_s", item.get("start_s", 0.0)))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Die gespeicherte Transkriptversion enthält ungültige Zeitdaten.") from exc
                if not (math.isfinite(start_s) and math.isfinite(end_s)
                        and start_s >= 0 and end_s >= start_s):
                    raise ValueError("Die gespeicherte Transkriptversion enthält ungültige Zeitbereiche.")
                other = s.get(TranscriptSegment, seg_id)
                if other is not None and other.meeting_id != meeting_id:
                    raise ValueError("Die gespeicherte Transkriptversion verweist auf ein fremdes Segment.")
                normalized = dict(item)
                normalized["id"] = seg_id
                normalized_payload.append(normalized)
            current = s.scalars(select(TranscriptVersion).where(
                TranscriptVersion.meeting_id == meeting_id,
                TranscriptVersion.is_current.is_(True))).all()
            for row in current:
                row.is_current = False
            s.execute(delete(TranscriptSegment).where(
                TranscriptSegment.meeting_id == meeting_id))
            for item in normalized_payload:
                s.add(TranscriptSegment(
                    id=item["id"], meeting_id=meeting_id,
                    start_s=float(item.get("start_s", 0.0)),
                    end_s=float(item.get("end_s", item.get("start_s", 0.0))),
                    text=str(item.get("text", "")),
                    raw_text=str(item.get("raw_text", item.get("text", ""))),
                    language=item.get("language"), confidence=item.get("confidence"),
                    speaker_id=item.get("speaker_id"), status="bestaetigt"))
            next_no = (max((v.version_no for v in s.scalars(select(TranscriptVersion).where(
                TranscriptVersion.meeting_id == meeting_id)).all()), default=0) + 1)
            restored = TranscriptVersion(
                id=new_id(), meeting_id=meeting_id, version_no=next_no,
                model=f"wiederhergestellt aus Version {version.version_no}",
                language=version.language,
                segments_json=json.dumps(normalized_payload, ensure_ascii=False),
                is_current=True)
            s.add(restored)
            # Existing vectors refer to the replaced active segment set.
            from core.store.models import SegmentEmbedding
            s.execute(delete(SegmentEmbedding).where(
                SegmentEmbedding.meeting_id == meeting_id))
            meeting.lang = version.language
            job = JobQueue.get_or_create(s, meeting_id, "transcribe")
            JobQueue.mark_done(s, job)
            n = reindex_meeting(s, meeting_id)
            s.commit()
            return {"meeting_id": meeting_id, "version_id": restored.id,
                    "segments": len(normalized_payload),
                    "fts_rows": n}

    def _backfill_transcript_versions(self) -> None:
        """Add non-destructive snapshots for legacy meetings once."""
        with session_scope() as s:
            meetings = s.scalars(select(Meeting)).all()
            for meeting in meetings:
                if s.scalar(select(TranscriptVersion.id).where(
                        TranscriptVersion.meeting_id == meeting.id).limit(1)):
                    continue
                segments = s.scalars(select(TranscriptSegment).where(
                    TranscriptSegment.meeting_id == meeting.id).order_by(
                    TranscriptSegment.start_s)).all()
                if not segments:
                    continue
                s.add(TranscriptVersion(
                    id=new_id(), meeting_id=meeting.id, version_no=1,
                    model="unbekannt (Bestand)", language=meeting.lang,
                    segments_json=json.dumps(
                        TranscriptionProcessor._segment_payload(segments),
                        ensure_ascii=False),
                    is_current=True))
            s.commit()

    @staticmethod
    def _decode_settings(value: str | None) -> dict:
        try:
            obj = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return obj if isinstance(obj, dict) else {}
