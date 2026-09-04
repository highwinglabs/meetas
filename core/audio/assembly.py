"""Assemble the immutable original recording from indexed chunks.

- The original is produced atomically (write to .tmp, then rename).
- After assembly the original is made read-only (0444) so no processing can
  ever overwrite it (privacy/provenance requirement).
- A 16 kHz mono copy is produced for later ASR without touching the original.
- Assembly is idempotent and verifiable (recovery uses the same path).
"""
from __future__ import annotations

import json
import os
import subprocess
import wave
import hashlib
import math
from pathlib import Path

from core.config import Config, get_config
from core.logging_setup import get_logger

log = get_logger("ma.audio.assembly")


class AssemblyError(RuntimeError):
    pass


def _meta(meeting_dir: Path) -> dict:
    p = meeting_dir / "meta.json"
    if not p.exists():
        return {}
    try:
        value = json.loads(p.read_text())
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _run_ffmpeg(args: list[str], timeout: float = 300.0) -> None:
    try:
        proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error"] + args,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AssemblyError(
            f"ffmpeg lief zu lange und wurde beendet (Timeout {timeout:.0f}s).") from exc
    if proc.returncode != 0:
        raise AssemblyError(f"ffmpeg failed: {proc.stderr.strip()[:500]}")


def _wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate() or 1
        return w.getnframes() / rate


def repair_wav_header(path: Path) -> bool:
    """Fix a RIFF/WAV header whose size fields do not match the file size.

    Some encoders (and torn writes) produce WAV files where the RIFF size or
    the ``data`` chunk size is zero or wrong while the PCM payload itself is
    complete.  Browsers and the ``wave`` module rely on those fields, so such
    files are unplayable there even though ffmpeg tolerates them (it falls
    back to reading until EOF).  The repair is lossless: only the size fields
    are rewritten, never the audio data.
    """
    size = os.path.getsize(path)
    if size < 44:
        return False
    with open(path, "rb") as handle:
        head = handle.read(12)
    if len(head) < 12 or head[0:4] != b"RIFF" or head[8:12] != b"WAVE":
        return False
    riff_size = int.from_bytes(head[4:8], "little")
    chunks: list[tuple[bytes, int, int]] = []
    with open(path, "rb") as handle:
        pos = 12
        while pos + 8 <= size:
            handle.seek(pos)
            block = handle.read(8)
            if len(block) < 8:
                break
            cid = block[0:4]
            csize = int.from_bytes(block[4:8], "little")
            if csize > size - pos - 8:
                break  # corrupt chunk table; do not touch the file
            chunks.append((cid, csize, pos))
            if cid == b"data":
                break
            pos += 8 + csize + (csize & 1)
    if not chunks or chunks[-1][0] != b"data":
        return False
    data_cid, data_size, data_pos = chunks[-1]
    data_offset = data_pos + 8
    expected_data = size - data_offset
    expected_riff = size - 8
    fixes: list[tuple[int, int]] = []
    if data_size != expected_data and data_size != 0xFFFFFFFF:
        fixes.append((data_offset - 4, expected_data))
    if riff_size != expected_riff and riff_size != 0xFFFFFFFF:
        fixes.append((4, expected_riff))
    if not fixes:
        return False
    mode = os.stat(path).st_mode
    made_writable = not (mode & 0o200)
    if made_writable:
        os.chmod(path, mode | 0o200)
    try:
        with open(path, "r+b") as handle:
            for offset, value in fixes:
                handle.seek(offset)
                handle.write(value.to_bytes(4, "little"))
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if made_writable:
            os.chmod(path, mode)
    log.info("wav_header_repaired path=%s fixes=%d", path.name, len(fixes))
    return True


def _validated_entries(meeting_dir: Path, raw_entries: list[dict]) -> list[dict]:
    """Validate the on-disk chunk manifest before handing paths to ffmpeg.

    The manifest is local state, but it is still an integrity boundary: a
    damaged or manually edited index must never make ffmpeg read a path outside
    the meeting's chunk directory.  Sequence/timing checks also prevent silent
    gaps and duplicated audio during recovery.
    """
    chunk_root = (meeting_dir / "chunks").resolve()
    out: list[dict] = []
    expected_seq = 0
    expected_start = 0.0
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise AssemblyError("Ungültiger Chunk-Eintrag.")
        name = entry.get("file")
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise AssemblyError("Ungültiger Chunk-Pfad im Aufnahmeindex.")
        path = (chunk_root / name).resolve()
        if chunk_root not in path.parents or not path.is_file():
            raise AssemblyError("Chunk fehlt oder liegt außerhalb des Aufnahmeordners.")
        try:
            seq = int(entry.get("seq"))
            start = float(entry.get("start_s"))
            dur = float(entry.get("dur_s"))
        except (TypeError, ValueError) as exc:
            raise AssemblyError("Ungültige Chunk-Zeitdaten.") from exc
        if (seq != expected_seq or not math.isfinite(start) or not math.isfinite(dur)
                or dur <= 0 or start < -1e-3
                or abs(start - expected_start) > 0.05):
            raise AssemblyError("Lücke oder Doppelung im Aufnahmeindex erkannt.")
        expected_seq += 1
        expected_start = start + dur
        digest = entry.get("sha256")
        if digest:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != str(digest):
                raise AssemblyError("Chunk-Prüfsumme stimmt nicht mit dem Aufnahmeindex überein.")
        out.append({**entry, "_path": path, "start_s": start, "dur_s": dur})
    if not out:
        raise AssemblyError("Keine vollständigen Chunks zum Assemblieren gefunden.")
    return out


