"""Transcript-version restore robustness.

Restoring a saved snapshot must re-create the segments **under the same ids**
that were captured in the snapshot, so Revision / Task / Analysis references
(``Task.source_segment_id``) stay intact. It must also keep the revision
bookkeeping correct (one new current version, old ones demoted) and stay
compatible with legacy snapshots that carry no segment ``id`` at all.
"""
from __future__ import annotations

import json

from sqlalchemy import delete, select

from core.store.db import session_scope
from core.store.models import (
    Analysis,
    Meeting,
    Task,
    TranscriptSegment,
    TranscriptVersion,
    new_id,
    utcnow,
)


def _meeting(mid: str) -> None:
    with session_scope() as s:
        s.add(Meeting(id=mid, title="V", title_status="auto",
                      start_at=utcnow(), status="done"))


def _set_segments(mid: str, specs: list[tuple[str, float, float, str]]) -> None:
    with session_scope() as s:
        s.execute(delete(TranscriptSegment).where(
            TranscriptSegment.meeting_id == mid))
        for seg_id, start, end, text in specs:
            s.add(TranscriptSegment(id=seg_id, meeting_id=mid, start_s=start,
                                    end_s=end, text=text, raw_text=text,
                                    speaker_id="Sprecher 1", status="bestaetigt"))
        s.commit()


def _payload(specs: list[tuple[str, float, float, str]]) -> list[dict]:
    return [
        {"id": seg_id, "start_s": start, "end_s": end, "text": text,
         "raw_text": text, "language": "de", "confidence": 0.9,
         "speaker_id": "Sprecher 1"}
        for (seg_id, start, end, text) in specs
    ]


def _snapshot(mid: str, version_no: int, payload: list[dict],
              is_current: bool = True, model: str = "mock") -> str:
    vid = new_id()
    with session_scope() as s:
        s.add(TranscriptVersion(id=vid, meeting_id=mid, version_no=version_no,
                                model=model, language="de",
                                segments_json=json.dumps(payload, ensure_ascii=False),
                                is_current=is_current))
        s.commit()
    return vid


def _store_analysis(mid: str, aufgaben: list[dict]) -> None:
    content = json.dumps({
        "kurzfassung": [], "agenda": [], "entscheidungen": [],
        "aufgaben": aufgaben, "offene_fragen": [], "risiken": [],
        "naechste_schritte": [], "zusammenfassung": [],
    }, ensure_ascii=False)
    with session_scope() as s:
        s.add(Analysis(meeting_id=mid, kind="summary", model="mock", content=content))
        s.commit()


def test_restore_preserves_segment_ids(make_service):
    svc = make_service()
    mid = new_id()
    _meeting(mid)
    base = [("seg0", 0.0, 2.0, "Erster Satz"),
            ("seg1", 2.0, 4.0, "Zweiter Satz"),
            ("seg2", 4.0, 6.0, "Dritter Satz")]
    _set_segments(mid, base)
    vid = _snapshot(mid, 1, _payload(base))

    # A newer run replaces the live segments under fresh ids.
    _set_segments(mid, [("x0", 0.0, 2.0, "Neu"), ("x1", 2.0, 4.0, "Neu 2")])
    with session_scope() as s:
        s.query(TranscriptVersion).filter_by(meeting_id=mid).update(
            {TranscriptVersion.is_current: False}, synchronize_session=False)
        s.commit()

    out = svc.restore_transcript_version(mid, vid)
    assert out["segments"] == 3
    with session_scope() as s:
        ids = {sg.id for sg in s.scalars(select(TranscriptSegment).where(
            TranscriptSegment.meeting_id == mid)).all()}
    # Restored segments reuse the *snapshot* ids, not freshly generated ones.
    assert ids == {"seg0", "seg1", "seg2"}


