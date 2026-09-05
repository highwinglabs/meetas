"""Export (markdown/txt/json) with structured source references."""
from __future__ import annotations

import json

import pytest

from core.export import export_meeting
from core.store.db import session_scope
from core.store.models import Meeting, Recording
from sqlalchemy import select


def _set_analysis_language(svc, mid, code: str) -> None:
    with session_scope() as s:
        m = s.get(Meeting, mid)
        existing = json.loads(m.settings_json or "{}")
        existing["analysis_language"] = code
        m.settings_json = json.dumps(existing, ensure_ascii=False)
        s.commit()


def test_export_markdown_has_source_refs(config, finalize_meeting):
    svc, mid = finalize_meeting()
    svc.transcribe(mid)
    out = svc.export_meeting(mid, fmt="markdown")

    assert out["path"].endswith(".md")
    assert "Quellenverweise" in out["content"]
    assert "seg:" in out["content"]
    assert "00:00" in out["content"]
    # mock segments have no speaker -> "nicht angegeben"
    assert "nicht angegeben" in out["content"]
    # original audio path is referenced
    with session_scope() as s:
        rec = s.scalar(select(Recording).where(Recording.meeting_id == mid))
    assert rec.original_path.split("/")[-1] in out["content"]


def test_export_txt_and_json(config, finalize_meeting):
    svc, mid = finalize_meeting()
    svc.transcribe(mid)

    txt = svc.export_meeting(mid, fmt="txt")
    assert "Transkript" in txt["content"]
    assert "seg:" in txt["content"]

    js = svc.export_meeting(mid, fmt="json")
    data = json.loads(js["content"])
    assert data["meeting"]["id"] == mid
    assert len(data["segments"]) == 2
    seg = data["segments"][0]
    for key in ("id", "start_s", "end_s", "text", "audio_ref"):
        assert key in seg
    assert data["segments"][0]["speaker_id"] is None  # "nicht angegeben" upstream


def test_export_files_written_with_restricted_perms(config, finalize_meeting):
    import os
    svc, mid = finalize_meeting()
    svc.transcribe(mid)
    path = export_meeting(mid, fmt="json", config=config)
    assert path.exists()
    assert path.parent == config.exports_dir
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600


def test_export_unknown_format_raises(config, finalize_meeting):
    svc, mid = finalize_meeting()
    with pytest.raises(ValueError):
        svc.export_meeting(mid, fmt="pdfx")


def test_export_unknown_meeting_raises(config, finalize_meeting):
    svc = finalize_meeting()[0]
    with pytest.raises(KeyError):
        svc.export_meeting("does-not-exist", fmt="markdown")


def test_export_html_lang_and_chrome_follow_analysis_language(config, finalize_meeting):
    """The document language (HTML <html lang> + all chrome labels) follows the
    analysis output language, not the meeting/transcript language."""
    # German control: no analysis, German transcript -> German document.
    svc, mid = finalize_meeting()
    svc.transcribe(mid)
    de = svc.export_meeting(mid, fmt="html")["content"]
    assert "<html lang='de'>" in de
    assert "Transkript" in de
    assert "Quellenverweise" in de

    # English: per-meeting analysis_language="en" -> English chrome and <html lang>,
    # even though the transcript (and its content) is still German.
    svc2, mid2 = finalize_meeting()
    svc2.transcribe(mid2)
    _set_analysis_language(svc2, mid2, "en")
    svc2.analyze(mid2)  # stores an analysis row with output_lang="en"
    en = svc2.export_meeting(mid2, fmt="html")["content"]
    assert "<html lang='en'>" in en
    assert "Transcript" in en
    assert "Source references" in en
    assert "Analysis (local, with sources)" in en
    assert "Quellenverweise" not in en


def test_export_markdown_localised_by_analysis_language(config, finalize_meeting):
    svc, mid = finalize_meeting()
    svc.transcribe(mid)
    _set_analysis_language(svc, mid, "en")
    svc.analyze(mid)
    md = svc.export_meeting(mid, fmt="markdown")["content"]
    assert "## Summary" in md            # localised analysis section heading
    assert "## Source references" in md  # localised chrome
    assert "## Kurzfassung" not in md
