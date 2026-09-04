#!/usr/bin/env python3
"""Echter End-to-End-Beweis (lokal):

  Aufnahme vom Mikrofon  ->  ffmpeg-Assembly  ->  echtes faster-whisper-Modell
  ->  Segmente in SQLite  ->  FTS5-Suche  ->  Export (Markdown)

Das Modell muss bereits heruntergeladen sein (siehe README / CLI
`download-model --confirm`). Beim Transkribieren wird NICHT ins Netz
verbunden (local_files_only), das Modell liegt im lokalen Cache.

Empfohlene Nutzung (du kontrollierst den Sprech-Timing selbst):

    .venv/bin/python scripts/mic_transcribe_demo.py --wait --model tiny --lang de

Oder automatisch mit Vorlauf (ich/du startet es, dann sofort sprechen):

    .venv/bin/python scripts/mic_transcribe_demo.py --lead 4 --capture 8 --model tiny
"""
from __future__ import annotations

import argparse
import sys
import time

from core.bootstrap import bootstrap
from core.config import Config
from core.store.db import session_scope
from core.store.fts import search as fts_search
from core.store.models import TranscriptSegment
from sqlalchemy import select


def main() -> int:
    ap = argparse.ArgumentParser(description="Lokaler ASR End-to-End-Beweis")
    ap.add_argument("--wait", action="store_true",
                    help="interaktiv: auf ENTER warten, bevor aufgenommen wird")
    ap.add_argument("--lead", type=float, default=4.0,
                    help="Vorlauf in Sekunden (nur ohne --wait)")
    ap.add_argument("--capture", type=float, default=8.0,
                    help="Aufnahmedauer in Sekunden")
    ap.add_argument("--model", default=None, help="ASR-Modell (z.B. tiny)")
    ap.add_argument("--lang", default=None, help="Sprache erzwingen (z.B. de)")
    ap.add_argument("--device", default=None, help="Eingabegerät-Teilname (z.B. headset)")
    ap.add_argument("--title", default="Live-Test", help="Meeting-Titel")
    args = ap.parse_args()

    cfg = Config.load()
    cfg.input_device_name = args.device or cfg.input_device_name
    if args.model:
        cfg.asr_model = args.model

    print("Modell:", cfg.asr_model, "| compute:", cfg.asr_compute_type)
    print("Eingabegerät (Name-Teil):", cfg.input_device_name)
    print("Base-Dir:", cfg.base_dir, flush=True)

    svc = bootstrap(cfg, use_migrations=False, run_recovery=True)

    st = svc.asr_model_status(cfg.asr_model)
    if not st["ready"]:
        print("\nFEHLER: Modell nicht vorhanden. Erst laden (explizit bestätigen):")
        print(f"  MA_NETWORK_ALLOWED=1 MA_ASR_MODEL={cfg.asr_model} "
              f"meeting-core download-model --confirm")
        return 1
    print("Modell bereit:", st["name"], "| Größe-Hinweis:", st.get("size"), flush=True)

    svc.record_consent(True)
    mid = svc.start_meeting(title=args.title, source="mic")
    print("Meeting gestartet:", mid, flush=True)

    if args.wait:
        print("\nBereit. Sprich jetzt in das Mikrofon, dann ENTER für den Start.")
        input(">>> ENTER drücken, um die Aufnahme zu starten >>>")
    else:
        for i in range(max(1, int(args.lead)), 0, -1):
            print(f"... Starte in {i}s – BITTE JETZT ANFANGEN ZU SPROCHEN ...", flush=True)
            time.sleep(1)

    print(f"\n<<< Aufnahme läuft für {args.capture:.0f}s – SPRICH JETZT! >>>", flush=True)
    time.sleep(args.capture)

    svc._sessions[mid].wait_done(timeout=15)
    svc.stop(mid)
    print("Aufnahme gespeichert & assembliert.", flush=True)

    out = svc.transcribe(mid, language=args.lang)
    print("\n== Transkription (Modell: %s) ==" % out["model"], flush=True)
    print("Status:", out["status"], "| Segmente:", out["segments"],
          "| FTS-Zeilen:", out["fts_rows"], "| Sprache:", out["language"], flush=True)

    with session_scope() as s:
        segs = s.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.meeting_id == mid)
            .order_by(TranscriptSegment.start_s)).all()

    if segs:
        print("\n== Transkript ==")
        for seg in segs:
            print(f"[{seg.start_s:7.3f} -> {seg.end_s:7.3f}] {seg.text}")
        first_word = (segs[0].text or "").split()
        if first_word:
            q = first_word[0]
            print(f"\n== FTS5-Volltextsuche nach '{q}' ==")
            for r in svc.search(q, limit=5):
                print(f"  seg={r['segment_id']}  t={r['start_s']}s  «{r['snippet']}»")
    else:
        print("\nKeine Segmente erkannt (VAD). Lauter/klarer in das Mikrofon "
              "sprechen und erneut laufen lassen.")

    exp = svc.export_meeting(mid, fmt="markdown")
    print("\n== Export (Markdown) ==")
    print("Pfad:", exp["path"], f"({exp['size']} Bytes)")
    print(exp["content"][:1500])
    print("\nFertig. Alle Daten bleiben lokal unter:", cfg.base_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
