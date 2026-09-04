"""Transcription processor.

Runs a (local) ASR engine over the assembled original audio and writes the
resulting timestamped segments into the database, then syncs the FTS index.
The operation is resumable/idempotent: it resets the `transcribe` job, clears
any prior segments for the meeting, then re-inserts. A crash mid-run is
recovered on startup (job reset to pending) and simply re-runs.
"""
from __future__ import annotations

from pathlib import Path
import json
import time
import wave
import math

from sqlalchemy import delete, select

from core.config import Config
from core.jobs.queue import JobQueue
from core.logging_setup import get_logger
from core.providers.base import ASREngine, ASRError, ASRSegment
from core.store.db import session_scope
from core.store.fts import reindex_meeting
from core.store.models import Meeting, Recording, TranscriptSegment, TranscriptVersion, new_id, utcnow

log = get_logger("ma.transcribe")

ASR_COPY_NAME = "original_16k.wav"


def pick_asr_audio(recording_original_path: str) -> Path:
    """Prefer the 16 kHz ASR copy if present, else the high-fidelity original."""
    original = Path(recording_original_path)
    copy = original.parent / ASR_COPY_NAME
    return copy if copy.is_file() else original


class TranscriptionProcessor:
    def __init__(self, config: Config):
        self.config = config

    def process(self, meeting_id: str, engine: ASREngine,
                language: str | None = None) -> dict:
        # Resolve the audio to transcribe from the recording.
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None:
                raise ASRError(f"Meeting nicht gefunden: {meeting_id}")
            rec = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
            if rec is None or not rec.original_path:
                raise ASRError("Keine Originalaufnahme vorhanden (Meeting nicht finalisiert?).")
            audio_path = pick_asr_audio(rec.original_path)

        audio_duration = None
        try:
            with wave.open(str(audio_path), "rb") as wav:
                audio_duration = wav.getnframes() / max(1, wav.getframerate())
        except (OSError, wave.Error):
            pass

        # Mark the job as running, but keep the previous transcript visible
        # until the new ASR run has completed successfully.
        with session_scope() as s:
            job = JobQueue.get_or_create(s, meeting_id, "transcribe")
            job.status = "running"
            job.progress = 0.0
            job.error = None
            m = s.get(Meeting, meeting_id)
            m.status = "transcribing"
            s.commit()

        try:
            log.info("transcribe_start meeting=%s model=%s lang=%s audio=%s",
                     meeting_id[:8], engine.model_name, language or "auto", audio_path)
            last_progress = 0.0
            last_update = 0.0

            def report_segment(segment: ASRSegment) -> None:
                nonlocal last_progress, last_update
                if not audio_duration or audio_duration <= 0:
                    return
                progress = min(0.98, max(0.01, float(segment.end_s) / audio_duration))
                now = time.monotonic()
                if progress - last_progress < 0.01 and now - last_update < 0.75:
                    return
                try:
                    with session_scope() as s:
                        job = JobQueue.get_or_create(s, meeting_id, "transcribe")
                        job.progress = progress
                        s.commit()
                    last_progress = progress
                    last_update = now
                except Exception:  # progress must never break transcription
                    log.debug("transcribe_progress_update_failed meeting=%s", meeting_id[:8], exc_info=True)

            segments = engine.transcribe_with_progress(
                audio_path, language=language, on_segment=report_segment)
            if not segments:
                raise ASRError(
                    "Die Transkription hat keinen Text erkannt. "
                    "Das bisherige Transkript wurde nicht verändert.")
            # Treat provider output as an integrity boundary. Invalid timing or
            # non-text values must never reach the database/FTS index.
            for segment in segments:
                try:
                    start_s = float(segment.start_s)
                    end_s = float(segment.end_s)
                except (TypeError, ValueError) as exc:
                    raise ASRError("ASR lieferte ungültige Zeitdaten.") from exc
                if (not math.isfinite(start_s) or not math.isfinite(end_s)
                        or start_s < 0 or end_s < start_s
                        or not isinstance(segment.text, str)
                        or not segment.text.strip()):
                    raise ASRError("ASR lieferte ein ungültiges Transkriptsegment.")
            detected = segments[0].language if segments else (language or None)
            audio_ref = str(audio_path)

            with session_scope() as s:
                previous = s.scalars(
                    select(TranscriptSegment)
                    .where(TranscriptSegment.meeting_id == meeting_id)
                    .order_by(TranscriptSegment.start_s)
                ).all()
                previous_version = s.scalar(
                    select(TranscriptVersion)
                    .where(TranscriptVersion.meeting_id == meeting_id)
                    .order_by(TranscriptVersion.version_no.desc())
                )
                next_version = (previous_version.version_no + 1) if previous_version else 1
                # A previous completed run already contains its immutable
                # snapshot. Only legacy/live segments without a version row
                # need an extra preservation snapshot before replacement.
                if previous and previous_version is None:
                    previous_payload = self._segment_payload(previous)
                    s.add(TranscriptVersion(
                        id=new_id(), meeting_id=meeting_id, version_no=next_version,
                        model=previous_version.model if previous_version else "unbekannt",
                        language=previous_version.language if previous_version else None,
                        segments_json=json.dumps(previous_payload, ensure_ascii=False),
                        is_current=False, created_at=utcnow(),
                    ))
                    next_version += 1
                s.execute(delete(TranscriptSegment)
                          .where(TranscriptSegment.meeting_id == meeting_id))
                # Assign explicit segment ids up front and store them in the
                # version snapshot, so a later restore re-creates the segments
                # under the SAME ids -- keeping Revision/Task/Analysis refs intact.
                snapshot = []
                for seg in segments:
                    seg_id = new_id()
                    s.add(TranscriptSegment(
                        id=seg_id, meeting_id=meeting_id,
                        start_s=seg.start_s, end_s=seg.end_s,
                        text=seg.text, raw_text=seg.raw_text or seg.text,
                        language=detected, confidence=seg.confidence,
                        audio_ref=audio_ref, status="vorlaeufig"))
                    snapshot.append({
                        "id": seg_id, "start_s": seg.start_s,
                        "end_s": seg.end_s, "text": seg.text,
                        "raw_text": seg.raw_text or seg.text,
                        "language": seg.language, "confidence": seg.confidence,
                        "speaker_id": getattr(seg, "speaker_id", None)})
                s.query(TranscriptVersion).filter(
                    TranscriptVersion.meeting_id == meeting_id).update(
                        {TranscriptVersion.is_current: False}, synchronize_session=False)
                s.add(TranscriptVersion(
                    id=new_id(), meeting_id=meeting_id, version_no=next_version,
                    model=engine.model_name, language=detected,
                    segments_json=json.dumps(snapshot, ensure_ascii=False),
                    is_current=True, created_at=utcnow(),
                ))
                n = reindex_meeting(s, meeting_id)
                s.commit()

            with session_scope() as s:
                job = JobQueue.get_or_create(s, meeting_id, "transcribe")
                JobQueue.mark_done(s, job)
                m = s.get(Meeting, meeting_id)
                m.status = "done"
                if detected:
                    m.lang = detected
                s.commit()

            log.info("transcribe_done meeting=%s segments=%s fts=%s lang=%s",
                     meeting_id[:8], len(segments), n, detected)
            return {
                "meeting_id": meeting_id, "status": "done",
                "segments": len(segments), "fts_rows": n, "language": detected,
                "model": engine.model_name, "audio": audio_ref,
                "audio_duration_s": audio_duration,
            }
        except Exception as exc:  # record failure, re-raise for API/CLI mapping
            with session_scope() as s:
                job = JobQueue.get_or_create(s, meeting_id, "transcribe")
                JobQueue.mark_failed(s, job, str(exc))
                m = s.get(Meeting, meeting_id)
                if m is not None:
                    m.status = "failed"
                s.commit()
            log.error("transcribe_failed meeting=%s: %s", meeting_id[:8], exc)
            raise

    @staticmethod
    def _segment_payload(segments: list[TranscriptSegment]) -> list[dict]:
        return [
            {"id": seg.id, "start_s": seg.start_s, "end_s": seg.end_s,
             "text": seg.text, "raw_text": seg.raw_text,
             "language": seg.language, "confidence": seg.confidence,
             "speaker_id": seg.speaker_id, "status": seg.status}
            for seg in segments
        ]
