"""Phase 5 service integration: live transcription pipeline + speaker
diarization. All engines are deterministic mocks; the capture source is
synthetic. Verifies the opt-in wiring (default batch behaviour is untouched)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from core.providers.base import ASRError
from core.providers.diar import DiarSpan, MockDiarizationEngine
from core.store.db import session_scope
from core.store.models import ProcessingJob, TranscriptSegment

from conftest import MockASREngine, MockLiveASREngine


def _two_speaker_diar():
    return MockDiarizationEngine([
        DiarSpan(0.0, 3.0, "Sprecher 1"),
        DiarSpan(3.0, 6.0, "Sprecher 2"),
    ])


def test_live_meeting_finalizes_segments_with_speakers(config, make_service):
    config.live_transcription = True
    config.live_period_s = 0.1
    config.live_tail_s = 2.0
    config.live_window_s = 60.0
    config.speaker_diarization = True

    svc = make_service(duration_s=6, asr_engine=MockASREngine(),
                       live_engine=MockLiveASREngine(), diar_engine=_two_speaker_diar())
    mid = svc.start_meeting(title="live", source="mic",
                            settings={"speaker_mode": "live"})

    # While recording, the status exposes the live snapshot.
    st = svc.get_status(mid)
    assert st["status"] == "recording"
    assert st.get("live", {}).get("enabled") is True

    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)

    with session_scope() as s:
        segs = s.scalars(select(TranscriptSegment)
                         .where(TranscriptSegment.meeting_id == mid)).all()
    assert len(segs) >= 1
    speakers = {seg.speaker_id for seg in segs}
    assert "Sprecher 1" in speakers
    assert "Sprecher 2" in speakers
    # the live pipeline is torn down on stop
    assert mid not in svc._live_pipelines


def test_live_disabled_by_default(config, make_service):
    # Default config: no live flag. No pipeline attached, no live status key.
    svc = make_service(duration_s=2, asr_engine=MockASREngine())
    mid = svc.start_meeting(title="plain", source="mic")
    st = svc.get_status(mid)
    assert "live" not in st
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    assert mid not in svc._live_pipelines
    with session_scope() as s:
        segs = s.scalars(select(TranscriptSegment)
                         .where(TranscriptSegment.meeting_id == mid)).all()
    assert len(segs) == 0  # nothing transcribed during a plain batch recording


def test_transcribe_auto_diarizes_when_enabled(config, make_service):
    config.speaker_diarization = True  # live stays off (batch path)
    diar = MockDiarizationEngine([
        DiarSpan(0.0, 2.0, "Sprecher 1"),
        DiarSpan(2.0, 4.0, "Sprecher 2"),
    ])
    svc = make_service(duration_s=3, asr_engine=MockASREngine(), diar_engine=diar)
    mid = svc.start_meeting(title="batch", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)

    out = svc.transcribe(mid, language="de")
    assert out["status"] == "done"

    with session_scope() as s:
        segs = s.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == mid)
            .order_by(TranscriptSegment.start_s)).all()
        job = s.scalar(select(ProcessingJob).where(
            ProcessingJob.meeting_id == mid, ProcessingJob.stage == "diarize"))
    assert len(segs) == 2
    assert segs[0].speaker_id == "Sprecher 1"  # [0, 1.2] overlaps 0-2
    assert segs[1].speaker_id == "Sprecher 2"  # [1.5, 3.0] overlaps 2-4 most
    assert job is not None and job.status == "done"


def test_transcribe_no_auto_diarize_by_default(config, make_service):
    # speaker_diarization off: transcribe leaves speaker_id untouched (None).
    svc = make_service(duration_s=3, asr_engine=MockASREngine())
    mid = svc.start_meeting(title="batch2", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    svc.transcribe(mid, language="de")
    with session_scope() as s:
        segs = s.scalars(select(TranscriptSegment)
                         .where(TranscriptSegment.meeting_id == mid)).all()
    assert len(segs) == 2
    assert all(seg.speaker_id is None for seg in segs)


def test_diarize_requires_transcript(config, make_service):
    config.speaker_diarization = True
    svc = make_service(duration_s=2, asr_engine=MockASREngine(),
                       diar_engine=_two_speaker_diar())
    mid = svc.start_meeting(title="nodiag", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    # No transcript yet -> diarize must fail cleanly.
    with pytest.raises(ASRError):
        svc.diarize(mid)


def test_diarize_explicit_is_idempotent(config, make_service):
    diar = MockDiarizationEngine([
        DiarSpan(0.0, 2.0, "Sprecher 1"),
        DiarSpan(2.0, 4.0, "Sprecher 2"),
    ])
    svc = make_service(duration_s=3, asr_engine=MockASREngine(), diar_engine=diar)
    mid = svc.start_meeting(title="diag", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    svc.transcribe(mid, language="de")

    r1 = svc.diarize(mid)
    r2 = svc.diarize(mid)  # re-run must not duplicate / must stay stable
    assert r1["status"] == "done" and r2["status"] == "done"
    assert r1["segments"] == 2 and r1["speakers"] == 2
    with session_scope() as s:
        segs = s.scalars(select(TranscriptSegment)
                         .where(TranscriptSegment.meeting_id == mid)).all()
    assert len(segs) == 2
    assert {seg.speaker_id for seg in segs} == {"Sprecher 1", "Sprecher 2"}


def test_feature_flags(config, make_service):
    svc = make_service(duration_s=1.5, asr_engine=MockASREngine(),
                       live_engine=MockLiveASREngine())
    flags = svc.feature_flags()
    # Defaults are off, but the injected live engine is reported ready.
    assert flags["live_transcription"] is False
    assert flags["speaker_diarization"] is False
    assert flags["diarization_engine"] == "numpy-diar"
    # System-audio is off by default (and no loopback device in the test env).
    assert flags["system_audio"] is False

    config.live_transcription = True
    config.speaker_diarization = True
    flags = svc.feature_flags()
    assert flags["live_transcription"] is True
    assert flags["speaker_diarization"] is True
    assert flags["live_ready"] is True


def test_live_meeting_finalizes_transcribe_and_diarize_jobs(config, make_service):
    """Live text is a preview; authoritative post-stop jobs remain resumable.

    The final batch transcription/diarization must not be marked done merely
    because the preview emitted segments.  This preserves the quality pass and
    prevents a live model from silently replacing the selected final model.
    """
    config.live_transcription = True
    config.live_period_s = 0.1
    config.live_tail_s = 2.0
    config.live_window_s = 60.0
    config.speaker_diarization = True

    svc = make_service(duration_s=6, asr_engine=MockASREngine(),
                       live_engine=MockLiveASREngine(), diar_engine=_two_speaker_diar())
    mid = svc.start_meeting(title="live", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)

    def job(stage):
        with session_scope() as s:
            return s.scalar(select(ProcessingJob).where(
                ProcessingJob.meeting_id == mid, ProcessingJob.stage == stage))

    tjob = job("transcribe")
    djob = job("diarize")
    assert tjob is not None and tjob.status == "pending"
    # No diarization row is needed until the batch pipeline/manual action is
    # started; pipeline_status exposes this as pending for the applicable
    # stage, without leaving a fake running job behind.
    assert djob is None or djob.status == "pending"


def test_start_meeting_source_failure_marks_failed(config, make_service):
    # Regression (#3): a failure while preparing the capture source (e.g. the
    # device rejects every rate) must mark the meeting "failed", not leave a
    # phantom "recording" meeting that blocks every later start.
    from core.llm import MockLLM
    from core.service import MeetingService
    from core.store.models import Meeting, Recording

    make_service()  # initialize the shared DB for this config

    def bad_factory(device_id, sr, ch):
        raise RuntimeError("device rejects all rates")

    svc = MeetingService(config, source_factory=bad_factory, llm_engine=MockLLM())
    svc.record_consent(True)
    with pytest.raises(RuntimeError):
        svc.start_meeting(title="X", source="mic")

    with session_scope() as s:
        m = s.scalars(select(Meeting).where(Meeting.title == "X")).first()
        assert m is not None and m.status == "failed"
        rec = s.scalars(select(Recording).where(
            Recording.meeting_id == m.id)).first()
        assert rec is not None and rec.in_progress is False

    # No phantom: a fresh service must be able to start a meeting.
    make_service().start_meeting(title="Y", source="mic")


def test_rate_probe_all_rejected_marks_failed(config, make_service, monkeypatch):
    # Regression: when a *real* mic source is built successfully but the device
    # rejects every candidate rate (try_rates -> RuntimeError), the meeting must
    # still be marked "failed" and must not block the next start. This exercises
    # the LiveSource rate-probe branch specifically (a synthetic source skips it).
    import core.service as service_mod
    from core.audio.stream import LiveSource
    from core.llm import MockLLM
    from core.store.models import Meeting, Recording

    make_service()  # initialize the shared DB for this config

    def live_factory(device_id, sr, ch):
        return LiveSource(device_id, sr, ch)

    def boom(device_id, rate, channels, block_ms=50):
        raise RuntimeError(f"device rejects {rate} Hz")

    monkeypatch.setattr(service_mod, "probe_portaudio", boom)
    svc = service_mod.MeetingService(config, source_factory=live_factory,
                                     llm_engine=MockLLM())
    svc.record_consent(True)
    with pytest.raises(RuntimeError, match="Keine Samplerate"):
        svc.start_meeting(title="rates", source="mic")

    with session_scope() as s:
        m = s.scalars(select(Meeting).where(Meeting.title == "rates")).first()
        assert m is not None and m.status == "failed"
        rec = s.scalars(select(Recording).where(
            Recording.meeting_id == m.id)).first()
        assert rec is not None and rec.in_progress is False and rec.status == "failed"

    # A failed rate-probe meeting is not in ACTIVE_STATUSES, so a fresh service
    # can still start a new meeting (no phantom "active" block).
    make_service().start_meeting(title="after", source="mic")
