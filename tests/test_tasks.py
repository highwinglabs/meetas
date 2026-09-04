"""Phase 6: central task overview -- extraction from analysis, idempotency, CRUD.

Extraction must never invent facts: tasks come only from the stored, validated
``aufgaben`` section; ``S<n>`` citations are mapped to real segment ids; re-running
extraction is a no-op (stable ``dedup_key``).
"""
from __future__ import annotations

import json

from core.store.db import session_scope
from core.store.models import Analysis, Meeting, TranscriptSegment, utcnow


def _seed(meeting_id: str, segments: list[str]) -> None:
    with session_scope() as s:
        s.add(Meeting(id=meeting_id, title="Test", title_status="auto",
                      start_at=utcnow(), status="done"))
        for i, txt in enumerate(segments):
            s.add(TranscriptSegment(
                id=f"seg{i}", meeting_id=meeting_id, start_s=float(i * 2),
                end_s=float(i * 2 + 2), text=txt, raw_text=txt,
                speaker_id="Sprecher 1", status="bestaetigt"))
        s.commit()


def _store_analysis(meeting_id: str, aufgaben: list[dict]) -> None:
    content = json.dumps({
        "kurzfassung": [], "agenda": [], "entscheidungen": [],
        "aufgaben": aufgaben, "offene_fragen": [], "risiken": [],
        "nächste_schritte": [], "zusammenfassung": [],
    }, ensure_ascii=False)
    with session_scope() as s:
        s.add(Analysis(meeting_id=meeting_id, kind="summary",
                       model="mock", content=content))
        s.commit()


def test_extraction_and_idempotency(make_service):
    svc = make_service()
    mid = svc.start_meeting(title="T", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    # Overwrite the mock ASR segments with controlled ones + a real analysis.
    with session_scope() as s:
        for seg in s.query(TranscriptSegment).filter_by(meeting_id=mid):
            s.delete(seg)
        s.add(TranscriptSegment(id="seg0", meeting_id=mid, start_s=0.0, end_s=2.0,
                                text="Wir müssen das Budget vorstellen.",
                                raw_text="x", speaker_id="Ben", status="bestaetigt"))
        s.add(TranscriptSegment(id="seg1", meeting_id=mid, start_s=2.0, end_s=4.0,
                                text="Und den Termin fixieren.", raw_text="x",
                                speaker_id="Ben", status="bestaetigt"))
        s.commit()

    aufgaben = [
        {"text": "Budget vorstellen", "verantwortlich": "Ben", "deadline": "Freitag",
         "quellen": [{"segment_id": "S1", "sprecher": "Ben",
                      "timestamp": "00:00:00-00:00:02"}]},
        {"text": "Termin fixieren", "verantwortlich": "", "deadline": "nicht angegeben",
         "quellen": [{"segment_id": "S2", "sprecher": "Ben",
                      "timestamp": "00:00:02-00:00:04"}]},
    ]
    _store_analysis(mid, aufgaben)

    first = svc.extract_tasks_for_meeting(mid)
    assert first["created"] == 2 and first["total"] == 2

    # Idempotent: re-run creates nothing.
    again = svc.extract_tasks_for_meeting(mid)
    assert again["created"] == 0 and again["total"] == 2

    tasks = svc.list_tasks()
    assert len(tasks) == 2
    by_text = {t["text"]: t for t in tasks}
    b = by_text["Budget vorstellen"]
    assert b["owner"] == "Ben"
    assert b["deadline"] == "Freitag"
    # S1 -> first playback-order segment = seg0
    assert b["source_segment_id"] == "seg0"
    t = by_text["Termin fixieren"]
    assert t["owner"] == "nicht angegeben" and t["deadline"] == "nicht angegeben"
    assert t["source_segment_id"] == "seg1"


def test_extracted_tasks_follow_meeting_project(make_service):
    svc = make_service()
    project = svc.create_project("Task-Projekt")
    mid = svc.start_meeting(title="T", source="mic", project_id=project["id"])
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    _store_analysis(mid, [{"text": "Projektaufgabe", "quellen": []}])
    svc.extract_tasks_for_meeting(mid)
    assert svc.list_tasks(project_id=project["id"])[0]["text"] == "Projektaufgabe"


def test_manual_task_inherits_meeting_project(make_service):
    svc = make_service()
    project = svc.create_project("Manuelle Aufgaben")
    mid = svc.start_meeting(title="T", source="mic", project_id=project["id"])
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)

    task = svc.create_manual_task("Im Meeting nachfassen", meeting_id=mid)

    assert task["project_id"] == project["id"]
    assert svc.list_tasks(project_id=project["id"])[0]["text"] == "Im Meeting nachfassen"


