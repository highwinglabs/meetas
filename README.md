# Meeting Assistant

A **local, privacy-first meeting assistant**. An independent Python core captures
microphone audio, stores it crash-safely in 1-second chunks, assembles an immutable
original recording, persists everything to SQLite (WAL mode), and exposes a local
REST API (bound to `127.0.0.1` only). It performs offline ASR transcription, full-text
search, grounded RAG questions, structured local-LLM analysis, and exports. A React
web UI is a pure client of that API and is served statically by the core itself.

> **Privacy by default.** All data stays on your machine. There is **no network
> access** (`network_allowed=false`) and **no cloud**. The API listens on loopback
> only. Model downloads are optional and always require an explicit confirmation.

---

## Features

- **Crash-safe recording** — 1-second WAV chunks, each fsync'd; the chunk index is the
  source of truth, so a hard crash loses at most ~1 second and an interrupted session
  is completed automatically on restart.
- **Immutable provenance** — the assembled `original.wav` is written atomically and
  made read-only; re-transcription keeps previous versions instead of overwriting.
- **Offline ASR** — local transcription (faster-whisper / Parakeet), auto language
  detection (de/en), selectable per meeting.
- **Local LLM analysis** — a strict, nine-section JSON analysis where every claim
  carries a transcript source. Nothing is invented; missing values are stored as
  "not specified".
- **Grounded search & RAG** — FTS5 full-text search plus a local, dependency-free
  relevance index for source-anchored questions.
- **Comfort features** — speaker diarization (opt-in), live transcription (opt-in),
  tasks, markers, tags, revisions, export, backup/restore.
- **Independent daemon** — the core survives a UI crash (double-fork + `setsid`).

---

## Quick start (Linux, ~3 minutes)

The web UI is **prebuilt** in `ui/dist`, so a normal install needs **no Node/npm**.
From a fresh clone, run the (reviewable) installer:

```bash
./install.sh
```

`install.sh` prints exactly what it will install and only adds what is missing:
the system packages `ffmpeg` + PortAudio, **Python 3.12 via `uv`**, and the locked
Python dependencies. It then initializes storage and starts the service. Open:

    http://127.0.0.1:8765/

| Flag | Effect |
|---|---|
| `--with-models` | Also download the live ASR model (network required) |
| `--no-start` | Install + initialize only; do not start the service |

ASR models are downloaded **on demand** from the UI (network + explicit confirmation);
by default nothing is downloaded. A local LLM server is optional — with
`MA_LLM_MOCK=true` the full pipeline runs without one.

