"""Headless end-to-end demo of the Phase 1 capture pipeline.

Runs the real code paths (capture -> crash-safe chunks -> atomic assembly ->
SQLite persistence -> crash recovery) using a deterministic synthetic source,
so it works on a machine without a microphone.

Usage:  .venv/bin/python scripts/run_demo_capture.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

# make the project importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.audio.capture import CaptureSession                       # noqa: E402
from core.audio.chunker import ChunkWriter                          # noqa: E402
from core.audio.stream import SyntheticSource                       # noqa: E402
from core.config import Config, set_config                          # noqa: E402
from core.logging_setup import setup_logging                        # noqa: E402
from core.recovery.recovery import recover                          # noqa: E402
from core.store.db import get_engine, make_engine, session_scope    # noqa: E402
from core.store.models import Base, Meeting, Recording              # noqa: E402


def rule(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def demo_capture(config: Config) -> None:
    rule("1) Synchrone Aufnahme (3 s synthetisches Signal)")
    make_engine(config)
    Base.metadata.create_all(get_engine())

    mdir = config.audio_dir / "demo-live"
    session = CaptureSession(mdir, SyntheticSource(3.0, 16000, 1, block_seconds=0.1),
                             16000, 1, 1.0, config)
    session.begin()
    for block in SyntheticSource(3.0, 16000, 1, block_seconds=0.1).frames():
        session.feed(block)
    # pausiere kurz (wird verworfen, nur als Event protokolliert)
    session.pause()
    session.resume()
    state = session.finish()

    print(f"   Status:      {state.status.value}")
    print(f"   Dauer:       {state.duration_s:.2f} s")
    print(f"   Chunks:      {state.chunks}")
    print(f"   Original:    {state.original_path}")
    print(f"   Peak Level:  {state.peak_db:.1f} dBFS")

    from core.audio.assembly import verify_original
    info = verify_original(mdir, config)
    print(f"   Integrität:  exists={info['original_exists']} read_only={info['read_only']} "
          f"chunks={info['chunks_indexed']}")
    assert state.original_path and Path(state.original_path).exists()


def demo_crash_recovery(config: Config) -> None:
    rule("2) Crash-Simulation + Recovery")
    # Meeting "recording" in DB + Chunks auf Disk, aber KEIN finaler Assemblierung
    # (z. B. Stromausfall während der Aufnahme)
    with session_scope() as s:
        m = Meeting(title="crashed-demo", status="recording")
        s.add(m); s.flush()
        s.add(Recording(meeting_id=m.id, source="mic", status="recording", in_progress=True))
        s.commit()
        mid = m.id
    mdir = config.audio_dir / mid
    w = ChunkWriter(mdir, 16000, 1, 1.0, source="mic")
    w.begin()
    for _ in range(2):
        w.write_chunk(np.zeros(16000, dtype=np.float32).reshape(-1, 1))
    w.close()  # Marker bleibt -> sieht aus wie ein Hard-Crash

    print("   Vorher: Meeting status = recording, in_progress vorhanden")
    with session_scope() as s:
        report = recover(config, s)
        m = s.get(Meeting, mid)
    print(f"   Report: finalized={report.meetings_finalized} failed={report.meetings_failed} "
          f"jobs_reset={report.jobs_reset}")
    print(f"   Nachher: Meeting status = {m.status}, Dauer = {m.duration_s}s")
    print(f"   Original: {config.audio_dir / mid / 'original.wav'}")
    assert m.status == "ready"


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="ma_demo_"))
    print(f"Demo-Speicher: {base}")
    config = Config(base_dir=base)
    set_config(config)
    config.ensure_dirs()
    setup_logging("WARNING", config.log_path, to_stderr=False)
    try:
        demo_capture(config)
        demo_crash_recovery(config)
        rule("Fertig")
        print("Phase-1-Pipeline (Aufnahme + Crash-Speicherung + Recovery) funktioniert.")
        return 0
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
