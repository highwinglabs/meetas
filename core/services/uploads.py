"""Resumable browser uploads and import of completed uploads. (part of MeetingService)."""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import wave
from pathlib import Path
from sqlalchemy import select
from core.audio.assembly import repair_wav_header
from core.jobs.queue import JobQueue
from core.store.db import session_scope
from core.store.models import Meeting, Recording, Project, ProjectFile, UploadSession, utcnow
from core.logging_setup import get_logger
from core.services._common import utc_iso, parse_utc_datetime

log = get_logger("ma.service")


class UploadsMixin:

    def import_upload(self, filename: str, content: bytes | None, *, title: str | None = None,
                      project_id: str | None = None, settings: dict | None = None,
                      content_type: str | None = None,
                      start_at: str | None = None,
                      object_id: str | None = None,
                      _source_path: Path | None = None) -> dict:
        """Store an upload locally; audio/video becomes a normal meeting."""
        safe_name = Path(filename or "upload").name.replace("\x00", "") or "upload"
        if len(safe_name) > 512:
            raise ValueError("Der Dateiname ist zu lang.")
        if title is not None and not isinstance(title, str):
            raise ValueError("Der Titel muss Text sein.")
        if title is not None and len(title) > 512:
            raise ValueError("Der Titel ist zu lang.")
        title = title.strip() if title and title.strip() else None
        suffix = Path(safe_name).suffix.lower()
        audio_ext = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
        video_ext = {".mp4", ".mkv", ".webm", ".mov", ".avi"}
        document_ext = {".txt", ".md", ".csv", ".log", ".pdf", ".docx", ".odt", ".xlsx", ".pptx"}
        kind = "audio" if suffix in audio_ext else ("video" if suffix in video_ext else "document")
        if suffix in document_ext and not project_id:
            raise ValueError("Projektdateien müssen einem Projekt zugeordnet werden.")
        source_path = None
        if _source_path is not None:
            source_path = Path(_source_path).resolve()
            temp_root = (self.config.base_dir / "upload_tmp").resolve()
            if temp_root not in source_path.parents or not source_path.is_file():
                raise ValueError("Ungültiger temporärer Uploadpfad.")
            content_size = source_path.stat().st_size
        else:
            if content is not None and not isinstance(content, (bytes, bytearray, memoryview)):
                raise ValueError("Upload-Inhalt muss binär sein.")
            content_size = len(content or b"")
        with session_scope() as s:
            if project_id:
                project = s.get(Project, project_id)
                if project is None or project.deleted_at is not None:
                    raise KeyError(project_id)
        if content_size <= 0:
            raise ValueError("Die hochgeladene Datei ist leer.")
        if content_size > int(self.config.max_upload_bytes):
            raise ValueError("Upload ist zu groß.")

        # Resumable completion uses the upload id as a deterministic object id.
        # If the process crashed after the meeting/file commit but before the
        # UploadSession status update, retrying therefore returns the existing
        # object instead of creating a duplicate.
        if object_id:
            with session_scope() as s:
                if kind == "document":
                    existing = s.get(ProjectFile, object_id)
                    if existing is not None:
                        return {"id": existing.id, "kind": existing.kind,
                                "name": existing.original_name,
                                "project_id": existing.project_id,
                                "meeting_id": existing.meeting_id,
                                "size": existing.size}
                else:
                    existing = s.get(Meeting, object_id)
                    if existing is not None:
                        return {"id": object_id, "kind": kind, "name": safe_name,
                                "project_id": existing.project_id,
                                "meeting_id": existing.id, "size": content_size}

        file_id = object_id or os.urandom(16).hex()
        if kind == "document":
            target_dir = self.config.base_dir / "project_files"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{file_id}_{safe_name}"
            if source_path is not None:
                self._atomic_copy(source_path, target)
            else:
                self._atomic_write(target, content or b"")
            try:
                with session_scope() as s:
                    asset = ProjectFile(id=file_id, project_id=project_id,
                                        original_name=safe_name, path=str(target),
                                        mime_type=content_type or mimetypes.guess_type(safe_name)[0],
                                        kind=kind, size=content_size)
                    s.add(asset)
                    s.commit()
            except Exception:
                # Do not leave an orphaned binary when the metadata transaction
                # fails.  A concurrent idempotent import may already have won;
                # in that case its database row owns the file and must remain.
                with session_scope() as check:
                    committed = check.get(ProjectFile, file_id) is not None
                if not committed:
                    target.unlink(missing_ok=True)
                raise
            if project_id:
                self.index_project_file(file_id)
            return {"id": file_id, "kind": kind, "name": safe_name,
                    "project_id": project_id, "meeting_id": None, "size": content_size}

        meeting_id = object_id or os.urandom(16).hex()
        meeting_dir = self.config.audio_dir / meeting_id
        meeting_dir.mkdir(parents=True, exist_ok=True)
        try:
            meeting_dir.chmod(0o700)
        except OSError:
            pass
        original = meeting_dir / f"original{suffix or '.bin'}"
        if source_path is not None:
            self._atomic_copy(source_path, original)
        else:
            self._atomic_write(original, content or b"")
        if kind == "audio" and suffix == ".wav":
            # Some encoders write WAV files with zeroed/wrong RIFF or data
            # chunk sizes; the payload is intact but players and the wave
            # module cannot use the file. Repair the header losslessly.
            try:
                repair_wav_header(original)
            except OSError:
                log.warning("wav_header_repair_failed path=%s", original.name)
        asr_copy = meeting_dir / "original_16k.wav"
        tmp_copy = asr_copy.with_suffix(".wav.tmp")
        if not self.config.ffmpeg_available():
            shutil.rmtree(meeting_dir, ignore_errors=True)
            raise ValueError("ffmpeg ist für Audio-/Video-Uploads erforderlich.")
        try:
            proc = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(original),
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-f", "wav", "-y",
                str(tmp_copy)], capture_output=True, text=True,
                timeout=max(300.0, min(3600.0, content_size / (1024 * 1024) * 4.0)))
        except subprocess.TimeoutExpired as exc:
            tmp_copy.unlink(missing_ok=True)
            shutil.rmtree(meeting_dir, ignore_errors=True)
            raise ValueError("Audioverarbeitung dauerte zu lange und wurde beendet.") from exc
        if proc.returncode != 0:
            tmp_copy.unlink(missing_ok=True)
            shutil.rmtree(meeting_dir, ignore_errors=True)
            raise ValueError(f"Audio konnte nicht aus dem Upload gelesen werden: {proc.stderr.strip()[:300]}")
        os.replace(tmp_copy, asr_copy)
        try:
            os.chmod(original, 0o444)
            os.chmod(asr_copy, 0o444)
        except OSError:
            pass
        meeting_settings = self._normalise_meeting_settings(settings)
        meeting_start = None
        if start_at:
            meeting_start = parse_utc_datetime(start_at)
        try:
            with session_scope() as s:
                m = Meeting(id=meeting_id, title=(title or Path(safe_name).stem or "Importiertes Meeting"),
                            status="processing", project_id=project_id,
                            settings_json=json.dumps(meeting_settings, ensure_ascii=False),
                            start_at=meeting_start or utcnow())
                s.add(m)
                s.add(Recording(meeting_id=meeting_id, source="upload", device=safe_name,
                                sample_rate=16000, channels=1, original_path=str(original),
                                status="assembled", in_progress=False))
                s.add(ProjectFile(id=file_id, project_id=project_id, meeting_id=meeting_id,
                                  original_name=safe_name, path=str(original),
                                  mime_type=content_type or mimetypes.guess_type(safe_name)[0],
                                  kind=kind, size=content_size))
                JobQueue.get_or_create(s, meeting_id, "transcribe")
                s.commit()
        except Exception:
            # See the document branch above.  Keep artifacts only when the
            # corresponding meeting row was committed by a competing retry.
            with session_scope() as check:
                committed = check.get(Meeting, meeting_id) is not None
            if not committed:
                shutil.rmtree(meeting_dir, ignore_errors=True)
            raise
        if self.config.auto_pipeline:
            self.schedule_pipeline(meeting_id)
        else:
            self._set_meeting_status(meeting_id, "ready")
        return {"id": file_id, "kind": kind, "name": safe_name,
                "project_id": project_id, "meeting_id": meeting_id, "size": content_size}

    # --- resumable browser uploads ---------------------------------------
    def start_upload(self, filename: str, total_size: int, *, title: str | None = None,
                     project_id: str | None = None, settings: dict | None = None,
                     content_type: str | None = None, start_at: str | None = None) -> dict:
        try:
            total_size = int(total_size or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Die Upload-Größe muss eine Zahl sein.") from exc
        if total_size <= 0:
            raise ValueError("Die hochgeladene Datei ist leer.")
        max_bytes = int(self.config.max_upload_bytes)
        if total_size > max_bytes:
            raise ValueError(
                f"Upload ist zu groß (max. {max_bytes // (1024 * 1024)} MiB).")
        safe_name = Path(filename or "upload").name.replace("\x00", "") or "upload"
        if len(safe_name) > 512:
            raise ValueError("Der Dateiname ist zu lang.")
        if title is not None and not isinstance(title, str):
            raise ValueError("Der Titel muss Text sein.")
        if title is not None and len(title) > 512:
            raise ValueError("Der Titel ist zu lang.")
        title = title.strip() if title and title.strip() else None
        suffix = Path(safe_name).suffix.lower()
        if suffix in {".txt", ".md", ".csv", ".log", ".pdf", ".docx", ".odt", ".xlsx", ".pptx"} and not project_id:
            raise ValueError("Projektdateien müssen einem Projekt zugeordnet werden.")
        if project_id:
            with session_scope() as s:
                project = s.get(Project, project_id)
                if project is None or project.deleted_at is not None:
                    raise KeyError(project_id)
        parsed_start = None
        if start_at:
            parsed_start = parse_utc_datetime(start_at)
        upload_id = os.urandom(16).hex()
        temp_dir = self.config.base_dir / "upload_tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            temp_dir.chmod(0o700)
        except OSError:
            pass
        temp_path = temp_dir / f"{upload_id}.part"
        with open(temp_path, "wb") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        try:
            with session_scope() as s:
                s.add(UploadSession(
                    id=upload_id, filename=safe_name, temp_path=str(temp_path),
                    total_size=int(total_size), received_size=0, title=title,
                    project_id=project_id, start_at=parsed_start,
                    settings_json=json.dumps(self._normalise_meeting_settings(settings), ensure_ascii=False),
                    content_type=content_type, status="uploading"))
                s.commit()
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return self.upload_status(upload_id)

    def upload_status(self, upload_id: str) -> dict:
        with session_scope() as s:
            row = s.get(UploadSession, upload_id)
            if row is None:
                raise KeyError(upload_id)
            return self._upload_payload(row)

    @staticmethod
    def _upload_payload(row: UploadSession) -> dict:
        return {
            "id": row.id, "filename": row.filename,
            "total_size": row.total_size, "received_size": row.received_size,
            "progress": (row.received_size / row.total_size) if row.total_size else 0.0,
            "status": row.status, "error": row.error,
            "project_id": row.project_id,
        }

    def _upload_temp_path(self, row: UploadSession) -> Path:
        """Resolve a resumable upload path and enforce its private root."""
        path = Path(row.temp_path).resolve()
        temp_root = (self.config.base_dir / "upload_tmp").resolve()
        if temp_root not in path.parents or path == temp_root:
            raise ValueError("Ungültiger temporärer Uploadpfad.")
        return path

    def list_uploads(self) -> list[dict]:
        with session_scope() as s:
            rows = s.scalars(select(UploadSession).where(
                UploadSession.status.in_(["uploading", "paused", "failed"])
            ).order_by(UploadSession.updated_at.desc())).all()
            return [self._upload_payload(row) for row in rows]

    def append_upload_chunk(self, upload_id: str, offset: int, content: bytes) -> dict:
        if offset < 0:
            raise ValueError("Ungültiger Upload-Versatz.")
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise ValueError("Upload-Chunk muss binär sein.")
        if len(content) > int(self.config.max_upload_chunk_bytes):
            raise ValueError("Upload-Chunk ist zu groß.")
        with self._upload_io_guard:
            with session_scope() as s:
                row = s.get(UploadSession, upload_id)
                if row is None:
                    raise KeyError(upload_id)
                if row.status not in ("uploading", "paused"):
                    raise ValueError("Dieser Upload ist nicht mehr fortsetzbar.")
                if int(offset) != int(row.received_size):
                    raise ValueError(
                        f"Falscher Upload-Versatz: erwartet {row.received_size}, erhalten {offset}.")
                if row.received_size + len(content) > row.total_size:
                    raise ValueError("Der Upload würde die angekündigte Dateigröße überschreiten.")
                path = self._upload_temp_path(row)
                expected_size = int(row.received_size)
                try:
                    if path.stat().st_size != expected_size:
                        raise ValueError("Die temporäre Uploaddatei stimmt nicht mit dem gespeicherten Fortschritt überein.")
                    with open(path, "ab") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    row.received_size = expected_size + len(content)
                    row.status = "uploading"
                    row.error = None
                    s.commit()
                    return self._upload_payload(row)
                except Exception:
                    # The file write precedes the metadata commit. Roll it
                    # back on a DB error so a retry cannot append the same
                    # chunk twice and silently corrupt the upload.
                    try:
                        with open(path, "r+b") as handle:
                            handle.truncate(expected_size)
                            handle.flush()
                            os.fsync(handle.fileno())
                    except OSError:
                        log.warning("upload_chunk_rollback_failed upload=%s", upload_id[:8],
                                    exc_info=True)
                    raise

    def pause_upload(self, upload_id: str) -> dict:
        with self._upload_io_guard, session_scope() as s:
            row = s.get(UploadSession, upload_id)
            if row is None:
                raise KeyError(upload_id)
            if row.status == "uploading":
                row.status = "paused"
            s.commit()
            return self._upload_payload(row)

    def resume_upload(self, upload_id: str) -> dict:
        with self._upload_io_guard, session_scope() as s:
            row = s.get(UploadSession, upload_id)
            if row is None:
                raise KeyError(upload_id)
            # ``uploading`` is also resumable: a browser or core crash can
            # leave the durable session in that state before the next chunk.
            if row.status not in ("uploading", "paused", "failed"):
                raise ValueError("Dieser Upload ist nicht pausiert.")
            row.status = "uploading"
            row.error = None
            s.commit()
            return self._upload_payload(row)

    def cancel_upload(self, upload_id: str) -> dict:
        with self._upload_io_guard, session_scope() as s:
            row = s.get(UploadSession, upload_id)
            if row is None:
                raise KeyError(upload_id)
            path = self._upload_temp_path(row)
            row.status = "cancelled"
            s.commit()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"id": upload_id, "status": "cancelled"}

    def complete_upload(self, upload_id: str) -> dict:
        # Serialise the check-and-import so a concurrent double-completion can
        # never import the upload twice (which would create two meetings).
        with self._upload_io_guard, self._upload_complete_guard:
            with session_scope() as s:
                row = s.get(UploadSession, upload_id)
                if row is None:
                    raise KeyError(upload_id)
                if row.status not in ("uploading", "failed"):
                    if row.status == "completed":
                        return self._upload_payload(row)
                    raise ValueError("Dieser Upload kann nicht abgeschlossen werden.")
                if row.received_size != row.total_size:
                    raise ValueError("Der Upload ist noch nicht vollständig.")
                path = self._upload_temp_path(row)
                if not path.is_file() or path.stat().st_size != row.total_size:
                    raise ValueError("Die temporäre Uploaddatei ist unvollständig.")
                try:
                    stored_settings = json.loads(row.settings_json or "{}")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("Die Upload-Einstellungen sind beschädigt.") from exc
                if not isinstance(stored_settings, dict):
                    raise ValueError("Die Upload-Einstellungen sind beschädigt.")
                metadata = {
                    "filename": row.filename, "title": row.title,
                    "project_id": row.project_id,
                    "settings": stored_settings,
                    "content_type": row.content_type,
                    "start_at": utc_iso(row.start_at),
                }
            return self._import_completed_upload(upload_id, metadata, path)

    def _import_completed_upload(self, upload_id: str, metadata: dict, path: Path) -> dict:
        try:
            result = self.import_upload(metadata["filename"], None,
                                        title=metadata["title"],
                                        project_id=metadata["project_id"],
                                        settings=metadata["settings"],
                                        content_type=metadata["content_type"],
                                        start_at=metadata["start_at"], object_id=upload_id,
                                        _source_path=path)
        except Exception as exc:
            with session_scope() as s:
                row = s.get(UploadSession, upload_id)
                if row is not None:
                    row.status = "failed"
                    row.error = str(exc)
                    s.commit()
            raise
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        with session_scope() as s:
            row = s.get(UploadSession, upload_id)
            if row is not None:
                row.status = "completed"
                row.error = None
                s.commit()
                status = self._upload_payload(row)
            else:
                status = {"id": upload_id, "status": "completed"}
        status["result"] = result
        return status

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        tmp = path.with_name(path.name + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        with open(tmp, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _atomic_copy(source: Path, path: Path) -> None:
        """Atomically copy a resumable upload without buffering it in RAM."""
        tmp = path.with_name(path.name + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        try:
            with source.open("rb") as src, tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, path)
            path.chmod(0o600)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
