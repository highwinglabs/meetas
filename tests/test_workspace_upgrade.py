"""Regression tests for the workspace lifecycle additions."""
from __future__ import annotations

import time

from sqlalchemy import select


def test_trash_restore_and_permanent_delete_keep_lifecycle_explicit(finalize_meeting):
    svc, meeting_id = finalize_meeting(title="Arbeitsmeeting")
    project = svc.create_project("Projekt A")
    task = svc.create_manual_task("Nachfassen", meeting_id=meeting_id, project_id=project["id"])

    listed = svc.list_meetings()
    assert listed[0]["start_at"].endswith("+00:00")
    assert task["source"] == "manuell"
    assert svc.list_tasks()[0]["project_id"] == project["id"]

    svc.trash_meeting(meeting_id)
    assert svc.list_meetings() == []
    assert svc.list_trash()["meetings"][0]["id"] == meeting_id
    svc.restore_meeting(meeting_id)
    assert svc.list_meetings()[0]["id"] == meeting_id

    svc.trash_meeting(meeting_id)
    svc.permanently_delete_meeting(meeting_id)
    assert svc.list_meetings() == []


def test_trashed_meeting_is_hidden_from_search(finalize_meeting):
    svc, meeting_id = finalize_meeting(title="Suchmeeting")
    svc.transcribe(meeting_id)
    assert svc.search("Budget")
    svc.trash_meeting(meeting_id)
    assert svc.search("Budget") == []


def test_permanent_meeting_delete_removes_linked_project_file(finalize_meeting):
    """The SET NULL meeting FK must not leave an orphaned upload record."""
    from core.store.db import session_scope
    from core.store.models import ProjectFile, Recording

    svc, meeting_id = finalize_meeting()
    with session_scope() as s:
        recording = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
        asset = ProjectFile(project_id=None, meeting_id=meeting_id,
                            original_name="aufnahme.wav", path=recording.original_path,
                            kind="audio", size=1)
        s.add(asset)
        s.flush()
        file_id = asset.id
    svc.trash_meeting(meeting_id)
    svc.permanently_delete_meeting(meeting_id)
    with session_scope() as s:
        assert s.get(ProjectFile, file_id) is None


def test_project_delete_preserves_meeting_recording(finalize_meeting):
    """Deleting a project must not erase audio still owned by its meeting."""
    from core.store.db import session_scope
    from core.store.models import Meeting, ProjectFile, Recording

    svc, meeting_id = finalize_meeting()
    project = svc.create_project("Audio-Projekt")
    svc.update_meeting(meeting_id, project_id=project["id"], project_specified=True)
    with session_scope() as s:
        recording = s.scalar(select(Recording).where(Recording.meeting_id == meeting_id))
        audio_path = recording.original_path
        asset = ProjectFile(project_id=project["id"], meeting_id=meeting_id,
                            original_name="aufnahme.wav", path=audio_path,
                            kind="audio", size=1)
        s.add(asset)
    svc.trash_project(project["id"])
    svc.permanently_delete_project(project["id"])
    with session_scope() as s:
        assert s.scalar(select(Meeting.project_id).where(Meeting.id == meeting_id)) is None
        assert s.scalar(select(ProjectFile.id).where(ProjectFile.meeting_id == meeting_id)) is None
        assert s.scalar(select(Recording.original_path).where(Recording.meeting_id == meeting_id)) == audio_path
    assert audio_path and __import__("pathlib").Path(audio_path).is_file()


def test_active_project_cannot_be_trashed(make_service):
    svc = make_service(duration_s=5.0)
    project = svc.create_project("Laufendes Projekt")
    meeting_id = svc.start_meeting(project_id=project["id"])
    try:
        from core.service import ActiveMeetingError
        try:
            svc.trash_project(project["id"])
        except ActiveMeetingError:
            pass
        else:
            raise AssertionError("active project was moved to trash")
    finally:
        svc.stop(meeting_id)


def test_manual_transcription_is_persistent_background_job(finalize_meeting):
    svc, meeting_id = finalize_meeting()
    queued = svc.start_transcription(meeting_id)
    assert queued["status"] in {"pending", "running"}
    deadline = time.time() + 5
    while time.time() < deadline:
        job = next((j for j in svc.get_meeting(meeting_id)["jobs"] if j["stage"] == "transcribe"), None)
        if job and job["status"] == "done":
            break
        time.sleep(0.02)
    assert job is not None and job["status"] == "done"


def test_permanent_delete_removes_audio_dir_without_original_path(finalize_meeting, config):
    """F3: a failed assembly (original_path NULL) must not leave the meeting's
    audio directory (raw chunks included) on disk after a permanent delete."""
    from core.store.db import session_scope
    from core.store.models import Recording

    svc, mid = finalize_meeting(title="Broken")
    with session_scope() as s:
        rec = s.scalar(select(Recording).where(Recording.meeting_id == mid))
        assert rec is not None and rec.original_path
        rec.original_path = None  # simulate failed assembly
        s.commit()
    audio_dir = config.audio_dir / mid
    assert audio_dir.is_dir()

    svc.trash_meeting(mid)
    out = svc.permanently_delete_meeting(mid)

    assert out["deleted"] is True
    assert out["audio_removed"] is True
    assert not audio_dir.exists()
