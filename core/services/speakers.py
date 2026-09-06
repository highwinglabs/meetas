"""Speaker diarization and speaker label management. (part of MeetingService)."""
from __future__ import annotations

import re
from sqlalchemy import func, select
from core.jobs.queue import JobQueue
from core.providers import ASRError
from core.store.db import session_scope
from core.store.fts import reindex_meeting
from core.transcribe.processor import pick_asr_audio
from core.store.models import Meeting, Recording, SpeakerLabel, TranscriptSegment
from core.logging_setup import get_logger
from core.services._common import SpeakerMergeConflictError, UnknownMeetingError

log = get_logger("ma.service")


class SpeakersMixin:

    def diarize(self, meeting_id: str) -> dict:
        """Assign a speaker label to every transcript segment (local, offline).

        Runs the diarization engine over the meeting audio and maps each segment
        to a speaker by temporal overlap. Idempotent: re-running simply recomputes
        the labels. Requires an existing transcript (run transcribe first)."""
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            rec = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
            n_seg = s.scalar(select(func.count()).select_from(TranscriptSegment)
                             .where(TranscriptSegment.meeting_id == meeting_id)) or 0
        if rec is None or not rec.original_path:
            raise ASRError("Keine Originalaufnahme vorhanden (Meeting nicht finalisiert?).")
        if n_seg == 0:
            raise ASRError("Kein Transkript vorhanden – bitte zuerst transkribieren.")

        audio_path = pick_asr_audio(rec.original_path)
        try:
            from faster_whisper.audio import decode_audio  # lazy heavy import
            # decode_audio resamples to 16 kHz mono and returns a 1-D float32 array.
            audio = decode_audio(str(audio_path))
            sr = 16000
        except Exception as exc:
            raise ASRError(f"Audio konnte nicht geladen werden: {exc}") from exc

        engine = self._diar_engine()
        spans = engine.diarize(audio, sr)

        with session_scope() as s:
            segs = s.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.meeting_id == meeting_id)
                .order_by(TranscriptSegment.start_s)).all()
            ids = engine.assign(segs, spans)
            for seg, sp in zip(segs, ids):
                seg.speaker_id = sp
            job = JobQueue.get_or_create(s, meeting_id, "diarize")
            JobQueue.mark_done(s, job)
            reindex_meeting(s, meeting_id)
            s.commit()

        distinct = {sp for sp in ids if sp}
        log.info("diarize_done meeting=%s segments=%s speakers=%s engine=%s",
                 meeting_id[:8], len(segs), len(distinct), engine.name)
        return {
            "meeting_id": meeting_id, "status": "done", "segments": len(segs),
            "speakers": len(distinct), "spans": len(spans), "engine": engine.name,
        }

    # --- Phase 4: speaker profiles / rename (per meeting) ---
    def list_speakers(self, meeting_id: str) -> list[dict]:
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            rows = s.execute(
                select(TranscriptSegment.speaker_id, func.count())
                .where(TranscriptSegment.meeting_id == meeting_id)
                .group_by(TranscriptSegment.speaker_id)
            ).all()
            # A profile row maps the diarizer's id -> human label. After an
            # in-place rename the segment carries the display name, so resolve
            # by either key.
            labels: dict[str, str] = {}
            for l in s.scalars(select(SpeakerLabel).where(
                    SpeakerLabel.meeting_id == meeting_id)).all():
                labels[l.speaker_id] = l.label
                labels[l.label] = l.label
            out = []
            for spk, n in rows:
                if not spk:
                    continue
                out.append({"speaker_id": spk, "label": labels.get(spk),
                            "segments": n})
            out.sort(key=lambda d: d["speaker_id"])
            return out

    def rename_speaker(self, meeting_id: str, current: str, new_name: str,
                       confirm_merge: bool = False) -> dict:
        """Rename a diarized speaker for this meeting.

        Renames ``current`` -> ``new_name`` on every segment of the meeting (so
        exports/FTS/analysis reflect the new name) and records the mapping as a
        per-meeting speaker profile. Idempotent and offline.

        If ``new_name`` already labels a different speaker, the rename would
        silently merge the two speakers. That requires ``confirm_merge=True``;
        otherwise a SpeakerMergeConflictError (HTTP 409) is raised (L13)."""
        current = (current or "").strip()
        new_name = (new_name or "").strip()
        if not current or not new_name:
            raise ValueError("'current' und 'new_name' dürfen nicht leer sein.")
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            if meeting is None or meeting.deleted_at is not None:
                raise UnknownMeetingError(meeting_id)
            # L13: refuse to merge two existing speakers without explicit
            # confirmation. A merge happens when some other segment already
            # carries the target name (current == new_name is a no-op, not a
            # merge, so it is excluded by the != current filter).
            n_target = s.scalar(select(func.count()).select_from(TranscriptSegment)
                                .where(TranscriptSegment.meeting_id == meeting_id,
                                       TranscriptSegment.speaker_id == new_name,
                                       TranscriptSegment.speaker_id != current)) or 0
            if n_target > 0 and not confirm_merge:
                raise SpeakerMergeConflictError(current, new_name, n_target)
            segs = s.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.meeting_id == meeting_id,
                       TranscriptSegment.speaker_id == current)
            ).all()
            for seg in segs:
                seg.speaker_id = new_name
            lab = s.scalar(select(SpeakerLabel).where(
                SpeakerLabel.meeting_id == meeting_id,
                SpeakerLabel.speaker_id == current))
            if lab is None:
                lab = SpeakerLabel(meeting_id=meeting_id, speaker_id=current)
                s.add(lab)
            lab.label = new_name
            reindex_meeting(s, meeting_id)
            s.commit()
            return {"meeting_id": meeting_id, "from": current, "to": new_name,
                    "updated_segments": len(segs)}
