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


# --- F5: start_transcription admission race ---------------------------------

def _wait_job_terminal(mid: str, stage: str = "transcribe", timeout: float = 30.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        with session_scope() as s:
            job = s.scalar(select(ProcessingJob).where(
                ProcessingJob.meeting_id == mid,
                ProcessingJob.stage == stage))
        if job is not None and job.status in ("done", "failed", "cancelled"):
            return job.status
        time.sleep(0.05)
    raise AssertionError(f"job {stage} for {mid[:8]} never reached a terminal state")


def test_start_transcription_concurrent_calls_single_run(config, finalize_meeting):
    """F5: concurrent start_transcription calls must schedule exactly one ASR
    run (atomic slot claim) and produce exactly one 'pending' ack."""
    import threading

    svc, mid = finalize_meeting(title="Race")

    submitted = []
    sub_lock = threading.Lock()
    real_submit = svc._pipeline_executor.submit

    def counting_submit(fn, *args, **kwargs):
        with sub_lock:
            submitted.append(1)
        return real_submit(fn, *args, **kwargs)

    svc._pipeline_executor.submit = counting_submit

    calls = []
    call_lock = threading.Lock()
    real_transcribe = svc.transcribe

    def counting_transcribe(*a, **k):
        with call_lock:
            calls.append(1)
        return real_transcribe(*a, **k)

    svc.transcribe = counting_transcribe

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    acks = []
    ack_lock = threading.Lock()

    def worker():
        barrier.wait()
        ack = svc.start_transcription(mid)
        with ack_lock:
            acks.append(ack)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in range(n_threads):
        threads[t].join(timeout=30)

    assert len(submitted) == 1, f"expected exactly one scheduled run, got {len(submitted)}"
    assert len(calls) == 1
    assert len(acks) == n_threads
    assert sum(1 for a in acks if a["status"] == "pending") == 1
    assert all(a["status"] in ("pending", "running") for a in acks)
    assert _wait_job_terminal(mid) == "done"


def test_start_transcription_releases_slot_on_unknown_meeting(config, finalize_meeting):
    """F5: a failed admission (unknown meeting) must release the in-memory
    slot, otherwise that id could never be transcribed again."""
    import pytest
    from core.services._common import UnknownMeetingError

    svc, _mid = finalize_meeting(title="Slot")
    missing = "no-such-meeting-id"
    with pytest.raises(UnknownMeetingError):
        svc.start_transcription(missing)
    assert missing not in svc._manual_transcriptions


def test_start_transcription_submit_failure_fails_job_and_releases_slot(
        config, finalize_meeting):
    """F5: an executor submit failure must persist the failure and release the
    slot; the meeting must be transcribable again afterwards."""
    import pytest

    svc, mid = finalize_meeting(title="SubmitFail")
    real_submit = svc._pipeline_executor.submit

    def boom(fn, *a, **k):
        raise RuntimeError("executor down")

    svc._pipeline_executor.submit = boom
    with pytest.raises(RuntimeError):
        svc.start_transcription(mid)
    assert mid not in svc._manual_transcriptions
    with session_scope() as s:
        job = s.scalar(select(ProcessingJob).where(
            ProcessingJob.meeting_id == mid,
            ProcessingJob.stage == "transcribe"))
        assert job is not None and job.status == "failed"
        assert s.get(Meeting, mid).status == "failed"

    # Retry works: slot was released and the job is retryable.
    svc._pipeline_executor.submit = real_submit
    ack = svc.start_transcription(mid)
    assert ack["status"] == "pending"
    assert _wait_job_terminal(mid) == "done"
