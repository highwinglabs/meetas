"""Non-destructive consistency checks for the local meeting workspace.

The checks deliberately do not repair anything and do not create indexes. They
combine SQLite integrity with application-level invariants so a healthy SQLite
file cannot hide broken references, missing recordings or stale search rows.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from core.audio.assembly import verify_original
from core.config import Config
from core.store.models import (
    Analysis, Meeting, ProcessingJob, Project, ProjectDocumentChunk, ProjectFile,
    Recording, Revision, SegmentEmbedding, Task, TranscriptSegment,
    TranscriptVersion, utcnow,
)


MEETING_STATUSES = {"recording", "paused", "ready", "processing", "transcribing",
                    "analyzing", "done", "failed", "archived"}
JOB_STAGES = {"capture", "transcribe", "diarize", "embed", "analyze"}
JOB_STATUSES = {"pending", "running", "done", "failed", "cancelled"}
SEGMENT_STATUSES = {"vorlaeufig", "bestaetigt", "manuell"}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _count(session: Session, query: str, params: dict | None = None) -> int:
    return int(session.execute(text(query), params or {}).scalar() or 0)


def _issue(issues: list[dict], severity: str, code: str, message: str,
           count: int | None = None) -> None:
    item = {"severity": severity, "code": code, "message": message}
    if count is not None:
        item["count"] = count
    issues.append(item)


def _valid_progress(value: object) -> bool:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf")) and 0 <= number <= 1


def _valid_segment(segment: TranscriptSegment) -> bool:
    try:
        start_s = float(segment.start_s)
        end_s = float(segment.end_s)
    except (TypeError, ValueError):
        return False
    return (math.isfinite(start_s) and math.isfinite(end_s)
            and start_s >= 0 and end_s >= start_s
            and segment.status in SEGMENT_STATUSES)


def run_integrity_check(config: Config, session: Session) -> dict:
    """Return a read-only report for the database and workspace files."""
    issues: list[dict] = []
    checks: list[dict] = []

    # SQLite's physical checks are necessary but not sufficient.
    try:
        row = session.execute(text("PRAGMA integrity_check")).first()
        sqlite_result = str(row[0]) if row else "unknown"
    except Exception as exc:  # pragma: no cover - SQLite itself is unavailable
        sqlite_result = f"error: {exc}"
    checks.append({"name": "SQLite-Struktur", "status": "ok" if sqlite_result == "ok" else "error",
                   "details": sqlite_result})
    if sqlite_result != "ok":
        _issue(issues, "error", "sqlite_integrity", "Die SQLite-Struktur ist beschädigt.")

    try:
        fk_rows = session.execute(text("PRAGMA foreign_key_check")).all()
    except Exception:
        fk_rows = []
        _issue(issues, "error", "foreign_key_check_failed", "Fremdschlüsselprüfung konnte nicht ausgeführt werden.")
    checks.append({"name": "Fremdschlüssel", "status": "ok" if not fk_rows else "error",
                   "details": f"{len(fk_rows)} Verstöße"})
    if fk_rows:
        _issue(issues, "error", "foreign_keys", "Es gibt verwaiste Fremdschlüsselbeziehungen.", len(fk_rows))

    # Application-level orphan checks. These remain useful even when SQLite's
    # foreign_keys pragma was disabled by an external maintenance tool.
    orphan_checks = [
        ("orphan_segments", "transcript_segment", "Transcript-Segmente ohne Meeting"),
        ("orphan_jobs", "processing_job", "Verarbeitungsjobs ohne Meeting"),
        ("orphan_analyses", "analysis", "Analysen ohne Meeting"),
        ("orphan_versions", "transcript_version", "Transkriptversionen ohne Meeting"),
        ("orphan_revisions", "revision", "Revisionen ohne Meeting"),
        ("orphan_embeddings", "segment_embedding", "Embeddings ohne Meeting"),
        ("orphan_tasks", "task", "Aufgaben mit nicht vorhandenem Meeting/Projekt"),
        ("orphan_project_files", "project_file", "Projektdateien mit unbekanntem Projekt"),
        ("orphan_document_chunks", "project_document_chunk", "Dokumentabschnitte ohne Datei/Projekt"),
    ]
    orphan_queries = {
        "orphan_segments": "SELECT COUNT(*) FROM transcript_segment s LEFT JOIN meeting m ON m.id=s.meeting_id WHERE m.id IS NULL",
        "orphan_jobs": "SELECT COUNT(*) FROM processing_job j LEFT JOIN meeting m ON m.id=j.meeting_id WHERE m.id IS NULL",
        "orphan_analyses": "SELECT COUNT(*) FROM analysis a LEFT JOIN meeting m ON m.id=a.meeting_id WHERE m.id IS NULL",
        "orphan_versions": "SELECT COUNT(*) FROM transcript_version v LEFT JOIN meeting m ON m.id=v.meeting_id WHERE m.id IS NULL",
        "orphan_revisions": "SELECT COUNT(*) FROM revision r LEFT JOIN meeting m ON m.id=r.meeting_id WHERE m.id IS NULL",
        "orphan_embeddings": "SELECT COUNT(*) FROM segment_embedding e LEFT JOIN meeting m ON m.id=e.meeting_id WHERE m.id IS NULL",
        "orphan_tasks": "SELECT COUNT(*) FROM task t LEFT JOIN meeting m ON m.id=t.meeting_id LEFT JOIN project p ON p.id=t.project_id WHERE (t.meeting_id IS NOT NULL AND m.id IS NULL) OR (t.project_id IS NOT NULL AND p.id IS NULL)",
        "orphan_project_files": "SELECT COUNT(*) FROM project_file f LEFT JOIN project p ON p.id=f.project_id WHERE f.project_id IS NOT NULL AND p.id IS NULL",
        "orphan_project_documents": "SELECT COUNT(*) FROM project_file f LEFT JOIN project p ON p.id=f.project_id WHERE f.kind='document' AND (f.project_id IS NULL OR p.id IS NULL)",
        "orphan_document_chunks": "SELECT COUNT(*) FROM project_document_chunk c LEFT JOIN project_file f ON f.id=c.project_file_id LEFT JOIN project p ON p.id=c.project_id WHERE f.id IS NULL OR p.id IS NULL",
    }
    orphan_checks.append(("orphan_project_documents", "project_file", "Dokumente ohne Projekt"))
    table_names = set(inspect(session.bind).get_table_names())
    for code, table, message in orphan_checks:
        if table not in table_names:
            continue
        n = _count(session, orphan_queries[code])
        checks.append({"name": message, "status": "ok" if n == 0 else "error", "details": str(n)})
        if n:
            _issue(issues, "error", code, message + ".", n)

    # Status and temporal invariants.
    meetings = session.scalars(select(Meeting)).all()
    bad_meetings = [m for m in meetings if m.status not in MEETING_STATUSES]
    if bad_meetings:
        _issue(issues, "error", "invalid_meeting_status", "Meetings enthalten unbekannte Statuswerte.", len(bad_meetings))
    jobs = session.scalars(select(ProcessingJob)).all()
    bad_jobs = [j for j in jobs if j.stage not in JOB_STAGES
                or j.status not in JOB_STATUSES
                or not _valid_progress(j.progress)]
    if bad_jobs:
        _issue(issues, "error", "invalid_job_state", "Verarbeitungsjobs enthalten ungültige Status-, Stufen- oder Fortschrittswerte.", len(bad_jobs))
    stale_cutoff = utcnow() - timedelta(hours=1)
    stale_jobs = [j for j in jobs if j.status == "running" and j.updated_at and
                  j.updated_at < stale_cutoff]
    if stale_jobs:
        _issue(issues, "warning", "stale_jobs", "Verarbeitungsjobs stehen ungewöhnlich lange auf ‚läuft‘.", len(stale_jobs))
    segments = session.scalars(select(TranscriptSegment)).all()
    bad_segments = [s for s in segments if not _valid_segment(s)]
    if bad_segments:
        _issue(issues, "error", "invalid_segment_timing", "Transkriptsegmente enthalten ungültige Zeitbereiche oder Statuswerte.", len(bad_segments))
    checks.append({"name": "Status und Zeitbereiche", "status": "error" if (bad_meetings or bad_jobs or bad_segments) else ("warning" if stale_jobs else "ok"),
                   "details": f"{len(meetings)} Meetings, {len(jobs)} Jobs, {len(segments)} Segmente"})

    # Version/source invariants: a source reference must remain resolvable.
    version_without_current = _count(session, "SELECT COUNT(*) FROM meeting m WHERE EXISTS (SELECT 1 FROM transcript_version v WHERE v.meeting_id=m.id) AND NOT EXISTS (SELECT 1 FROM transcript_version v WHERE v.meeting_id=m.id AND v.is_current=1)")
    multiple_current = _count(session, "SELECT COUNT(*) FROM (SELECT meeting_id FROM transcript_version WHERE is_current=1 GROUP BY meeting_id HAVING COUNT(*) > 1)")
    missing_revision_segments = _count(session, "SELECT COUNT(*) FROM revision r LEFT JOIN transcript_segment s ON s.id=r.segment_id WHERE s.id IS NULL")
    mismatched_revision_segments = _count(session, "SELECT COUNT(*) FROM revision r JOIN transcript_segment s ON s.id=r.segment_id WHERE r.meeting_id != s.meeting_id")
    missing_task_sources = _count(session, "SELECT COUNT(*) FROM task t LEFT JOIN transcript_segment s ON s.id=t.source_segment_id WHERE t.source_segment_id IS NOT NULL AND s.id IS NULL")
    mismatched_task_sources = _count(session, "SELECT COUNT(*) FROM task t JOIN transcript_segment s ON s.id=t.source_segment_id WHERE t.meeting_id IS NOT NULL AND t.meeting_id != s.meeting_id")
    missing_embedding_segments = _count(session, "SELECT COUNT(*) FROM segment_embedding e LEFT JOIN transcript_segment s ON s.id=e.seg_id WHERE s.id IS NULL")
    mismatched_embedding_segments = _count(session, "SELECT COUNT(*) FROM segment_embedding e JOIN transcript_segment s ON s.id=e.seg_id WHERE e.meeting_id != s.meeting_id")
    mismatched_document_chunks = _count(session, "SELECT COUNT(*) FROM project_document_chunk c JOIN project_file f ON f.id=c.project_file_id WHERE c.project_id != f.project_id") if "project_document_chunk" in table_names else 0
    if version_without_current or multiple_current:
        _issue(issues, "warning", "transcript_current_version", "Transkriptversionen haben keine eindeutige aktuelle Fassung.", version_without_current + multiple_current)
    if missing_revision_segments or mismatched_revision_segments:
        _issue(issues, "warning", "revision_source_missing", "Revisionen verweisen auf fehlende oder fremde Segmente.", missing_revision_segments + mismatched_revision_segments)
    if missing_task_sources or mismatched_task_sources:
        _issue(issues, "warning", "task_source_missing", "KI-Aufgaben verweisen auf fehlende oder fremde Quellsegmente.", missing_task_sources + mismatched_task_sources)
    if missing_embedding_segments or mismatched_embedding_segments:
        _issue(issues, "warning", "embedding_source_missing", "Embeddings verweisen auf fehlende oder fremde Quellsegmente.", missing_embedding_segments + mismatched_embedding_segments)
    if mismatched_document_chunks:
        _issue(issues, "error", "document_chunk_scope_mismatch", "Dokumentabschnitte gehören nicht zum Projekt ihrer Datei.", mismatched_document_chunks)
    embedding_rows = session.scalars(select(SegmentEmbedding)).all()
    malformed_embeddings = []
    for embedding in embedding_rows:
        try:
            malformed = (not embedding.dim or not embedding.vector
                         or len(embedding.vector) != int(embedding.dim) * 4)
            if not malformed:
                values = np.frombuffer(embedding.vector, dtype=np.float32)
                malformed = not bool(np.all(np.isfinite(values)))
        except (TypeError, ValueError):
            malformed = True
        if malformed:
            malformed_embeddings.append(embedding)
    if malformed_embeddings:
        _issue(issues, "warning", "embedding_blob_invalid",
               "Embeddings enthalten ungültige Vektordaten.", len(malformed_embeddings))
    checks.append({"name": "Versionen und Quellen", "status": "error" if mismatched_document_chunks else ("ok" if not (version_without_current or multiple_current or missing_revision_segments or mismatched_revision_segments or missing_task_sources or mismatched_task_sources or missing_embedding_segments or mismatched_embedding_segments or malformed_embeddings) else "warning"),
                   "details": "Quellverweise und aktuelle Version geprüft"})

    # FTS is a denormalised index and must have exactly one row per transcript
    # segment. Do not create it here; absence is a warning, not a mutation.
    if "transcript_fts" in table_names:
        try:
            fts_rows = _count(session, "SELECT COUNT(*) FROM transcript_fts")
            segment_count = len(segments)
            fts_missing = _count(session, "SELECT COUNT(*) FROM transcript_segment s LEFT JOIN transcript_fts f ON f.seg_id=s.id WHERE f.seg_id IS NULL")
            fts_orphan = _count(session, "SELECT COUNT(*) FROM transcript_fts f LEFT JOIN transcript_segment s ON s.id=f.seg_id WHERE s.id IS NULL")
            fts_mismatch = _count(session, "SELECT COUNT(*) FROM transcript_fts f JOIN transcript_segment s ON s.id=f.seg_id WHERE f.meeting_id != s.meeting_id")
            count_delta = abs(fts_rows - segment_count)
            bad_fts = bool(count_delta or fts_missing or fts_orphan or fts_mismatch)
            checks.append({"name": "Transkript-Suchindex", "status": "warning" if bad_fts else "ok",
                           "details": f"{fts_rows} Indexzeilen für {segment_count} Segmente"})
            if bad_fts:
                _issue(issues, "warning", "fts_out_of_sync", "Der Transkript-Suchindex entspricht nicht den Segmenten.", int(count_delta + fts_missing + fts_orphan + fts_mismatch))
        except Exception:
            _issue(issues, "warning", "fts_unreadable", "Der Transkript-Suchindex konnte nicht gelesen werden.")
    else:
        checks.append({"name": "Transkript-Suchindex", "status": "warning", "details": "Index nicht vorhanden"})
        _issue(issues, "warning", "fts_missing", "Der Transkript-Suchindex ist nicht vorhanden.")

    # Files are checked for containment and readability, never modified.
    audio_root = config.audio_dir.resolve()
    for recording in session.scalars(select(Recording)).all():
        if not recording.original_path:
            if recording.status == "assembled":
                _issue(issues, "error", "recording_path_missing", "Eine abgeschlossene Aufnahme hat keinen Originalpfad.")
            continue
        try:
            path = Path(recording.original_path).resolve()
        except (TypeError, ValueError, OSError):
            _issue(issues, "error", "recording_file_missing",
                   "Eine Aufnahme hat einen ungültigen Dateipfad.")
            continue
        if not _inside(path, audio_root) or not path.is_file():
            _issue(issues, "error", "recording_file_missing", "Eine Aufnahme fehlt oder liegt außerhalb des Audioverzeichnisses.")
            continue
        try:
            info = verify_original(path.parent, config)
            expected, actual = info["expected_duration_s"], info["actual_duration_s"]
            if not info["read_only"]:
                _issue(issues, "warning", "recording_not_read_only", "Eine abgeschlossene Originalaufnahme ist nicht schreibgeschützt.")
            if expected and abs(expected - actual) > max(0.1, expected * 0.02):
                _issue(issues, "warning", "recording_duration_mismatch", "Aufnahme- und Chunkdauer weichen auffällig ab.")
        except Exception:
            _issue(issues, "error", "recording_unreadable", "Eine Originalaufnahme ist nicht lesbar.")
    project_root = (config.base_dir / "project_files").resolve()
    for asset in session.scalars(select(ProjectFile)).all():
        try:
            path = Path(asset.path).resolve()
        except (TypeError, ValueError, OSError):
            _issue(issues, "error", "project_file_missing",
                   f"Gespeicherte Projektdatei hat einen ungültigen Pfad: {asset.original_name}.")
            continue
        expected_root = project_root if asset.kind == "document" else audio_root
        if not _inside(path, expected_root) or not path.is_file():
            _issue(issues, "error", "project_file_missing", f"Gespeicherte Datei fehlt oder liegt außerhalb des erwarteten Verzeichnisses: {asset.original_name}.")
        elif asset.kind == "document" and asset.deleted_at is None and asset.extraction_status == "ready" and not asset.chunks:
            _issue(issues, "error", "document_chunks_missing", "Eine als bereit markierte Projektdatei besitzt keine Textabschnitte.")
        elif asset.kind == "document" and asset.deleted_at is None and asset.extraction_status == "processing":
            _issue(issues, "warning", "document_extraction_running", f"Dokumentindexierung steht noch auf ‚läuft‘: {asset.original_name}.")
    checks.append({"name": "Audio- und Projektdateien", "status": "ok" if not any(i["code"] in {"recording_path_missing", "recording_file_missing", "recording_unreadable", "project_file_missing", "document_chunks_missing"} for i in issues) else "error",
                   "details": "Pfade, Lesbarkeit und Dokumentindex geprüft"})

    # Backup files are checked independently from the live database.
    backup_errors = 0
    for backup in config.base_dir.joinpath("backups").glob("meeting_assistant_*.db"):
        try:
            with sqlite3.connect(str(backup)) as conn:
                result = conn.execute("PRAGMA integrity_check").fetchone()
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            if (backup.stat().st_size == 0 or not result or result[0] != "ok"
                    or not {"meeting", "recording", "transcript_segment"}.issubset(tables)):
                backup_errors += 1
        except (OSError, sqlite3.Error):
            backup_errors += 1
    checks.append({"name": "Backups", "status": "ok" if backup_errors == 0 else "error",
                   "details": f"{backup_errors} unlesbare oder ungültige Dateien"})
    if backup_errors:
        _issue(issues, "error", "backup_invalid", "Mindestens ein lokales Backup ist leer, unlesbar oder beschädigt.", backup_errors)

    errors = sum(item["severity"] == "error" for item in issues)
    warnings = sum(item["severity"] == "warning" for item in issues)
    status = "error" if errors else ("warning" if warnings else "ok")
    return {
        "status": status,
        "generated_at": _iso_now(),
        "summary": {"errors": errors, "warnings": warnings, "checks": len(checks)},
        "checks": checks,
        "issues": issues,
        "repair_policy": "Keine automatische Reparatur. Änderungen erst nach ausdrücklicher Bestätigung und Backup.",
    }
