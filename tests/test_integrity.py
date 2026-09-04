"""Non-destructive workspace integrity reports."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from core.store.db import session_scope
from core.store.models import Recording


def test_integrity_report_has_no_errors_for_healthy_meeting(finalize_meeting):
    svc, meeting_id = finalize_meeting(duration_s=0.8)
    report = svc.integrity_report()
    assert report["status"] in {"ok", "warning"}
    assert report["summary"]["errors"] == 0
    assert report["summary"]["checks"] >= 5


def test_integrity_report_detects_missing_original_without_repair(finalize_meeting):
    svc, meeting_id = finalize_meeting(duration_s=0.8)
    with session_scope() as session:
        recording = session.scalar(select(Recording).where(
            Recording.meeting_id == meeting_id))
        original = Path(recording.original_path)
    original.unlink()

    report = svc.integrity_report()
    assert report["status"] == "error"
    assert any(issue["code"] == "recording_file_missing" for issue in report["issues"])
    assert not original.exists()
