"""ChunkWriter + assembly: crash-safety, index integrity, idempotency."""
from __future__ import annotations

import json
import numpy as np
import pytest

from core.audio.assembly import AssemblyError, assemble_original, verify_original
from core.audio.chunker import ChunkWriter
from core.config import get_config


def _writer(tmp_path, n_chunks=3):
    w = ChunkWriter(tmp_path / "m", 16000, 1, 1.0, source="mic")
    w.begin()
    for _ in range(n_chunks):
        w.write_chunk(np.zeros(16000, dtype=np.float32).reshape(-1, 1))
    return w


def test_index_written_and_readable(tmp_path):
    w = _writer(tmp_path, 3)
    entries = w.read_index()
    assert len(entries) == 3
    assert entries[0]["seq"] == 0
    assert abs(entries[0]["start_s"] - 0.0) < 1e-6
    assert abs(entries[1]["start_s"] - 1.0) < 1e-6
    assert abs(entries[2]["start_s"] - 2.0) < 1e-6
    assert all(e["dur_s"] == 1.0 for e in entries)
    assert all(len(e["sha256"]) == 64 for e in entries)
    assert w.total_duration_s() == 3.0
    w.close()


def test_assembly_produces_read_only_original(tmp_path):
    w = _writer(tmp_path, 3)
    w.close()
    original = assemble_original(tmp_path / "m", get_config())
    assert original.exists()
    info = verify_original(tmp_path / "m", get_config())
    assert info["original_exists"] is True
    assert info["read_only"] is True
    assert info["chunks_indexed"] == 3
    assert info["actual_duration_s"] > 2.9


def test_assembly_is_idempotent(tmp_path):
    w = _writer(tmp_path, 2)
    w.close()
    p1 = assemble_original(tmp_path / "m", get_config())
    p2 = assemble_original(tmp_path / "m", get_config())
    assert p1 == p2
    assert p1.exists()


def test_torn_index_line_is_ignored(tmp_path):
    w = _writer(tmp_path, 2)
    # simulate a crash that left a half-written index line
    with open(w.index_path, "a", encoding="utf-8") as f:
        f.write('{"seq": 99, "start_s": 9.0, "dur_s": 1.0, "file": "c')
    w.close()
    entries = w.read_index()
    assert len(entries) == 2  # torn line dropped
    original = assemble_original(tmp_path / "m", get_config())
    assert original.exists()
    info = verify_original(tmp_path / "m", get_config())
    assert info["chunks_indexed"] == 2


def test_in_progress_marker_lifecycle(tmp_path):
    w = _writer(tmp_path, 1)
    assert w.is_in_progress is True
    w.close()
    w.clear_in_progress()
    assert w.is_in_progress is False


def test_assembly_rejects_chunk_path_escape(tmp_path):
    meeting = tmp_path / "m"
    (meeting / "chunks").mkdir(parents=True)
    (meeting / "chunks.index.jsonl").write_text(json.dumps({
        "seq": 0, "start_s": 0, "dur_s": 1, "file": "../../outside.wav"
    }) + "\n")
    with pytest.raises(AssemblyError, match="Chunk-Pfad"):
        assemble_original(meeting, get_config())
