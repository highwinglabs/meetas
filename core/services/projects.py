"""Projects, project files and document indexing. (part of MeetingService)."""
from __future__ import annotations

import re
from pathlib import Path
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from core.project_documents import extract_document
from core.store.db import session_scope
from core.store.models import Meeting, Recording, Task, Project, ProjectFile, ProjectDocumentChunk, utcnow
from core.logging_setup import get_logger
from core.services._common import utc_iso, ActiveMeetingError

log = get_logger("ma.service")


class ProjectsMixin:

    def list_projects(self, include_archived: bool = False) -> list[dict]:
        with session_scope() as s:
            q = select(Project).where(Project.deleted_at.is_(None)).order_by(
                Project.updated_at.desc(), Project.name, Project.id)
            if not include_archived:
                q = q.where(Project.status == "active")
            projects = s.scalars(q).all()
            result = []
            for p in projects:
                meeting_count = s.scalar(select(func.count(Meeting.id)).where(
                    Meeting.project_id == p.id, Meeting.deleted_at.is_(None))) or 0
                file_count = s.scalar(select(func.count(ProjectFile.id)).where(
                    ProjectFile.project_id == p.id,
                    ProjectFile.deleted_at.is_(None))) or 0
                result.append({"id": p.id, "name": p.name, "description": p.description,
                               "status": p.status, "meetings": meeting_count,
                               "files": file_count,
                               "created_at": utc_iso(p.created_at),
                               "updated_at": utc_iso(p.updated_at)})
            return result

    def create_project(self, name: str, description: str = "") -> dict:
        if not isinstance(name, str):
            raise ValueError("Projektname muss Text sein.")
        if not isinstance(description, str):
            raise ValueError("Die Projektbeschreibung muss Text sein.")
        name = name.strip()
        if not name:
            raise ValueError("Projektname darf nicht leer sein.")
        if len(name) > 512 or len(description) > 10_000:
            raise ValueError("Projektname oder Beschreibung ist zu lang.")
        with session_scope() as s:
            p = Project(name=name, description=(description or "").strip())
            s.add(p)
            s.commit()
            return {"id": p.id, "name": p.name, "description": p.description,
                    "status": p.status, "meetings": 0, "files": 0}

    def update_project(self, project_id: str, name: str | None = None,
                       description: str | None = None, status: str | None = None) -> dict:
        with session_scope() as s:
            p = s.get(Project, project_id)
            if p is None or p.deleted_at is not None:
                raise KeyError(project_id)
            if name is not None and not isinstance(name, str):
                raise ValueError("Projektname muss Text sein.")
            if description is not None and not isinstance(description, str):
                raise ValueError("Die Projektbeschreibung muss Text sein.")
            if name is not None and len(name) > 512:
                raise ValueError("Der Projektname ist zu lang.")
            if description is not None and len(description) > 10_000:
                raise ValueError("Die Projektbeschreibung ist zu lang.")
            if name is not None and name.strip():
                p.name = name.strip()
            if description is not None:
                p.description = description.strip()
            if status is not None:
                if status not in {"active", "archived"}:
                    raise ValueError("Unbekannter Projektstatus.")
                p.status = status
            s.commit()
            return {"id": p.id, "name": p.name, "description": p.description,
                    "status": p.status}

    def trash_project(self, project_id: str) -> dict:
        # Project deletion must obey the same active-recording invariant as
        # meeting deletion. Serialize with meeting admission so a start cannot
        # race the check and leave an active recording in a trashed project.
        with self._meeting_start_guard:
            with session_scope() as s:
                p = s.get(Project, project_id)
                if p is None:
                    raise KeyError(project_id)
                active = s.scalar(select(Meeting.id).where(
                    Meeting.project_id == project_id,
                    Meeting.status.in_(("recording", "paused")),
                    Meeting.deleted_at.is_(None)).limit(1))
                if active is not None:
                    raise ActiveMeetingError(
                        "Ein Projekt mit aktiver Aufnahme kann nicht gelöscht werden.")
                p.deleted_at = p.deleted_at or utcnow()
                s.commit()
                return {"id": p.id, "deleted_at": utc_iso(p.deleted_at)}

    def restore_project(self, project_id: str) -> dict:
        with session_scope() as s:
            p = s.get(Project, project_id)
            if p is None or p.deleted_at is None:
                raise ValueError("Projekt liegt nicht im Papierkorb.")
            p.deleted_at = None
            s.commit()
            return {"id": p.id, "restored": True}

    def permanently_delete_project(self, project_id: str) -> dict:
        with session_scope() as s:
            p = s.get(Project, project_id)
            if p is None or p.deleted_at is None:
                raise ValueError("Ein Projekt muss zuerst in den Papierkorb verschoben werden.")
            active = s.scalar(select(Meeting.id).where(
                Meeting.project_id == project_id,
                Meeting.status.in_(("recording", "paused")),
                Meeting.deleted_at.is_(None)).limit(1))
            if active is not None:
                raise ActiveMeetingError(
                    "Ein Projekt mit aktiver Aufnahme kann nicht endgültig gelöscht werden.")
            assets = s.scalars(select(ProjectFile).where(
                ProjectFile.project_id == project_id)).all()
            # A project may contain an imported audio/video file that is also
            # the recording of a meeting. Removing the project must not erase
            # that meeting's audio; only the project-file metadata is removed.
            document_paths = [row.path for row in assets
                              if row.meeting_id is None and row.path]
            s.query(Meeting).filter(Meeting.project_id == project_id).update({Meeting.project_id: None})
            s.query(Task).filter(Task.project_id == project_id).update({Task.project_id: None})
            s.delete(p)
            s.commit()
        files_removed = 0
        file_root = (self.config.base_dir / "project_files").resolve()
        for raw_path in dict.fromkeys(document_paths):
            path = Path(raw_path).resolve()
            if file_root in path.parents and path.is_file():
                try:
                    path.unlink()
                    files_removed += 1
                except OSError as exc:
                    log.warning("project_file_delete_failed project=%s error=%s", project_id[:8], exc)
        return {"id": project_id, "deleted": True, "files_removed": files_removed}

    def trash_project_file(self, file_id: str) -> dict:
        """Move one project file to the recoverable paper bin."""
        with session_scope() as s:
            asset = s.get(ProjectFile, file_id)
            if asset is None or asset.project_id is None:
                raise KeyError(file_id)
            if asset.meeting_id:
                meeting = s.get(Meeting, asset.meeting_id)
                if meeting is not None and meeting.status in {"recording", "paused"}:
                    raise ActiveMeetingError(
                        "Dateien einer aktiven Aufnahme können nicht gelöscht werden.")
            if asset.deleted_at is None:
                asset.deleted_at = utcnow()
            s.commit()
            return {"id": asset.id, "deleted_at": utc_iso(asset.deleted_at)}

    def restore_project_file(self, file_id: str) -> dict:
        """Restore one project file without changing its stored contents."""
        with session_scope() as s:
            asset = s.get(ProjectFile, file_id)
            if asset is None or asset.project_id is None or asset.deleted_at is None:
                raise ValueError("Datei liegt nicht im Papierkorb.")
            asset.deleted_at = None
            s.commit()
            return {"id": asset.id, "restored": True}

    def permanently_delete_project_file(self, file_id: str) -> dict:
        """Irreversibly remove a trashed project file and its extracted chunks."""
        with session_scope() as s:
            asset = s.get(ProjectFile, file_id)
            if asset is None or asset.project_id is None or asset.deleted_at is None:
                raise ValueError("Eine Datei muss zuerst in den Papierkorb verschoben werden.")
            if asset.meeting_id:
                meeting = s.get(Meeting, asset.meeting_id)
                if meeting is not None and meeting.status in {"recording", "paused"}:
                    raise ActiveMeetingError(
                        "Dateien einer aktiven Aufnahme können nicht endgültig gelöscht werden.")
            raw_path = asset.path
            # Imported audio/video files are also the source of a surviving
            # meeting. Deleting their project entry must never unlink the
            # recording that the meeting still references.
            preserves_recording = False
            if asset.meeting_id:
                recording_path = s.scalar(select(Recording.original_path).where(
                    Recording.meeting_id == asset.meeting_id))
                preserves_recording = bool(recording_path and
                                           Path(recording_path).resolve() == Path(raw_path).resolve())
            s.delete(asset)
            s.commit()
        removed = False
        path = Path(raw_path).resolve()
        file_root = (self.config.base_dir / "project_files").resolve()
        if not preserves_recording and file_root in path.parents and path.is_file():
            try:
                path.unlink()
                removed = True
            except OSError as exc:
                log.warning("project_file_delete_failed file=%s error=%s", file_id[:8], exc)
        return {"id": file_id, "deleted": True, "file_removed": removed}

    def project_detail(self, project_id: str) -> dict:
        with session_scope() as s:
            p = s.get(Project, project_id)
            if p is None or p.deleted_at is not None:
                raise KeyError(project_id)
            meetings = s.scalars(select(Meeting).where(
                Meeting.project_id == project_id, Meeting.deleted_at.is_(None)).order_by(
                    Meeting.start_at.desc(), Meeting.id)).all()
            files = s.scalars(select(ProjectFile).where(
                ProjectFile.project_id == project_id,
                ProjectFile.deleted_at.is_(None)).order_by(
                    ProjectFile.created_at.desc(), ProjectFile.id)).all()
            return {
                "id": p.id, "name": p.name, "description": p.description,
                "status": p.status,
                "meetings": [{"id": m.id, "title": m.title, "status": m.status,
                              "start_at": utc_iso(m.start_at),
                              "duration_s": m.duration_s} for m in meetings],
                "files": [{"id": f.id, "name": f.original_name, "kind": f.kind,
                           "mime_type": f.mime_type, "size": f.size,
                           "extraction_status": (f.extraction_status or "pending")
                           if f.kind == "document" else "not_applicable",
                           "extraction_error": f.extraction_error,
                           "extracted_chars": int(f.extracted_chars or 0),
                           "indexed_at": utc_iso(f.indexed_at),
                           "chunks": len(f.chunks),
                           "created_at": utc_iso(f.created_at)}
                          for f in files],
            }

    def index_project_file(self, file_id: str) -> dict:
        """Extract one project document locally and replace its chunks atomically."""
        with session_scope() as s:
            asset = s.get(ProjectFile, file_id)
            if asset is None or asset.deleted_at is not None:
                raise KeyError(file_id)
            if asset.kind != "document" or not asset.project_id:
                return {"id": file_id, "status": "skipped", "chunks": 0}
            path = Path(asset.path).resolve()
            # Documents are always stored below the dedicated project-files
            # root.  Checking only base_dir would allow a corrupted metadata
            # row to make a parser read audio, cache, or other private files.
            root = (self.config.base_dir / "project_files").resolve()
            if root not in path.parents or not path.is_file():
                asset.extraction_status = "failed"
                asset.extraction_error = "Datei liegt außerhalb des lokalen Speicherbereichs."
                s.commit()
                return {"id": file_id, "status": "failed", "chunks": 0,
                        "error": asset.extraction_error}
            asset.extraction_status = "processing"
            asset.extraction_error = None
            s.commit()
            filename = asset.original_name
        try:
            chunks = extract_document(path, filename)
        except Exception as exc:  # noqa: BLE001 - extraction is a user-file boundary
            with session_scope() as s:
                asset = s.get(ProjectFile, file_id)
                if asset is not None:
                    asset.extraction_status = "failed"
                    asset.extraction_error = str(exc)[:2000]
                    asset.extracted_chars = 0
                    asset.indexed_at = None
                    s.query(ProjectDocumentChunk).filter(
                        ProjectDocumentChunk.project_file_id == file_id).delete(
                            synchronize_session=False)
                    s.commit()
            return {"id": file_id, "status": "failed", "chunks": 0, "error": str(exc)}
        with session_scope() as s:
            asset = s.get(ProjectFile, file_id)
            if asset is None or asset.deleted_at is not None:
                raise KeyError(file_id)
            s.query(ProjectDocumentChunk).filter(
                ProjectDocumentChunk.project_file_id == file_id).delete(
                    synchronize_session=False)
            for index, chunk in enumerate(chunks):
                s.add(ProjectDocumentChunk(
                    id=f"{file_id}{index:06d}", project_file_id=file_id,
                    project_id=asset.project_id, chunk_index=index,
                    text=chunk.text, locator=chunk.locator))
            asset.extraction_status = "ready"
            asset.extraction_error = None
            asset.extracted_chars = sum(len(chunk.text) for chunk in chunks)
            asset.indexed_at = utcnow()
            s.commit()
            return {"id": file_id, "status": "ready", "chunks": len(chunks),
                    "extracted_chars": asset.extracted_chars}

    def reindex_project_files(self, project_id: str) -> dict:
        """(Re-)index all document files in a project, preserving failed status per file."""
        with session_scope() as s:
            project = s.get(Project, project_id)
            if project is None or project.deleted_at is not None:
                raise KeyError(project_id)
            file_ids = [row.id for row in s.scalars(select(ProjectFile).where(
                ProjectFile.project_id == project_id,
                ProjectFile.kind == "document",
                ProjectFile.deleted_at.is_(None))).all()]
        results = [self.index_project_file(file_id) for file_id in file_ids]
        return {"project_id": project_id, "files": len(results),
                "ready": sum(item.get("status") == "ready" for item in results),
                "failed": sum(item.get("status") == "failed" for item in results)}

    @staticmethod
    def _project_document_hits(session: Session, project_id: str,
                               query: str, limit: int = 10) -> list[dict]:
        """Return query-relevant extracted chunks, scoped strictly to a project."""
        import re
        terms = [term.lower() for term in re.findall(r"[\wÄÖÜäöüß]{3,}", query or "")]
        if not terms:
            return []
        rows = session.execute(select(ProjectDocumentChunk, ProjectFile).join(
            ProjectFile, ProjectFile.id == ProjectDocumentChunk.project_file_id).where(
            ProjectDocumentChunk.project_id == project_id,
            ProjectFile.project_id == project_id,
            ProjectFile.extraction_status == "ready",
                ProjectFile.deleted_at.is_(None))).all()
        ranked: list[tuple[int, ProjectDocumentChunk, ProjectFile]] = []
        for chunk, asset in rows:
            haystack = (chunk.text or "").lower()
            score = sum(haystack.count(term) for term in terms)
            if score:
                ranked.append((score, chunk, asset))
        ranked.sort(key=lambda item: (-item[0], item[1].chunk_index, item[1].id))
        return [{
            "segment_id": f"doc{asset.id}{chunk.chunk_index:06d}",
            "meeting_id": None,
            "start_s": None,
            "end_s": None,
            "text": chunk.text,
            "speaker_id": None,
            "meeting_title": asset.original_name,
            "snippet": chunk.text[:200],
            "source_kind": "document",
            "file_id": asset.id,
            "file_name": asset.original_name,
            "locator": chunk.locator,
        } for _, chunk, asset in ranked[:limit]]

    def project_file_path(self, file_id: str) -> tuple[Path, str, str | None]:
        """Return a locally stored project file after containment checking.

        Project files are never served by arbitrary paths from the request. The
        database row must exist and the resolved path must remain below the
        application data directory.
        """
        with session_scope() as s:
            row = s.get(ProjectFile, file_id)
            if row is None or row.deleted_at is not None:
                raise KeyError(file_id)
            raw_path, name, mime, kind = row.path, row.original_name, row.mime_type, row.kind
        path = Path(raw_path).resolve()
        root = ((self.config.base_dir / "project_files") if kind == "document"
                else self.config.audio_dir).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError("Die Projektdatei wurde nicht gefunden.")
        return path, name, mime
