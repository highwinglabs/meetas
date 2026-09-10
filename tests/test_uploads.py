"""Resumable upload completion: crash-window recovery (F4)."""
from __future__ import annotations

from core.store.db import session_scope
from core.store.models import Meeting, UploadSession, new_id, utcnow


def test_complete_upload_after_crash_between_import_and_status(make_service, config):
    """F4: a crash after the import committed the meeting but before the
    UploadSession status update (temp file already unlinked) must not leave
    the session un-completable forever."""
    svc = make_service()
    upload_id = new_id()
    with session_scope() as s:
        # The import committed this meeting (id == upload_id)...
        s.add(Meeting(id=upload_id, title="Imported", status="ready",
                      start_at=utcnow()))
        # ...but the process died before the status update and the temp file
        # is gone.
        s.add(UploadSession(
            id=upload_id, filename="call.wav",
            temp_path=str(config.base_dir / "upload_tmp" / f"{upload_id}.part"),
            total_size=1024, received_size=1024, status="uploading"))
        s.commit()

    out = svc.complete_upload(upload_id)

    assert out["status"] == "completed"
    with session_scope() as s:
        assert s.get(UploadSession, upload_id).status == "completed"
        assert s.get(UploadSession, upload_id).error is None
        assert s.get(Meeting, upload_id) is not None


def test_complete_upload_missing_file_without_meeting_still_fails(make_service, config):
    """The error path must stay intact: missing temp file AND no imported
    object is a real incomplete upload."""
    svc = make_service()
    upload_id = new_id()
    with session_scope() as s:
        s.add(UploadSession(
            id=upload_id, filename="call.wav",
            temp_path=str(config.base_dir / "upload_tmp" / f"{upload_id}.part"),
            total_size=1024, received_size=1024, status="uploading"))
        s.commit()

    try:
        svc.complete_upload(upload_id)
        assert False, "expected ValueError"
    except ValueError:
        pass
    with session_scope() as s:
        row = s.get(UploadSession, upload_id)
        assert row.status == "uploading"