def test_restore_keeps_task_source_segment_ids_valid(make_service):
    svc = make_service()
    mid = new_id()
    _meeting(mid)
    _set_segments(mid, [("seg0", 0.0, 2.0, "Budget"), ("seg1", 2.0, 4.0, "Termin")])
    _store_analysis(mid, [
        {"text": "Termin fixieren", "verantwortlich": "Ben", "deadline": "nicht angegeben",
         "quellen": [{"segment_id": "S2"}]},
    ])
    svc.extract_tasks_for_meeting(mid)
    task = svc.list_tasks()[0]
    assert task["source_segment_id"] == "seg1"  # S2 -> playback row 1 -> seg1

    vid = _snapshot(mid, 1, _payload([("seg0", 0.0, 2.0, "Budget"),
                                      ("seg1", 2.0, 4.0, "Termin")]))
    # A newer run replaces the segments under fresh ids -> the task anchor dangles.
    _set_segments(mid, [("x0", 0.0, 2.0, "a"), ("x1", 2.0, 4.0, "b")])
    with session_scope() as s:
        dangling_id = s.scalar(select(Task.source_segment_id).where(Task.meeting_id == mid))
        exists = s.scalar(select(TranscriptSegment.id).where(
            TranscriptSegment.meeting_id == mid, TranscriptSegment.id == dangling_id))
    assert exists is None  # confirmed dangling

    svc.restore_transcript_version(mid, vid)
    with session_scope() as s:
        t = s.scalar(select(Task).where(Task.meeting_id == mid))
        seg = s.scalar(select(TranscriptSegment).where(
            TranscriptSegment.meeting_id == mid,
            TranscriptSegment.id == t.source_segment_id))
    # The restore re-created seg1, so the task anchor resolves again (no dangling ref).
    assert seg is not None and seg.id == "seg1"
    assert t.source_segment_id == "seg1"


def test_restore_creates_revision_and_demotes_old(make_service):
    svc = make_service()
    mid = new_id()
    _meeting(mid)
    _set_segments(mid, [("seg0", 0.0, 2.0, "a"), ("seg1", 2.0, 4.0, "b")])
    payload = _payload([("seg0", 0.0, 2.0, "a"), ("seg1", 2.0, 4.0, "b")])
    v1 = _snapshot(mid, 1, payload, is_current=False)
    v2 = _snapshot(mid, 2, _payload([("seg0", 0.0, 2.0, "a")]), is_current=True)

    out = svc.restore_transcript_version(mid, v1)
    assert out["segments"] == 2
    with session_scope() as s:
        versions = s.scalars(select(TranscriptVersion).where(
            TranscriptVersion.meeting_id == mid)).all()
        current = [v for v in versions if v.is_current]
        newv = s.get(TranscriptVersion, out["version_id"])
    # A brand-new version is the single current one, numbered past the old max.
    assert len(current) == 1
    assert current[0].id == out["version_id"] == newv.id
    assert newv.id not in (v1, v2)
    assert newv.version_no == 3
    assert newv.is_current is True
    assert all(not v.is_current for v in versions if v.id in (v1, v2))


def test_restore_legacy_snapshot_without_ids(make_service):
    # Regression: snapshots written before segment ids were recorded carry no
    # "id" field. Restore must still work, generating fresh unique ids.
    svc = make_service()
    mid = new_id()
    _meeting(mid)
    legacy = [
        {"start_s": 0.0, "end_s": 2.0, "text": "Alt", "raw_text": "Alt",
         "language": "de", "confidence": 0.8},
        {"start_s": 2.0, "end_s": 4.0, "text": "Alt 2", "raw_text": "Alt 2",
         "language": "de", "confidence": 0.8},
    ]
    vid = _snapshot(mid, 1, legacy)
    out = svc.restore_transcript_version(mid, vid)
    assert out["segments"] == 2
    with session_scope() as s:
        ids = [sg.id for sg in s.scalars(select(TranscriptSegment).where(
            TranscriptSegment.meeting_id == mid)).all()]
    assert len(ids) == 2
    assert len(set(ids)) == 2  # fresh, unique
    assert all(i for i in ids)