def _read_index(path: Path) -> list[dict]:
    """Read the manifest, tolerating only a torn final write.

    ``ChunkWriter`` appends and fsyncs one JSON line at a time.  A process
    crash can therefore leave a final, unterminated partial line.  That line
    is safe to discard because the preceding entries are the committed audio
    state.  Malformed lines in the middle of the file (or a malformed line
    that did receive its newline) are treated as corruption and fail closed.
    """
    entries: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                if not line.endswith("\n"):
                    # A write interrupted before the record delimiter is the
                    # expected crash-safe/torn-line case.
                    break
                raise AssemblyError(
                    f"Ungültiger Aufnahmeindex (Zeile {line_no}).") from exc
    return entries


def assemble_original(meeting_dir: Path, config: Config | None = None,
                      force: bool = False) -> Path:
    """Build original.wav (+16k copy) from the chunk index. Idempotent."""
    cfg = config or get_config()
    meeting_dir = Path(meeting_dir)
    original = meeting_dir / "original.wav"
    if original.exists() and not force:
        # Idempotence must not mean blind trust: validate an existing output
        # before reusing it so a tampered/truncated recording is detected.
        writer_index = meeting_dir / "chunks.index.jsonl"
        if writer_index.exists():
            existing_entries = _read_index(writer_index)
            _verify(meeting_dir, original, _validated_entries(meeting_dir, existing_entries))
        else:
            try:
                _wav_duration_s(original)
            except (OSError, wave.Error) as exc:
                raise AssemblyError("Vorhandene Originalaufnahme ist beschädigt.") from exc
        return original

    if not cfg.ffmpeg_available():
        raise AssemblyError("ffmpeg ist nicht installiert (erforderlich).")

    writer_index = meeting_dir / "chunks.index.jsonl"
    if not writer_index.exists():
        raise AssemblyError("Kein Chunk-Index vorhanden.")

    entries = _read_index(writer_index)
    entries = _validated_entries(meeting_dir, entries)

    meta = _meta(meeting_dir)
    sr = int(meta.get("sample_rate", 16000))
    ch = int(meta.get("channels", 1))
    # Timeout scales with the expected recording length so large assemblies are
    # not killed, but a hung ffmpeg process can never block the pipeline forever.
    expected_s = sum(float(e.get("dur_s", 0.0)) for e in entries)
    ffmpeg_timeout = max(120.0, min(3600.0, expected_s * 20.0))

    concat = meeting_dir / "concat.txt.tmp"
    with open(concat, "w", encoding="utf-8") as f:
        for e in entries:
            chunk = e["_path"]
            f.write(f"file {chunk.as_posix()!r}\n")

    tmp = original.with_name("original.wav.tmp")
    try:
        _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat),
                     "-ar", str(sr), "-ac", str(ch), "-c:a", "pcm_s16le",
                     "-f", "wav", "-y", str(tmp)], timeout=ffmpeg_timeout)
        os.replace(tmp, original)
        os.chmod(original, 0o444)  # immutable
    finally:
        try:
            concat.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    # 16 kHz mono copy for ASR (leave the original untouched)
    if sr != 16000:
        out_16k = meeting_dir / "original_16k.wav"
        tmp16 = out_16k.with_name("original_16k.wav.tmp")
        try:
            _run_ffmpeg(["-i", str(original), "-ar", "16000", "-ac", "1",
                         "-c:a", "pcm_s16le", "-f", "wav", "-y", str(tmp16)],
                        timeout=max(120.0, min(3600.0, expected_s * 20.0)))
            os.replace(tmp16, out_16k)
            os.chmod(out_16k, 0o444)
        finally:
            try:
                tmp16.unlink(missing_ok=True)
            except Exception:
                pass

    _verify(meeting_dir, original, entries)
    log.info("original_assembled path=%s chunks=%s", original, len(entries))
    return original


def _verify(meeting_dir: Path, original: Path, entries: list[dict]) -> None:
    expected = sum(float(e["dur_s"]) for e in entries)
    try:
        actual = _wav_duration_s(original)
    except (OSError, wave.Error) as exc:
        raise AssemblyError("Assemblierte Aufnahme ist keine gültige WAV-Datei.") from exc
    if abs(expected - actual) > max(0.1, expected * 0.02):
        raise AssemblyError(
            f"Aufnahmedauer stimmt nicht (erwartet {expected:.3f}s, tatsächlich {actual:.3f}s).")


def verify_original(meeting_dir: Path, config: Config | None = None) -> dict:
    """Return integrity info about the assembled original (for tests/UI)."""
    cfg = config or get_config()
    meeting_dir = Path(meeting_dir)
    original = meeting_dir / "original.wav"
    writer_index = meeting_dir / "chunks.index.jsonl"
    entries = []
    parse_error = None
    if writer_index.exists():
        try:
            entries = _read_index(writer_index)
        except AssemblyError as exc:
            parse_error = str(exc)
    try:
        valid = _validated_entries(meeting_dir, entries) if entries and parse_error is None else []
        validation_error = parse_error
    except AssemblyError as exc:
        valid = []
        validation_error = str(exc)
    try:
        actual = _wav_duration_s(original) if original.exists() else 0.0
        audio_error = None
    except (OSError, wave.Error) as exc:
        actual = 0.0
        audio_error = str(exc)
    expected = sum(float(e.get("dur_s", 0.0)) for e in valid)
    info = {
        "original_exists": original.exists(),
        "read_only": original.exists() and not os.access(original, os.W_OK),
        "chunks_indexed": len(entries),
        "chunks_valid": len(valid),
        "expected_duration_s": round(expected, 3),
        "actual_duration_s": round(actual, 3),
        "duration_match": abs(expected - actual) <= max(0.1, expected * 0.02),
        "validation_error": validation_error or audio_error,
    }
    return info