def test_crud_and_overview(make_service):
    svc = make_service()
    mid = svc.start_meeting(title="T", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    _store_analysis(mid, [
        {"text": "Task A", "verantwortlich": "Ben", "deadline": None,
         "quellen": [{"segment_id": "S1", "sprecher": "Ben", "timestamp": "0"}]}
    ])
    svc.extract_tasks_for_meeting(mid)

    ov0 = svc.task_overview()
    assert ov0["total"] == 1 and ov0["by_status"]["ohne_deadline"] == 1

    tid = svc.list_tasks()[0]["id"]
    upd = svc.update_task(tid, status="laeuft")
    assert upd["status"] == "laeuft"

    upd = svc.update_task(tid, status="erledigt")
    assert upd["status"] == "erledigt"

    ov1 = svc.task_overview()
    assert ov1["by_status"]["erledigt"] == 1 and ov1["open"] == 0

    # Filter by owner + status.
    assert len(svc.list_tasks(owner="Ben")) == 1
    assert svc.list_tasks(status="offen") == []

    # Re-opening returns it to offen.
    svc.update_task(tid, status="offen")
    assert svc.list_tasks()[0]["status"] == "offen"

    # Text and deadline are editable, and both changes remain auditable.
    upd = svc.update_task(tid, text="Task A präzisieren", deadline="2026-09-04")
    assert upd["text"] == "Task A präzisieren"
    assert upd["deadline"] == "2026-09-04"
    history = svc.task_history(tid)
    assert {row["field"] for row in history} >= {"status", "text", "deadline"}


def test_invalid_status_rejected(make_service):
    svc = make_service()
    mid = svc.start_meeting(title="T", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    _store_analysis(mid, [
        {"text": "Task A", "verantwortlich": "Ben", "deadline": None,
         "quellen": [{"segment_id": "S1", "sprecher": "Ben", "timestamp": "0"}]}
    ])
    svc.extract_tasks_for_meeting(mid)
    tid = svc.list_tasks()[0]["id"]
    try:
        svc.update_task(tid, status="misterstand")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_task_archive_trash_restore_and_permanent_delete(make_service):
    svc = make_service()
    task = svc.create_manual_task("Aufräumen")
    tid = task["id"]

    assert [row["id"] for row in svc.list_tasks()] == [tid]
    assert svc.list_tasks(view="archived") == []
    assert svc.list_tasks(view="trash") == []

    svc.archive_task(tid)
    assert svc.list_tasks() == []
    assert svc.list_tasks(view="archived")[0]["id"] == tid

    svc.restore_task(tid)
    assert svc.list_tasks()[0]["id"] == tid

    svc.trash_task(tid)
    assert svc.list_tasks() == []
    assert svc.list_tasks(view="trash")[0]["deleted_at"]
    try:
        svc.permanently_delete_task(tid)
        assert False, "explicit confirmation is required"
    except ValueError:
        pass
    svc.permanently_delete_task(tid, confirm=True)
    assert svc.list_tasks(view="trash") == []


def test_no_analysis_no_tasks(make_service):
    svc = make_service()
    mid = svc.start_meeting(title="T", source="mic")
    svc._sessions[mid].wait_done(timeout=10)
    svc.stop(mid)
    out = svc.extract_tasks_for_meeting(mid)
    assert out["created"] == 0 and out["total"] == 0


def test_unique_dedup_keys_enforced(config):
    """Regression: the DB-level unique indexes guard the query-then-insert dedup
    paths, so a duplicate insert fails even if the application-level check races.
    A brand-new create_all DB must carry both unique keys."""
    from sqlalchemy.exc import IntegrityError
    from core.store.db import get_engine, make_engine, _secure_db_file
    from core.store.models import Base, Meeting, ProcessingJob, Task

    make_engine(config)
    if not config.data_dir.exists():
        config.ensure_dirs()
    Base.metadata.create_all(get_engine())
    _secure_db_file(config)

    mid = "m-unique-test"
    with session_scope() as s:
        s.add(Meeting(id=mid, title="T", title_status="auto",
                      start_at=utcnow(), status="done"))

    # task.dedup_key is unique (NULL keys for manual tasks are distinct).
    with session_scope() as s:
        s.add(Task(id="t1", meeting_id=mid, text="a", dedup_key="same-key"))
    try:
        with session_scope() as s:
            s.add(Task(id="t2", meeting_id=mid, text="b", dedup_key="same-key"))
        raise AssertionError("expected IntegrityError for duplicate dedup_key")
    except IntegrityError:
        pass

    # one logical job per (meeting_id, stage).
    with session_scope() as s:
        s.add(ProcessingJob(id="j1", meeting_id=mid, stage="transcribe"))
    try:
        with session_scope() as s:
            s.add(ProcessingJob(id="j2", meeting_id=mid, stage="transcribe"))
        raise AssertionError("expected IntegrityError for duplicate (meeting, stage)")
    except IntegrityError:
        pass

    # a different stage is allowed.
    with session_scope() as s:
        s.add(ProcessingJob(id="j3", meeting_id=mid, stage="diarize"))
