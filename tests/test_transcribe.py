"""Transcription pipeline: audio -> segments in DB -> FTS, resumable/idempotent."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from core.providers.base import ASRError, ASRSegment
from core.store.db import session_scope
from core.store.models import Meeting, ProcessingJob, TranscriptSegment, TranscriptVersion
from tests.conftest import MockASREngine


def test_transcribe_writes_segments_and_fts(config, finalize_meeting):
    svc, mid = finalize_meeting()
    out = svc.transcribe(mid, language="de")

    assert out["status"] == "done"
    assert out["segments"] == 2

    with session_scope() as s:
        segs = s.scalars(select(TranscriptSegment)
                         .where(TranscriptSegment.meeting_id == mid)).all()
        assert len(segs) == 2
        assert all(x.audio_ref for x in segs)
        m = s.get(Meeting, mid)
        assert m.status == "done"
        assert m.lang == "de"
        job = s.scalar(select(ProcessingJob).where(
            ProcessingJob.meeting_id == mid, ProcessingJob.stage == "transcribe"))
        assert job.status == "done"

    # FTS is searchable
    results = svc.search("Budget")
    assert len(results) >= 1
    assert any("Q2-Budget" in (r["text"] or "") for r in results)


def test_transcribe_stores_per_segment_language(config, finalize_meeting):
    # Regression (L5): code-switched segments keep their own language instead
    # of all inheriting the first-segment (detected) language.
    engine = MockASREngine(segments=[
        ASRSegment(0.0, 1.2, "Hallo, alle zusammen.", "de", 0.9),
        ASRSegment(1.5, 3.0, "Let's talk budget.", "en", 0.8),
    ])
    svc, mid = finalize_meeting(asr_engine=engine)
    out = svc.transcribe(mid)
    assert out["status"] == "done"
    assert out["language"] == "de"  # detected = first segment's language

    with session_scope() as s:
        segs = sorted(
            s.scalars(select(TranscriptSegment)
                      .where(TranscriptSegment.meeting_id == mid)).all(),
            key=lambda x: x.start_s)
        assert [x.language for x in segs] == ["de", "en"]
        m = s.get(Meeting, mid)
        assert m.lang == "de"


def test_transcribe_is_idempotent(config, finalize_meeting):
    svc, mid = finalize_meeting()
    svc.transcribe(mid)
    svc.transcribe(mid)  # re-run must not duplicate
    with session_scope() as s:
        segs = s.scalars(select(TranscriptSegment)
                         .where(TranscriptSegment.meeting_id == mid)).all()
        versions = s.scalars(select(TranscriptVersion).where(
            TranscriptVersion.meeting_id == mid).order_by(TranscriptVersion.version_no)).all()
    assert len(segs) == 2
    assert len(versions) == 2
    assert versions[-1].is_current is True
    assert 'Q2-Budget' in versions[0].segments_json


def test_transcribe_failure_sets_failed(config, make_service):
    engine = MockASREngine(fail=True)
    svc = make_service(asr_engine=engine)
    mid = svc.start_meeting(title="fail", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)

    with pytest.raises(ASRError):
        svc.transcribe(mid)

    with session_scope() as s:
        m = s.get(Meeting, mid)
        job = s.scalar(select(ProcessingJob).where(
            ProcessingJob.meeting_id == mid, ProcessingJob.stage == "transcribe"))
        assert m.status == "failed"
        assert job.status == "failed"
        assert job.error


def test_empty_asr_result_keeps_previous_transcript(config, finalize_meeting):
    svc, mid = finalize_meeting()
    svc.transcribe(mid)
    with session_scope() as s:
        before = [row.text for row in s.scalars(select(TranscriptSegment).where(
            TranscriptSegment.meeting_id == mid).order_by(TranscriptSegment.start_s))]

    class EmptyASR(MockASREngine):
        def transcribe(self, audio_path, language=None):
            return []

    svc._asr_override = EmptyASR()
    with pytest.raises(ASRError, match="bisherige Transkript"):
        svc.transcribe(mid)
    with session_scope() as s:
        after = [row.text for row in s.scalars(select(TranscriptSegment).where(
            TranscriptSegment.meeting_id == mid).order_by(TranscriptSegment.start_s))]
    assert after == before


def test_transcribe_without_original_fails(config, make_service):
    svc = make_service(asr_engine=MockASREngine())
    with session_scope() as s:
        m = Meeting(title="no audio", status="ready")
        s.add(m)
        s.commit()
        mid = m.id
    with pytest.raises(ASRError):
        svc.transcribe(mid)


def test_asr_prefers_16k_copy(config, make_service):
    """If original_16k.wav exists, it is transcribed (not the high-fidelity file)."""
    svc, mid = _finalize(config, make_service)
    from core.transcribe.processor import pick_asr_audio
    with session_scope() as s:
        from sqlalchemy import select as sel
        from core.store.models import Recording
        rec = s.scalar(sel(Recording).where(Recording.meeting_id == mid))
        # Precondition: assembly only writes the 16k copy when the capture rate
        # differs from 16 kHz (headless CI captures at the 16 kHz default). Create
        # it explicitly so the test stays deterministic on any host.
        copy = Path(rec.original_path).parent / "original_16k.wav"
        if not copy.is_file():
            copy.write_bytes(Path(rec.original_path).read_bytes())
        audio = pick_asr_audio(rec.original_path)
    assert audio.name == "original_16k.wav"


def _finalize(config, make_service):
    engine = MockASREngine()
    svc = make_service(asr_engine=engine)
    mid = svc.start_meeting(title="t", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    return svc, mid


class _RecordingASREngine(MockASREngine):
    """Records the language each transcribe() call received."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen_languages: list[str | None] = []

    def transcribe(self, audio_path, language: str | None = None):
        self.seen_languages.append(language)
        return super().transcribe(audio_path, language)


def test_transcribe_honours_per_meeting_language(config, make_service):
    """A manual (re)transcribe without an explicit language must honour the
    per-meeting choice made at recording start, not silently auto-detect."""
    engine = _RecordingASREngine()
    svc = make_service(asr_engine=engine)
    mid = svc.start_meeting(title="t", source="mic", settings={"language": "en"})
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)

    out = svc.transcribe(mid)  # no explicit language
    assert out["status"] == "done"
    assert engine.seen_languages[-1] == "en"

    # An explicit request still wins over the stored per-meeting choice.
    svc.transcribe(mid, language="de")
    assert engine.seen_languages[-1] == "de"


def test_transcribe_auto_when_no_per_meeting_language(config, make_service):
    """A meeting whose stored language is "auto" keeps auto-detection (None)."""
    engine = _RecordingASREngine()
    svc = make_service(asr_engine=engine)
    mid = svc.start_meeting(title="t", source="mic", settings={"language": "auto"})
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    svc.transcribe(mid)
    assert engine.seen_languages[-1] is None