**Developers** working from source (Node/npm, test suite, `npm run dev`) should use the
full [Installation](#installation) section instead.

---

## Requirements

| Component | Role | Notes |
|---|---|---|
| **Python 3.12** | Runtime | `uv` uses/provisions it automatically |
| **uv** | venv + dependencies + lockfile | https://docs.astral.sh/uv/ |
| **ffmpeg** | Assemble chunks into the original recording | External system binary |
| **PortAudio** (`libportaudio2`) | Microphone capture via `sounddevice` | External system library |
| **A local LLM server** (optional) | OpenAI-compatible `/v1` endpoint (e.g. llama.cpp, Ollama) | External, already running; the app never starts one |
| **Node.js + npm** | One-time build of the React UI (`ui/`) | Build-time only |

`ffmpeg`, PortAudio, a local LLM server, and Node are **external system components** —
they are not installed into the virtual environment. Node is needed only to build the
UI once; at runtime the core alone is sufficient.

### System packages (one-time, outside the venv)

On a fresh machine install the runtime system libraries first. Without them the app
still installs and runs, but recording assembly (`ffmpeg`) or microphone capture
(`PortAudio`) will be unavailable until you do:

| Package | Debian / Ubuntu | Fedora | macOS (Homebrew) |
|---|---|---|---|
| **ffmpeg** | `sudo apt install ffmpeg` | `sudo dnf install ffmpeg` | `brew install ffmpeg` |
| **PortAudio** | `sudo apt install libportaudio2` | `sudo dnf install portaudio` | `brew install portaudio` |
| **uv** | `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `sudo dnf install uv` | `brew install uv` |
| **Node.js** (UI build only) | `sudo apt install nodejs npm` | `sudo dnf install nodejs npm` | `brew install node` |

A local LLM server is **optional**: with `MA_LLM_MOCK=true` the full pipeline runs
without one (useful for trying the app out), and real analysis only needs a running
OpenAI-compatible server when you actually ask for it.

---

## Installation

All Python dependencies live **only** in `.venv/`; nothing is installed or removed
globally. The reproducible dependency list is in [`uv.lock`](./uv.lock), declared in
[`pyproject.toml`](./pyproject.toml).

```bash
# From the repository root:
uv venv .venv --python 3.12
uv sync                 # base dependencies
uv sync --extra diar    # + CPU-only PyTorch (optional diarization / VAD)
uv sync --extra dev     # + pytest, httpx (tests)
```

Activate the environment (or call the command directly with `.venv/bin/meeting-core`):

```bash
source .venv/bin/activate
meeting-core --version
```

---

## Configuration

Configuration is resolved with this precedence (**highest wins**):

1. Real environment variables (shell / systemd `EnvironmentFile`).
2. A `.env` file (in the project directory or in `MA_BASE_DIR`) — see `.env.example`.
3. `config.json` in `MA_BASE_DIR` (generated/edited at runtime).
4. Built-in defaults.

Start from the documented, credential-free template:

```bash
cp .env.example .env   # then edit the values you need
```

No credentials are required: the app is offline-first and never needs API keys,
tokens, or passwords. The most relevant settings:

| Variable | Default | Meaning |
|---|---|---|
| `MA_BASE_DIR` | `~/.local/share/meeting_assistant` | Where all data/audio/logs/state live |
| `MA_HOST` | `127.0.0.1` | API host (loopback; do not expose publicly) |
| `MA_PORT` | `8765` | API port |
| `MA_NETWORK_ALLOWED` | `false` | Allow network access (model downloads) |
| `MA_SAMPLE_RATE` / `MA_CHANNELS` | `16000` / `1` | Capture defaults |
| `MA_ASR_MODEL` | `small` | Final/batch ASR model |
| `MA_LIVE_ASR_MODEL` | `parakeet-tdt-0.6b-v3-int8` | Live ASR model (opt-in) |
| `MA_LIVE_TRANSCRIPTION` | `false` | Rolling-window live transcription |
| `MA_SPEAKER_DIARIZATION` | `false` | Local speaker diarization |
| `MA_LLM_BASE_URL` | `http://127.0.0.1:8081/v1` | Local OpenAI-compatible LLM server |
| `MA_OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1` | Ollama OpenAI-compatible endpoint |
| `MA_LLM_MODEL` | `qwen3.8-27b-q4kxl` | Must match a model id your server reports under `/v1/models` |
| `MA_LLM_MOCK` | `false` | Use the deterministic mock LLM (no server call) |
| `MA_EMBEDDINGS` / `MA_RAG` | `true` / `true` | Local relevance index / grounded RAG |
| `MA_AUTO_PIPELINE` / `MA_AUTO_ANALYZE` | `false` / `false` | Auto-processing after stop |

The complete, commented list is in [`.env.example`](./.env.example).

**Choosing a microphone:** set `input_device_name` to a case-insensitive substring of
your device name (e.g. `"headset"`), or `input_device_index` to an explicit PortAudio
index. Leave both unset to use the system default device:

```json
{ "input_device_name": "headset" }
```

**Model profiles:** `model_profiles` (in `config.json` or `MA_MODEL_PROFILES` JSON) lets
each role — `live` / `offline` / `sprecher` / `analyse` / `embeddings` / `reranking` —
point at a different local model/endpoint, overriding the main `MA_LLM_*` values.

---

## Running

The core is an **independent OS process** (double-fork + `setsid`) that survives a UI
crash. It binds to `127.0.0.1` only.

```bash
meeting-core init        # create storage + run Alembic migrations + recovery
meeting-core devices     # list input devices
meeting-core serve       # run in the foreground
meeting-core daemon      # run as an independent daemon
meeting-core status      # show status
meeting-core stop        # stop the daemon
```

A single instance is enforced by a lock + PID file under `<MA_BASE_DIR>/state/`; a
second start fails while the first is running.

### Start automatically (systemd `--user`)

A ready unit ships in `packaging/systemd/meeting-assistant.service`. It starts the
core under systemd, still binds only to `127.0.0.1`, and restarts on failure:

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/meeting-assistant.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now meeting-assistant.service
```

**Migrations on existing data:** `init`/`serve`/`daemon` always run Alembic. An existing
database is **never deleted or overwritten**. If a DB has the base tables but an empty
`alembic_version` (older/locally-created DBs), the initial migration is stamped (not
re-run) and only the missing migrations up to `head` are applied. Fresh and already
migrated databases behave normally.

---

## Web UI

The UI is a **React + TypeScript + Vite** single-page app under `ui/`. It is a pure
client of the REST API and is **served statically by the core** — no separate process,
no cross-origin, no cloud. With the core running on `http://127.0.0.1:8765`, the UI is
available at `http://127.0.0.1:8765/`.

```bash
cd ui
npm install        # once
npm run build      # production build to ui/dist (served by the core)
npm run dev        # optional: dev server with live reload (:5173, API proxied to core)
```

Notes:
- Without a built `ui/dist/`, the core is a **pure REST API** (`GET /` returns 404 and
  all endpoints still work). There is deliberately **no catch-all** route, so REST routes
  can never be shadowed; only `GET /` (`index.html`) and `GET /assets/*` are registered.
- Build path: `<project>/ui/dist` (override with `MA_UI_DIST`).
- The UI stores nothing itself; all state lives in the core (SQLite + filesystem).

## Language (de/en)

There are **three independent language axes** — do not conflate them:

1. **Interface language** (what you read in the UI) — German (default) or English,
   switchable in the UI (persisted in the browser). It controls labels, status text,
   dates, and messages. The UI also sends it as an `Accept-Language` header, so the
   core localises **error messages** and the **consent notice** to match. With no
   header (e.g. plain `curl` / other clients) the core answers in German.
2. **Transcription language** (what ASR recognises) — auto-detected per meeting
   (de/en), or forced globally via `MA_ASR_LANGUAGE` or per meeting in the UI.
3. **Analysis language** (what the LLM writes) — chosen per meeting
   (`analysis.output_lang`), independent of the other two.

Switching the interface language never changes what is transcribed or analysed; it
only changes how the app is *displayed* and how the core phrases its messages to you.
Exports are written in the **analysis/transcription language** of the meeting, not in
the interface language.

---

## Typical workflow

1. **Consent** — acknowledge the privacy notice once (`POST /consent/ack`), required
   before the first recording.
2. **Record** — start a meeting (source: microphone or, if enabled, system audio),
   pause/resume as needed, then stop.
3. **Transcribe** — run offline ASR (model download is confirmation-gated if needed).
4. **Optional stages** — speaker diarization, local embeddings.
5. **Analyze** — trigger a structured local-LLM analysis (9 sections, source-anchored).
6. **Search & ask** — full-text search and grounded RAG questions over the transcript.
7. **Export / backup** — Markdown/TXT/JSON/HTML (PDF/DOCX with clean fallback), and
   consistent local backups.

CLI equivalents:

```bash
meeting-core transcribe <id>            # offline transcription
meeting-core diarize <id>               # local speaker diarization
meeting-core analyze <id> --mock        # analysis (mock LLM for development)
meeting-core search "budget"            # full-text search
meeting-core export <id> --format markdown
```

---

## REST API (excerpt, `127.0.0.1` only)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Status, ffmpeg, version |
| `GET` | `/devices` | Input devices |
| `GET` / `POST` | `/consent` · `/consent/ack` | Consent text + status · acknowledge |
| `POST` | `/meetings` | Start a meeting (409 without consent / one already running) |
| `GET` | `/meetings` · `/meetings/{id}` | List · details (recording, segments, jobs, analyses) |
| `POST` | `/meetings/{id}/pause` · `resume` · `stop` | Control |
| `POST` | `/meetings/{id}/transcribe` | Transcribe (offline) |
| `POST` | `/models/asr/download` | Download an ASR model (confirmed, local) |
| `POST` | `/meetings/{id}/analyze` | LLM analysis (503 if server busy/absent) |
| `POST` | `/meetings/{id}/diarize` | Local speaker diarization (400 without transcript) |
| `GET` | `/search?q=` | Full-text search (FTS5) |
| `GET` | `/ask?q=` · `POST` `/chat` | Grounded RAG answers / source-anchored questions |
| `GET` | `/tasks` · `/tasks/overview` | Central tasks |
| `POST` | `/meetings/{id}/export` | Export (md/txt/json/html; pdf/docx with fallback) |
| `POST` | `/backup` · `GET` `/backup` · `POST` `/backup/restore` | Backup, list, restore (dry-run + confirm) |
| `GET` | `/models/providers` | Resolved local provider per role (no request) |

Example:

```bash
curl -s localhost:8765/health
curl -s -X POST localhost:8765/consent/ack -d '{"acknowledged": true}'
curl -s -X POST localhost:8765/meetings -d '{"title":"Standup"}'
```

The full endpoint list is in the API layer (`core/api/rest.py`) and the request/response
shapes in `core/api/schemas.py`.

---

## LLM analysis (local)

The assistant uses **an already-running local** LLM server with an OpenAI-compatible
`/v1` interface (e.g. llama.cpp). It is **not** started by the app and **no second LLM
is loaded** — it is an external process like ffmpeg, reachable only over loopback. A
real analysis runs **only** when you trigger one. In **development and tests**, no real
LLM call is ever made (a mock engine or `httpx.MockTransport` is used; `MA_LLM_MOCK=true`
forces the mock).

**Busy server:** the client waits patiently on HTTP 429/503 — up to
`MA_LLM_MAX_BUSY_RETRIES` retries with `MA_LLM_BUSY_WAIT` between them, capped at an
overall wall-clock budget of `MA_LLM_BUSY_BUDGET` seconds (default 600) — and otherwise
returns a clear error (API `503`). An absent server also yields `503` with a clear message.

**Structured output:** the analysis is a **required JSON** with exactly nine sections —
`kurzfassung`, `themen`, `entscheidungen`, `aufgaben`, `offene_fragen`,
`naechste_schritte`, `risiken`, `wichtige_fakten`, `follow_ups` (empty sections as `[]`).
Each entry needs a `text` and at least one `quelle` with `segment_id` / `sprecher` /
`timestamp`, so nothing is asserted without a transcript anchor. Missing owners/deadlines
are stored verbatim as "not specified" — **never invented**. The JSON is strictly
validated; if it is invalid/incomplete the model corrects itself once, and if the second
attempt still fails the analysis aborts with a clear message and **nothing is stored**.

---

## Storage layout

```
~/.local/share/meeting_assistant/
├── config.json
├── data/meeting_assistant.db        # SQLite (WAL), mode 0600
├── audio/<meeting_id>/
│   ├── chunks/chunk_NNNNNN.wav      # 1-second chunks
│   ├── chunks.index.jsonl           # authoritative index (fsync per line)
│   ├── events.jsonl                 # pause/resume/stop events
│   ├── meta.json
│   ├── original.wav                 # assembled atomically, read-only (0444)
│   └── in_progress                  # marker while recording
├── exports/
├── logs/core.log
└── state/{core.lock, core.pid}
```

Data, state, audio, logs, models, and export directories are created with `0700` and
the database with `0600`, so they are inaccessible to other local users.

**Crash safety:** the index is the truth. Incomplete chunks (without an index line) are
discarded on recovery — at most ~1 s is lost, and an interrupted recording is completed
automatically on restart.

**Sample rate:** the microphone is captured at the device's **native** rate (many ALSA
devices cannot open 16 kHz). `original.wav` stays high-fidelity at the native rate;
`original_16k.wav` is the auto-generated copy used for ASR.

