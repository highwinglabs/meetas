"""ChunkWriter + assembly: crash-safety, index integrity, idempotency."""
from __future__ import annotations

import json
import os
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


def test_chunk_wav_fsynced_before_index_line(tmp_path, monkeypatch):
    """H2: the chunk's bytes must reach stable storage before the index line
    referencing them is written; otherwise a power loss could persist an index
    entry pointing at a chunk that only exists in the page cache."""
    from core.audio import chunker as chunker_mod

    order: list[tuple[str, str]] = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        try:
            path = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            path = f"fd{fd}"
        order.append(("fsync", path))
        return real_fsync(fd)

    monkeypatch.setattr(chunker_mod.os, "fsync", spy_fsync)
    w = ChunkWriter(tmp_path / "m", 16000, 1, 1.0, source="mic")
    w.begin()
    real_append = w._append_index

    def spy_append(entry):
        order.append(("index_write", str(w.index_path)))
        return real_append(entry)

    w._append_index = spy_append
    try:
        w.write_chunk(np.zeros(16000, dtype=np.float32).reshape(-1, 1))
    finally:
        w.close()
        monkeypatch.undo()

    index_positions = [i for i, e in enumerate(order) if e[0] == "index_write"]
    assert index_positions, "index line was never written"
    chunk = (tmp_path / "m" / "chunks" / "chunk_000000.wav").resolve()
    first_index = index_positions[0]
    fsynced_before = [
        e for e in order[:first_index]
        if e[0] == "fsync" and e[1] == str(chunk)
    ]
    assert fsynced_before, (
        f"chunk WAV must be fsynced before the index line is written, got {order}"
    )


def test_verify_original_upload_layout(tmp_path):
    """M2: uploaded meetings store original.<ext> (+ original_16k.wav), not
    original.wav -- verify_original must report on the real file instead of
    claiming the original is missing and durations trivially match."""
    import wave as wave_mod

    meeting = tmp_path / "m"
    meeting.mkdir()
    (meeting / "original.mp3").write_bytes(b"\x00\x00fake mp3 payload")
    asr = meeting / "original_16k.wav"
    with wave_mod.open(str(asr), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 2)  # 2 s of silence

    info = verify_original(meeting, get_config())
    assert info["source"] == "upload"
    assert info["original_name"] == "original.mp3"
    assert info["original_exists"] is True
    assert info["read_only"] is False
    assert info["chunks_indexed"] == 0
    assert info["expected_duration_s"] is None
    assert info["duration_match"] is None
    assert abs(info["actual_duration_s"] - 2.0) < 0.05
    assert info["validation_error"] is None

    os.chmod(asr, 0o444)
    os.chmod(meeting / "original.mp3", 0o444)
    info = verify_original(meeting, get_config())
    assert info["read_only"] is True


def test_verify_original_revalidates_only_when_index_changes(tmp_path, monkeypatch):
    """M3: chunk hashes must not be recomputed while the index file is
    unchanged (the meeting-detail endpoint calls verify_original per request)."""
    from core.audio import assembly as assembly_mod

    w = _writer(tmp_path, 3)
    w.close()
    assemble_original(tmp_path / "m", get_config())

    calls = {"n": 0}
    real_sha256 = assembly_mod.hashlib.sha256

    def counting_sha256(data=b""):
        calls["n"] += 1
        return real_sha256(data)

    monkeypatch.setattr(assembly_mod.hashlib, "sha256", counting_sha256)
    meeting = tmp_path / "m"
    index = meeting / "chunks.index.jsonl"

    first = verify_original(meeting, get_config())
    assert first["chunks_valid"] == 3
    hashed = calls["n"]
    assert hashed > 0

    second = verify_original(meeting, get_config())
    assert second == first
    assert calls["n"] == hashed, "unchanged index must not trigger re-hashing"

    # touch the index -> new (mtime, size) key -> validation runs again
    st = index.stat()
    os.utime(index, (st.st_atime, st.st_mtime + 10.0))
    third = verify_original(meeting, get_config())
    assert third["chunks_valid"] == 3
    assert calls["n"] > hashed


def test_assembly_rejects_chunk_path_escape(tmp_path):
    meeting = tmp_path / "m"
    (meeting / "chunks").mkdir(parents=True)
    (meeting / "chunks.index.jsonl").write_text(json.dumps({
        "seq": 0, "start_s": 0, "dur_s": 1, "file": "../../outside.wav"
    }) + "\n")
    with pytest.raises(AssemblyError, match="Chunk-Pfad"):
        assemble_original(meeting, get_config())
