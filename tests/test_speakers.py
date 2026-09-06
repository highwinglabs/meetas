"""Phase 4: per-meeting speaker rename/profiles + opt-in pyannote engine."""
from __future__ import annotations

from core.providers import ASRError
from core.providers.diar import (
    NumpyDiarizationEngine, PyannoteDiarizationEngine,
)
from core.store.db import session_scope
from core.store.models import TranscriptSegment


def _seed_segments(mid: str, pairs: list[tuple[str, float]]):
    with session_scope() as s:
        for i, (spk, st) in enumerate(pairs):
            s.add(TranscriptSegment(
                id=f"s{i}", meeting_id=mid, start_s=st, end_s=st + 2.0,
                text=f"Text {i}", raw_text="x", speaker_id=spk, status="bestaetigt"))
        s.commit()


def test_list_and_rename(make_service, config):
    svc = make_service()
    mid = svc.start_meeting(title="T", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    with session_scope() as s:
        for seg in s.query(TranscriptSegment).filter_by(meeting_id=mid):
            s.delete(seg)
        s.commit()
    _seed_segments(mid, [("Sprecher 1", 0.0), ("Sprecher 1", 3.0),
                         ("Sprecher 2", 6.0)])

    spk = {x["speaker_id"]: x for x in svc.list_speakers(mid)}
    assert spk["Sprecher 1"]["segments"] == 2 and spk["Sprecher 2"]["segments"] == 1
    assert spk["Sprecher 1"]["label"] is None

    out = svc.rename_speaker(mid, "Sprecher 2", "Dr. Berger")
    assert out["updated_segments"] == 1 and out["to"] == "Dr. Berger"

    spk = {x["speaker_id"]: x for x in svc.list_speakers(mid)}
    assert "Sprecher 2" not in spk
    assert spk["Dr. Berger"]["segments"] == 1
    assert spk["Dr. Berger"]["label"] == "Dr. Berger"

    # The rename is reflected on the actual segments (exports/FTS see it).
    with session_scope() as s:
        texts = s.query(TranscriptSegment.speaker_id).filter_by(meeting_id=mid).all()
    assert sorted(r[0] for r in texts) == ["Dr. Berger", "Sprecher 1", "Sprecher 1"]

    # Renaming again (on the already-renamed id) updates segments in place.
    out2 = svc.rename_speaker(mid, "Dr. Berger", "Dr. Berger M.")
    assert out2["updated_segments"] == 1
    with session_scope() as s:
        ids = sorted(r[0] for r in
                     s.query(TranscriptSegment.speaker_id)
                     .filter_by(meeting_id=mid).all())
    assert ids == ["Dr. Berger M.", "Sprecher 1", "Sprecher 1"]


def test_rename_merge_requires_confirm(make_service, config):
    # L13: renaming onto an existing speaker name merges the two speakers and
    # must be explicitly confirmed.
    from core.service import SpeakerMergeConflictError
    svc = make_service()
    mid = svc.start_meeting(title="T", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    with session_scope() as s:
        for seg in s.query(TranscriptSegment).filter_by(meeting_id=mid):
            s.delete(seg)
        s.commit()
    _seed_segments(mid, [("Sprecher 1", 0.0), ("Sprecher 2", 3.0)])

    # Renaming onto a non-existing name is a plain rename (no confirm needed).
    out = svc.rename_speaker(mid, "Sprecher 1", "Neu")
    assert out["updated_segments"] == 1

    # Renaming the other speaker onto "Neu" (an existing speaker) would merge ->
    # refused without confirmation.
    try:
        svc.rename_speaker(mid, "Sprecher 2", "Neu")
        assert False, "expected SpeakerMergeConflictError"
    except SpeakerMergeConflictError as exc:
        assert exc.existing_segments == 1

    # With confirmation the merge proceeds and both segments share the name.
    out2 = svc.rename_speaker(mid, "Sprecher 2", "Neu", confirm_merge=True)
    assert out2["updated_segments"] == 1
    with session_scope() as s:
        ids = sorted(r[0] for r in
                     s.query(TranscriptSegment.speaker_id)
                     .filter_by(meeting_id=mid).all())
    assert ids == ["Neu", "Neu"]


def test_rename_requires_nonempty(make_service, config):
    svc = make_service()
    mid = svc.start_meeting(title="T", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    try:
        svc.rename_speaker(mid, "", "X")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_pyannote_is_optin_not_ready(make_service, config):
    # numpy is the default engine.
    assert isinstance(make_service()._diar_engine(), NumpyDiarizationEngine)

    # Explicitly selecting pyannote yields the pyannote engine, but it is NOT
    # ready (dependency/model absent) and diarize fails clearly -- never
    # downloading anything.
    config.diarization_backend = "pyannote"
    svc = make_service()
    eng = svc._diar_engine()
    assert isinstance(eng, PyannoteDiarizationEngine)
    assert eng.is_ready() is False
    import numpy as np
    try:
        eng.diarize(np.zeros(16000, dtype="float32"), 16000)
        assert False, "expected ASRError"
    except ASRError as exc:
        assert "opt-in" in str(exc)