---

## Privacy, security & data protection

- **Local-first:** the API binds to `127.0.0.1`; the app has **no** telemetry,
  analytics, or cloud module, and **no** network by default.
- **Network gating:** `network_allowed=false` by default. Any model download requires an
  explicit confirmation (`downloads_require_confirmation=true`). The only HTTP egress is
  the local LLM (loopback) and the CLI health check (loopback).
- **Credentials:** none are required or stored by the app. The optional keyring-backed
  secret store (with a `0600` file fallback) never writes secrets to logs or the DB, and
  log output redacts known secret patterns.
- **Upload limits:** hard caps on direct and resumable upload sizes prevent a single
  request from exhausting memory/disk.
- **Least privilege:** data/state/audio/logs directories use `0700` and the database
  `0600`; the systemd unit runs with `UMask=0077`.
- **Consent:** recording is gated behind a one-time, persisted privacy notice.
- **No catch-all routing:** the static UI never shadows REST routes.

> If you need to expose the API beyond loopback (not recommended), you must add your own
> authentication and a reverse proxy with TLS; the app intentionally ships no
> authentication because it is designed to be reachable only on the local machine.

---

## Testing

The test suite is **headless and offline**: capture uses synthetic/file sources (never a
real microphone), and ASR and LLM run on mocks or `httpx.MockTransport` (never a real
model or LLM call).

```bash
uv sync --extra dev     # once
uv run pytest -q
```

Coverage includes: chunk storage, pause/resume, crash recovery, DB persistence
(WAL, FK cascade, permissions), devices, daemon lock, REST API, ASR transcription,
FTS search, export, and LLM analysis including busy/error paths — plus the static UI
serving (`GET /` + `/assets/*`) **without** shadowing REST routes, live transcription,
local diarization, the rolling-window pipeline, and backup/restore (dry-run, safety
backup, retention).

For UI changes, run the TypeScript build:

```bash
cd ui && npm run build
```

---

## License

Distributed under the **MIT License** — see [LICENSE](./LICENSE).
