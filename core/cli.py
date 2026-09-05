"""Command-line interface.

Commands:
  init      Create storage dirs + DB (migrations) + run recovery.
  serve     Run the core in the foreground (used by Tauri to spawn it).
  daemon    Start the core as a detached, independent process (survives UI).
  stop      Gracefully stop the daemon (finalizes the active recording).
  status    Show PID + health.
  restart   Stop + daemon.
  recovery  Run crash recovery against the existing DB.
  devices   List available input devices.
  models    Show ASR model status.
  download-model  Download the ASR model (network + confirmation required).
  transcribe  Transcribe a meeting (offline, local ASR).
  search      Full-text search over transcripts.
  export      Export a meeting to markdown / txt / json / html / pdf / docx.
   llm         Show LLM status (local, no request).
   analyze     Analyze a meeting with the local LLM (real or --mock).
   diarize     Assign a speaker per transcript segment (local, offline).
   features    Show Phase-5 feature flags (live transcription / diarization).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import urllib.request

from core import __version__
from core.config import Config, get_config
from core.daemon import (
    AlreadyRunningError, acquire_lock, daemonize, graceful_stop, pid_alive,
    read_pid, release_lock, wait_healthy,
)
from core.logging_setup import setup_logging


def _cfg() -> Config:
    return get_config()


def _print_health(config: Config) -> dict | None:
    try:
        host = config.host if config.host in {"127.0.0.1", "localhost"} else "127.0.0.1"
        with urllib.request.urlopen(
            f"http://{host}:{config.port}/health", timeout=2
        ) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def cmd_init(args) -> int:
    config = _cfg()
    from core.bootstrap import bootstrap
    setup_logging(config.log_level, to_stderr=True)
    bootstrap(config, use_migrations=not args.no_migrations)
    print(f"Initialized under: {config.base_dir}")
    print(f"DB: {config.db_path}")
    return 0


def cmd_serve(args) -> int:
    from core.api.app import run_server
    run_server(_cfg(), use_migrations=not args.no_migrations)
    return 0


def cmd_daemon(args) -> int:
    config = _cfg()
    pid = read_pid(config)
    if pid and pid_alive(pid):
        print(f"Already running: PID {pid}")
        return 0
    # The grandchild's stderr goes to a log file, otherwise a late crash
    # (e.g. after double-fork) dies silently with /dev/null as stderr.
    daemonize(stderr_to=config.logs_dir / "daemon.err.log")
    from core.api.app import run_server
    run_server(config, use_migrations=not getattr(args, "no_migrations", False))
    return 0


def cmd_stop(args) -> int:
    config = _cfg()
    ok = graceful_stop(config)
    print("Stopped." if ok else "Stop could not be confirmed (timeout).")
    return 0 if ok else 1


def cmd_status(args) -> int:
    config = _cfg()
    pid = read_pid(config)
    alive = pid is not None and pid_alive(pid)
    health = _print_health(config)
    print(json.dumps({"pid": pid, "alive": alive, "health": health}, indent=2))
    return 0 if alive else 1


def cmd_restart(args) -> int:
    config = _cfg()
    graceful_stop(config)
    time.sleep(0.3)
    return cmd_daemon(args)


def cmd_recovery(args) -> int:
    from core.bootstrap import bootstrap
    config = _cfg()
    setup_logging(config.log_level, to_stderr=True)
    bootstrap(config, use_migrations=not args.no_migrations)
    print("Recovery run.")
    return 0


def cmd_devices(args) -> int:
    from core.audio.devices import list_input_devices
    devs = list_input_devices()
    if not devs:
        print("No input devices found (no audio subsystem / headless).")
        return 0
    for d in devs:
        marker = " (default)" if d.is_default else ""
        print(f"[{d.id}] {d.name}{marker}")
    return 0


def _service() -> "MeetingService":
    from core.bootstrap import bootstrap
    config = _cfg()
    setup_logging(config.log_level, to_stderr=True)
    return bootstrap(config, use_migrations=False, run_recovery=True)


def cmd_models(args) -> int:
    svc = _service()
    st = svc.asr_model_status(args.model)
    print(json.dumps(st, indent=2, ensure_ascii=False))
    print("Ready." if st["ready"] else "Model not downloaded (see 'download-model').")
    return 0


def cmd_download_model(args) -> int:
    svc = _service()
    try:
        out = svc.download_asr_model(confirm=args.confirm, model_name=args.model)
    except Exception as exc:
        print(f"Download failed: {exc}")
        return 1
    print(f"Model '{out['name']}' ready: {out['ready']}")
    return 0 if out["ready"] else 1


def cmd_transcribe(args) -> int:
    svc = _service()
    try:
        out = svc.transcribe(args.meeting_id, language=args.lang,
                             model_name=args.model, allow_download=args.download)
    except Exception as exc:
        print(f"Transcription failed: {exc}")
        return 1
    print(f"Transcript: {out['segments']} segments, language={out['language']} "
          f"(FTS rows={out['fts_rows']})")
    return 0


def cmd_search(args) -> int:
    svc = _service()
    results = svc.search(args.query, limit=args.limit)
    if not results:
        print("No results.")
        return 0
    for r in results:
        ts = f"{int(r['start_s'] // 60):02d}:{int(r['start_s'] % 60):02d}" if r["start_s"] is not None else "--:--"
        print(f"- [{ts}] {r['meeting_title']}: {r['snippet']} (seg:{r['segment_id']})")
    return 0


def cmd_export(args) -> int:
    svc = _service()
    out = svc.export_meeting(args.meeting_id, fmt=args.format)
    print(f"Exported to: {out['path']} ({out['size']} bytes)")
    return 0


def cmd_llm(args) -> int:
    if getattr(args, "mock", False):
        os.environ["MA_LLM_MOCK"] = "1"
    svc = _service()
    st = svc.llm_status()
    print(json.dumps(st, indent=2, ensure_ascii=False))
    return 0


def cmd_analyze(args) -> int:
    if getattr(args, "mock", False):
        os.environ["MA_LLM_MOCK"] = "1"
    svc = _service()
    try:
        out = svc.analyze(args.meeting_id, kind=args.kind, override_system=args.system)
    except Exception as exc:
        print(f"Analysis failed: {exc}")
        return 1
    print(f"Analysis saved (model={out['model']}, {len(out['content'])} chars).")
    print("-" * 60)
    print(out.get("markdown") or out["content"])
    return 0


def cmd_diarize(args) -> int:
    svc = _service()
    try:
        out = svc.diarize(args.meeting_id)
    except Exception as exc:
        print(f"Speaker detection failed: {exc}")
        return 1
    print(f"Speakers: {out['speakers']} detected across {out['segments']} segments "
          f"(engine={out['engine']}).")
    return 0


def cmd_features(args) -> int:
    svc = _service()
    print(json.dumps(svc.feature_flags(), indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meeting-core", description="Meeting Assistant Core (local, privacy-first)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--no-migrations", action="store_true",
                        help="use create_all instead of alembic migrations")

    for name, fn, help_ in [
        ("init", cmd_init, "Initialize storage + database"),
        ("serve", cmd_serve, "Run in the foreground"),
        ("daemon", cmd_daemon, "Start as an independent daemon"),
        ("stop", cmd_stop, "Stop the daemon"),
        ("status", cmd_status, "Show status"),
        ("restart", cmd_restart, "Restart"),
        ("recovery", cmd_recovery, "Run crash recovery"),
        ("devices", cmd_devices, "List input devices"),
        ("models", cmd_models, "Show ASR model status"),
        ("download-model", cmd_download_model, "Download the ASR model"),
        ("transcribe", cmd_transcribe, "Transcribe a meeting (offline)"),
        ("search", cmd_search, "Full-text search over transcripts"),
        ("export", cmd_export, "Export a meeting (md/txt/json/html/pdf/docx)"),
        ("llm", cmd_llm, "Show LLM status (local, no request)"),
        ("analyze", cmd_analyze, "Analyze a meeting with the LLM (local)"),
        ("diarize", cmd_diarize, "Detect speakers per segment (local, offline)"),
        ("features", cmd_features, "Show Phase-5 features (live/diarization)"),
    ]:
        sp = sub.add_parser(name, help=help_)
        if name in ("init", "serve", "daemon", "restart", "recovery"):
            add_common(sp)
        if name == "models":
            sp.add_argument("--model", default=None, help="Model size (e.g. small, medium)")
        if name == "download-model":
            sp.add_argument("--confirm", action="store_true",
                            help="Explicitly confirm the download (required)")
            sp.add_argument("--model", default=None)
        if name == "transcribe":
            sp.add_argument("meeting_id")
            sp.add_argument("--lang", default=None, help="e.g. de, en (empty = auto)")
            sp.add_argument("--model", default=None)
            sp.add_argument("--download", action="store_true",
                            help="Download the model if needed (requires network)")
        if name == "search":
            sp.add_argument("query")
            sp.add_argument("--limit", type=int, default=25)
        if name == "export":
            sp.add_argument("meeting_id")
            sp.add_argument("--format", default="markdown",
                            choices=["markdown", "txt", "json", "html", "pdf", "docx"])
        if name == "llm":
            sp.add_argument("--mock", action="store_true",
                            help="Use the deterministic mock LLM (no request)")
        if name == "analyze":
            sp.add_argument("meeting_id")
            sp.add_argument("--kind", default="summary",
                            choices=["summary", "action_items"])
            sp.add_argument("--system", default=None,
                            help="Override the system instruction")
            sp.add_argument("--mock", action="store_true",
                            help="Use the deterministic mock LLM (no request)")
        if name == "diarize":
            sp.add_argument("meeting_id")
        sp.set_defaults(func=fn)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AlreadyRunningError as exc:
        print(str(exc))
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
