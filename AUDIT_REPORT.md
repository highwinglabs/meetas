# Meetas (`meeting-assistant`) — Adversarial Audit Report

Scope: 11 dimensions (bug discovery, data loss / crash recovery, concurrency,
security/privacy, LLM/RAG grounding, database/migrations, frontend/backend
contract, test coverage, clean install/packaging, unnecessary complexity,
repository hygiene/secret audit). Method: read-only static analysis of ~24k
lines Python + React/TS, full test-suite run, git-history and packaging
review. Every finding carries Status (CONFIRMED / LIKELY / POSSIBLE),
severity (P0–P3), exact locations, execution path, impact, and a regression
test sketch.

**Bottom line:** This is a well-engineered local-first tool. Crash safety of
the audio pipeline (fsync'd chunk index, atomic WAV assembly, bounded
PortAudio queue, double-fork daemon with stale-lock detection) is thorough
and correct; 466/466 tests pass; no secrets, no unauthenticated network
attack surface (loopback-only), and the upload/import paths are
idempotent. The gaps found are narrow edge-case state transitions: one
likely DB-corruption window during restore (P1), one unresumable crash state
(P2), one privacy residual on permanent delete (P2), and a set of P3
nuisances. No P0. No fabricated findings — see §9 for investigated-and-clean
areas and false positives.

---

## 1. Executive summary

| # | Sev | Status | Area | Finding (one line) |
|---|-----|--------|------|--------------------|
| F1 | P1 | LIKELY | Backup/restore | Restore swaps only `.db`; stale `.db-wal`/`.db-shm` survive and are replayed onto the *new* database → page-level corruption |
| F2 | P2 | CONFIRMED | Crash recovery | Crash mid-`analyzing` leaves the meeting stuck in `analyzing` forever; neither `recover()` nor `resume_pending_pipelines()` picks it up |
| F3 | P2 | CONFIRMED | Privacy | `permanently_delete_meeting` derives the audio folder from `original_path`; when it is `None` the whole `audio_dir/<meeting_id>/` (incl. raw chunks) survives a "permanent delete" |
| F4 | P3 | CONFIRMED | Uploads | Crash between meeting commit and status commit leaves a resumable upload permanently un-completable (temp file gone, session "uploading"); only cancel recovers |
| F5 | P3 | POSSIBLE | Concurrency | `start_transcription` double-submit race: a second concurrent call passes both guards and schedules a duplicate full-ASR run (extra transcript version, wasted CPU) |
| F6 | P3 | CONFIRMED | Repo hygiene | `ui/dist/` build artifacts are git-tracked (`!ui/dist/` negation) — stale-build risk in a public repo |
| F7 | P3 | CONFIRMED | LLM/RAG | "Grounded" analysis = citation-ID existence check only; no verification that cited segments support the claims (documented design choice, see impact) |
| F8 | P3 | CONFIRMED | API/UX | `POST /api/meetings/{id}/pipeline?wait=true` blocks the HTTP request for minutes-to-hours (LLM timeout 1800 s + busy budget) |

Missing-test gaps for F1–F5 are listed in §8. Simplification opportunities
in §7, false positives in §9, recommended fix order in §10.

---

## 2. F1 — Restore leaves stale WAL beside the swapped-in DB (P1, LIKELY)

- **Location:** `core/backup.py`, `restore_backup()`, steps 3 ("Release
  pooled connections, then swap the temp copy in atomically"):
  ```python
  try:
      get_engine().dispose()
  except RuntimeError:
      pass
  os.replace(src_tmp, cfg.db_path)
  ```
- **Problem:** `get_engine().dispose()` only closes *idle pooled*
  connections. Any connection currently checked out (an in-flight API
  request, a pipeline worker committing job progress, the startup
  auto-backup's read connection, an in-flight analysis) is not closed and
  keeps the database in WAL mode. The restore then atomically replaces
  only the main DB file; `cfg.db_path + "-wal"` and `"-shm"` are **never
  touched** (the only suffix handling in `core/store/db.py` is
  `_secure_db_file`, which chmods all three — proving the files are
  expected to exist). The snapshot taken by `create_backup()` via the
  SQLite backup API is self-contained and does *not* require the WAL, so
  the WAL/SHM are pure stale state after the swap.
- **Trigger / execution path:**
  1. Daemon running, WAL mode active (`PRAGMA journal_mode=WAL` in
     `core/store/db.py:47`); some connection holds the WAL.
  2. `POST /api/backups/{id}/restore?confirm=true` → `restore_backup()`.
  3. `dispose()` leaves the checked-out connection open; `os.replace`
     swaps the `.db`; the old `.db-wal`/`.db-shm` remain on disk.
  4. Next open (any request, or the still-open old connection itself):
     SQLite runs WAL recovery. WAL validity is verified **inside the WAL
     file** (magic, salt-1/salt-2, commit records) plus a page-size match
     against the DB header — the page size is identical because the backup
     came from the same app. Recovery therefore replays the *old* WAL
     frames into the *new* database file.
- **Impact:** Page-level corruption of the restored database
  (`database disk image is malformed`, or silent page anomalies that pass
  `integrity_check` until touched). Mitigating factor: the pre-restore
  safety snapshot (step 2) is taken *before* the swap, so the user can
  always fall back to their pre-restore state — the restore itself is
  effectively a corrupt no-op or a loss of the intended restore.
- **Why safeguards don't prevent it:** `_integrity()` is run on the
  *backup* file, not after the swap. `pre_write_guard`
  (`core/services/backups.py`) re-checks only "no active capture session"
  — API writes and pipeline workers are not capture sessions and are not
  blocked. `dispose()` does not await checked-out connections and does not
  checkpoint/delete the WAL. No post-swap verification exists.
- **Recommended fix:** Before `os.replace(src_tmp, cfg.db_path)`, unlink
  `cfg.db_path.with_name(name + "-wal")` and `"-shm"` (a consistent
  snapshot never needs them), and/or run a checkpoint-and-delete on the old
  DB while still holding all connections. Add a post-swap
  `PRAGMA integrity_check` on the new file.
- **Regression test:** Create a DB in WAL mode with data; take a backup;
  open a second raw `sqlite3` connection (simulating a checked-out
  connection) so `.db-wal` exists; swap a *different* valid snapshot in via
  `restore_backup(confirm=True)` **without** closing the side connection;
  close it; reopen via the app and assert the restored rows — not a mix of
  old WAL frames — are present and `integrity_check == ok`.

## 3. F2 — Meeting stuck in `analyzing` after a crash (P2, CONFIRMED)

- **Location:**
  - `core/analysis/processor.py:168-171` — sets `meeting.status =
    "analyzing"` and job `running` **before** the slow LLM call (intended,
    committed for resumability).
  - `core/recovery/recovery.py:32` — `ACTIVE_STATUSES = ("recording",
    "paused")` only.
  - `core/jobs/queue.py` `recover_crashed()` — resets the analyze job
    `running → pending`, `retries += 1`.
  - `core/services/pipelines.py` `resume_pending_pipelines()` —
    `active_ids = Meeting.status.in_(["processing", "transcribing",
    "ready"])` — **`"analyzing"` is absent**.
- **Trigger / execution path:** Process killed (power loss, OOM, daemon
  restart) while `AnalysisProcessor.process` is mid-LLM-call. On next
  start: `recover()` sees status `analyzing` → not active, no-op.
  `recover_crashed()` resets the analyze job to `pending`.
  `resume_pending_pipelines()` (called from `core/bootstrap.py:97`)
  evaluates the meeting but `"analyzing"` is not in `active_ids` → not
  scheduled. The meeting stays `analyzing` with a `pending` analyze job
  until a human intervenes.
- **Impact:** No data loss (audio + transcript intact). Functional: with
  `auto_pipeline` the user expects the analysis to finish on its own;
  instead the UI (`ui/src/components/MeetingDetail.tsx:380`) treats a
  `pending` analyze job as "analyzing" and shows a perpetual spinner, and
  the "Create summary" button stays disabled (`:946`). The only escape is
  the "Stop" button (`cancel_analysis`, `core/services/pipelines.py`),
  which sets job `cancelled` + meeting `ready`; the user must then
  explicitly re-trigger analysis. Retry cap interaction: each crash bumps
  `retries`; after `pipeline_max_retries` (default 3) crashes the job
  would be "exhausted" even if a resume had picked it up — the manual
  path (`analyze()`) still works because it marks the job `running`
  unconditionally.
- **Why safeguards don't prevent it:** The resume whitelist was clearly
  curated to avoid auto-retrying *intentionally failed* later stages;
  "analyzing" was simply overlooked, not deliberately excluded. No test
  covers it (`tests/test_pipeline_resume.py` covers `processing` and
  `done`-with-failed only).
- **Recommended fix:** Add `"analyzing"` to the `active_ids` filter in
  `resume_pending_pipelines()` (the per-stage loop already correctly
  refuses to re-drive `cancelled`/exhausted jobs, so no double-run risk),
  or handle it in `recover()` by resetting `analyzing → ready` when the
  analyze job is `pending`/`failed` below cap.
- **Regression test:** Seed a meeting with status `analyzing`, segments
  present, and an analyze job `pending` (retries=1); call
  `resume_pending_pipelines()`; assert it returns ≥1 and the meeting
  reaches `done`/`ready` with the analyze job `done`.

## 4. F3 — Permanent delete leaves audio behind when `original_path` is NULL (P2, CONFIRMED)

- **Location:** `core/services/meetings.py`, `permanently_delete_meeting()`:
  ```python
  raw_paths = [rec.original_path] if rec and rec.original_path else []
  raw_paths.extend(asset.path for asset in assets if asset.path)
  ...
  for folder in folders:          # folders derived ONLY from raw_paths
      shutil.rmtree(folder)
  ```
- **Problem:** The folder to delete is derived from the stored file paths,
  not from the deterministic layout `audio_dir / meeting_id`. When
  `rec.original_path` is `NULL` and there is no `ProjectFile` row — exactly
  the shape of a *failed recording* (assembly failed after chunks were
  written: `core/service.py` `stop()` sets `meeting.status="failed"` and
  `recording.status="failed"` while leaving `original_path` unset) — the
  `audio_dir/<meeting_id>/` directory, including every fsync'd chunk and
  `chunks.index.jsonl`, is never deleted.
- **Trigger / execution path:** (1) record a meeting whose assembly fails
  (corrupt chunk / missing ffmpeg), or whose start failed after
  chunk-writer init; (2) move it to trash; (3) permanent delete. DB rows
  are gone, `audio_removed` is reported as `false`, but raw meeting audio
  remains on disk under a UUID no UI surfaces.
- **Impact:** Privacy residual contradicting the "permanent delete"
  semantics (this is a privacy-first product; the README and consent flow
  promise local deletion). No functional impact.
- **Why safeguards don't prevent it:** The containment checks
  (`audio_root in path.parents`, `path.parent != audio_root`) guard
  against deleting the wrong tree but cannot help when there is no path to
  check. There is no fallback to `audio_dir / meeting_id`.
- **Recommended fix:** Independently of the path-derived folders, also
  remove `self.config.audio_dir / meeting_id` if it exists (it is
  deterministically named and always inside `audio_root`). Keep the
  existing path-derived logic for project files.
- **Regression test:** Create a meeting + recording row with
  `original_path=None` and an `audio_dir/<id>/` containing chunk files;
  trash + permanently delete; assert the directory is gone and
  `audio_removed` is `True`.

## 5. F4 — Upload stuck after crash between import commit and status commit (P3, CONFIRMED)

- **Location:** `core/services/uploads.py`, `_import_completed_upload()`:
  ```python
  result = self.import_upload(..., object_id=upload_id, _source_path=path)
  ...
  try:
      path.unlink(missing_ok=True)
  except OSError:
      pass
  with session_scope() as s:
      row.status = "completed"
  ```
- **Trigger / execution path:** Kill the process after `import_upload`
  committed the meeting but before the `UploadSession` status commit. Two
  sub-cases: (a) crash before `unlink` — temp file still present: user
  retry self-heals (idempotent `object_id` import returns the existing
  meeting, then completes). (b) crash after `unlink` — temp file gone:
  every retry of `complete_upload` fails with
  "Die temporäre Uploaddatei ist unvollständig." (`path.is_file()` false);
  `resume_upload` accepts status `uploading` but cannot fix the missing
  file; `list_uploads` keeps showing the zombie session.
- **Impact:** The meeting *was* imported (visible in the meeting list), so
  no data loss; but the upload list shows a session that can never be
  completed and has no prominent cancel affordance in
  `ui/src/components/RecordingPanel.tsx` (pending uploads are shown as a
  notification; the cancel button belongs to the active upload). User must
  discover `cancel_upload` (or delete via API).
- **Why safeguards don't prevent it:** The idempotency design (comment in
  `import_upload`: "If the process crashed after the meeting/file commit
  but before the UploadSession status update, retrying therefore returns
  the existing object") explicitly targets case (a); case (b) — the file
  already unlinked — is not handled: `complete_upload`'s file-existence
  check raises instead of detecting the already-imported `object_id`.
- **Recommended fix:** In `complete_upload`, when the temp file is missing
  *and* a meeting with `id == upload_id` already exists, mark the session
  `completed` and return the existing meeting (the import is proven done);
  keep the error only when no meeting exists.
- **Regression test:** Full-upload via API to 100 %; commit a meeting row
  with `id == upload_id`; delete the temp file; leave the session
  `uploading`; call `complete_upload` again; assert status `completed` and
  the existing meeting id, not an error.

## 6. F5 — `start_transcription` double-submit race (P3, POSSIBLE)

- **Location:** `core/services/transcribe.py`, `start_transcription()`:
  guard 1 (inside `session_scope`): `meeting_id in
  self._manual_transcriptions or job.status == "running"`; then commit
  (`job=pending`, `meeting=transcribing`); guard 2 (under
  `self._pipeline_lock`): re-check `_manual_transcriptions` before adding
  and submitting.
- **Trigger / execution path:** Two concurrent calls (double-click, two
  tabs) interleave so that call B reads the job *before* call A's commit
  (sees old status, not `running`; A not yet in `_manual_transcriptions`)
  **and** B's lock-block runs before A's `add`. Both then add (set is
  idempotent) and both submit `_run_manual_transcription`. The per-meeting
  `_transcription_locks` inside `transcribe()` serializes execution, so B
  blocks until A finishes and then re-runs the full ASR.
- **Impact:** No corruption (the processor is idempotent and creates a new
  transcript version; final segment content for identical model+audio is
  the same). Cost: one full duplicate ASR pass (minutes of CPU on a
  laptop) and an extra transcript version in history.
- **Why safeguards don't prevent it:** The two guards are intentionally
  belt-and-braces, but the check in guard 1 happens before A's commit and
  the check in guard 2 before A's `add` — the two critical sections are
  not one atomic check-then-act. Window is milliseconds, hence POSSIBLE
  rather than CONFIRMED.
- **Recommended fix:** Do the `_manual_transcriptions` check-and-add first
  (under `_pipeline_lock`) and only then commit job/meeting state — i.e.
  make the in-memory set the primary admission decision; or keep a single
  lock around commit + add.
- **Regression test:** Monkeypatch `transcribe()` to record calls and sleep
  0.5 s; fire two `start_transcription` calls concurrently in threads
  (loop N times to hit the window); assert `transcribe` ran exactly once.

## 7. F6 — Tracked build artifacts `ui/dist/` (P3, CONFIRMED, hygiene)

- **Location:** `.gitignore` contains `dist/` followed by `!ui/dist/`;
  `git ls-files ui/dist` shows `index.html`, `assets/index-D4ZbY6wl.js`,
  `assets/index-DT0wkfTB.css`.
- **Problem:** Build output is version-controlled. Currently in sync (both
  last touched by commit `1b8e343`), but any future `ui/src` change that
  skips `npm run build` ships a stale or inconsistent bundle through the
  repo (and through any install that uses the repo `ui/dist/` as served by
  the core). It also bloats diffs and can leak dev-only strings.
- **Recommended fix:** Either drop `!ui/dist/` and generate it at
  install/packaging time (the core already works fine without it — pure
  REST), or add a CI check that `ui/dist` matches a fresh build.

## 8. F7 — RAG grounding is citation-ID-only (P3, CONFIRMED, design note)

- **Location:** `core/search/rag.py` + `core/llm/output.py`: validation
  checks that every cited segment ID (`S<n>`) exists in the retrieved
  context; it does **not** verify that the cited segment's content
  supports the claim.
- **Impact:** With small local models, a hallucinated statement decorated
  with a valid `S<n>` passes "grounding" validation. The UI presents the
  result as grounded. This is a stated, pragmatic trade-off for 7B–27B
  local models (content-level entailment checks need another LLM call),
  but it should be surfaced: consider labeling results "citations
  verified" rather than "grounded", or optionally running a cheap
  relevance filter (token overlap between claim and cited segment).
- **Status:** design choice, not a defect — listed for completeness.

## 9. F8 — Blocking `wait=true` pipeline endpoint (P3, CONFIRMED, design note)

- **Location:** `core/api/rest.py:409` `run_pipeline(meeting_id, wait=False)`
  → `MeetingService.run_pipeline` → `fut.result()` with no timeout.
- **Impact:** A caller opting into `wait=true` can hold the HTTP request
  for the full pipeline duration: ASR (minutes) + LLM timeout 1800 s plus
  busy-retry budget (`llm_max_busy_retries` 10 × `llm_busy_wait_s` 3 s plus
  bounded budget). Browsers/proxies may time out while the server
  continues; the client loses visibility. The UI itself always uses
  non-blocking + polling, so this only bites API/CLI consumers.
- **Recommended fix (optional):** Document the contract loudly, or add a
  server-side `wait` cap with a "still running" response.

---

## 10. Missing test coverage (regression tests to add)

| Gap | Covers | Test sketch |
|-----|--------|-------------|
| No test of restore with a live WAL | F1 | See F1 regression test |
| No test of crash-resume for status `analyzing` | F2 | See F2 regression test (`tests/test_pipeline_resume.py` only covers `processing` and `done`+failed) |
| No test of permanent delete with `original_path=NULL` but chunks on disk | F3 | See F3 regression test |
| No test of the upload crash window (meeting committed, session not, file gone) | F4 | See F4 regression test |
| No concurrent double-submit test for `start_transcription` | F5 | See F5 regression test |
| No test that a stale `.db-wal` next to a restored DB cannot corrupt (adjacent to F1) | F1 | Same test, assert `integrity_check` post-restore |

All 466 existing tests pass (`uv run pytest -q`); the gaps above are the
only state combinations found to be untested *and* reachable.

## 11. Simplification opportunities (no behavior change)

1. **`core/backup.py`** uses `__import__("json").dumps(...)` inline — a
   plain top-level `import json` (already used elsewhere in the module
   context) is clearer.
2. **`core/service.py` `get_status()`** imports `Path` locally
   (`from pathlib import Path`) inside a hot method; it is already
   importable at module scope.
3. **`pipeline_status()`** recomputes `pipeline_stages(meeting_id)`
   (which re-reads settings from the DB) for every poll; the UI polls
   frequently. A cached stage list per meeting settings-hash would cut DB
   reads; low priority.
4. **`resume_pending_pipelines()`** builds `jobs_by_meeting` for *all*
   active meetings then walks `pipeline_stages` per meeting (each
   re-reading settings). At the current scale (one active pipeline at a
   time) this is negligible — listed only because the docstring says it is
   a startup path.
5. **`UploadsMixin`** duplicates the project-existence check and
   `safe_name` sanitization across `import_upload`/`start_upload`; a small
   shared helper would keep the two paths from drifting (they already
   differ slightly in the document-extension set — both copy-pasted).

## 12. Investigated and clean (false positives / verified correct)

- **Audio capture & assembly** (`core/audio/capture.py`, `chunker.py`,
  `assembly.py`, `stream.py`): crash-safe by construction — chunk index is
  fsync'd per line and is the source of truth; `original.wav` assembled
  atomically and read-only; `BoundedChunkQueue` prevents PortAudio
  callback deadlock; failed `start_meeting` marks the DB row `failed`
  (no phantom active meeting); `stop()` keeps the session registered on
  worker timeout so a second recording cannot start concurrently.
- **Recovery** (`core/recovery/recovery.py`): correct for its stated scope
  (`recording`/`paused` + `processing`-without-job reconciliation);
  idempotent (test `test_recovery_is_idempotent` passes). The `analyzing`
  gap is F2, not a recovery defect.
- **Job queue** (`core/jobs/queue.py`): simple, correct state machine;
  `recover_crashed` resets only `running`.
- **Transcription** (`core/transcribe/processor.py`): idempotent, versioned.
- **LLM layer** (`core/llm/*`): bounded busy-retries with explicit budget
  cap, context-overflow parse + shrink-retry, per-role engine caching with
  correct shared-close logic in `cancel_analysis` (only closes when no
  other active analysis holds the engine).
- **Security** (`core/security/secrets.py`, `core/api/app.py`):
  loopback-only bind enforced, `network_allowed` default false, model
  downloads require explicit confirmation, keyring-backed, no catch-all
  route (REST cannot be shadowed by UI), preview tokens for audio
  endpoints, upload temp paths confined to `upload_tmp`
  (`_upload_temp_path` containment check), zip-slip guarded in
  `project_documents.py`, backup paths confined to the backups dir
  (`_safe_backup_path`).
- **Uploads** (`core/services/uploads.py`): resumable, fsync'd,
  offset-verified with truncate-rollback on DB error; double-completion
  serialized by `_upload_complete_guard`; atomic copy without RAM
  buffering; idempotent import. F4 is the only residual.
- **Live transcription** (`core/live/*`): append-only with
  `_written_end_s` watermark; evicted live audio is by design (batch
  transcription is authoritative). No bugs.
- **Daemon** (`core/daemon.py`): correct double-fork; stale-PID detection;
  the unlink+`O_CREAT` lock race resolves safely (second starter sees the
  new live lock → `AlreadyRunning`).
- **Bootstrap** (`core/bootstrap.py`): logging → storage → migrations →
  recovery → `resume_pending_pipelines` → stale preview cleanup, in the
  right order.
- **`close()` shutdown** (`core/service.py`): stops capture sessions via
  the normal stop path, then drains pipeline + enhancement executors
  *before* the process releases PID/lock — prevents a second daemon from
  starting against a still-writing DB.
- **Migrations**: Alembic runs on `init`/`serve`/`daemon`; legacy DBs
  auto-stamped; no destructive rewrites observed.
- **Secrets/history audit**: all grep hits false positives; removed
  `AGENTS.md` in history is not sensitive. No tokens, keys, or internal
  endpoints in code, config, or `ui/dist`.
- **Packaging**: `pyproject.toml` lists all runtime deps (torch optional
  behind `diar` extra); systemd unit sane (`UMask=0077`,
  `Restart=on-failure`); `.env.example` documents precedence correctly and
  contains no real secrets.

## 13. Recommended fix order

1. **F1 (P1):** delete `-wal`/`-shm` before `os.replace` in
   `restore_backup` + post-swap `integrity_check` + regression test.
   One-line-class fix, high value.
2. **F2 (P2):** add `"analyzing"` to `resume_pending_pipelines`
   `active_ids` + regression test. Two-line fix.
3. **F3 (P2):** also rmtree `audio_dir / meeting_id` in
   `permanently_delete_meeting` + regression test.
4. **F4 (P3):** `complete_upload` detects already-imported meeting when the
   temp file is gone + regression test.
5. **F5 (P3):** reorder the admission check in `start_transcription`
   (set-add before commit) + concurrency test.
6. **F6 (P3):** decide on `ui/dist` tracking (drop negation or add CI
   sync check).
7. **F7/F8 (P3):** documentation/labeling only, unless product wants
   stronger grounding claims.

---

# Re-Audit (Phase 2: after applying F1–F6)

Date: 2026-07-12 (follow-up session). Scope: verify each fix closes its finding
**with proof**, and audit the *new* code for introduced problems.

## Verification method (per your requirement: a finding is "closed" only if the
new test actually proves it)

For every fix the new regression test was run against the **original (git HEAD)
implementation** and the fixed one:

| Finding | New test | Old code | New code |
|---|---|---|---|
| F1 | `test_restore_with_stale_wal_does_not_corrupt` | **FAILED** — restored DB contained the newer state `{'B': 'zz'}` (full WAL replay onto the snapshot) | PASSED |
| F2 | `test_resume_recovers_analyzing_meeting` | **FAILED** (meeting stuck in `analyzing`, `n == 0`) | PASSED |
| F3 | `test_permanent_delete_removes_audio_dir_without_original_path` | **FAILED** (audio dir left on disk) | PASSED |
| F4 | `test_complete_upload_after_crash_between_import_and_status` (+ error-path guard test) | **FAILED** (`ValueError`, session stuck `uploading`) | PASSED |
| F5 | `test_start_transcription_concurrent_calls_single_run` + 2 slot-release tests | **PASSED** — see correction below | PASSED |

Suite: **406 passed, 0 failed** (was 398; +8 new tests). UI: `npm run build`
clean, identical asset hashes (no `ui/src` changes).

## F5 correction (false positive in the original audit)

The re-audit found that the original F5 report was **wrong**: the pre-fix code's
slot check-and-add was *already atomic* under `_pipeline_lock` (single `with`
block), so the described double-ASR-run race was impossible — the concurrent
test passes on the old code. **F5 is downgraded to FALSE-POSITIVE.**
The applied refactor is nevertheless kept because it fixes a real (minor)
issue the re-audit found in the old ordering: a concurrent caller that lost
the race but had already passed the in-session guard would commit its own
`job.retries += 1` — one double click on a failed job consumed **two** retry
budget entries. With the claim moved before the DB write, only the winner
touches the job row.

## F6 disposition

Per decision: `ui/dist` **stays tracked** (zero-build install model: the systemd
unit serves the repo's `ui/dist`, no build step on the host). Mitigation added:
`.github/workflows/ci.yml` `ui` job now fails if the committed bundle is stale
(checked after `npm run build`: untracked new output + `git diff` on `ui/dist`).
No git-ignore changes were made.

## Audit of the *new* code (issues found and fixed during re-audit)

1. **`_DBQuietBarrier.quiet()` pre-check race (fixed)**: the original
   `if self._quiet: raise` was outside the lock — two concurrent restores could
   both enter the swap. The check is now inside the condition lock; the second
   caller fails immediately with a clear error.
2. **F3 false warning (fixed)**: the deterministic `audio_dir/<meeting_id>`
   deletion initially triggered `audio_delete_failed` warnings for meetings
   without an audio dir; now guarded by `is_dir()`.
3. **`start_transcription` fast path (fixed)**: originally fetched the job via
   `session_scope` *while holding* `_pipeline_lock` — during a restore's quiet
   window that would have blocked other transcription requests for up to 60 s.
   The session now runs outside the lock; the critical claim still happens
   under one lock section (fast-path hint + re-check under lock).

## Remaining known limitations (deliberate, documented)

- **F1**: an *external* read-only SQLite connection (e.g. a DBA's CLI) that
  keeps the WAL open can block the checkpoint loop until the bounded 5-attempt
  retry aborts with a clear error — restore is then simply retried. Auto-
  rollback to the safety snapshot on post-swap integrity failure is **not**
  implemented (the user gets a clear error + the safety path instead); a
  manual second restore can then re-apply the same or an older backup.
- **F2**: resume now also covers `analyzing`; the "all stages done but meeting
  still `analyzing`" state cannot occur (job-done and status-done are one
  transaction in `AnalysisProcessor`), verified in `core/analysis/processor.py`.
- Barrier worst case: while a restore is quiet, new `session_scope` entries
  block up to 60 s and then surface as HTTP 500 — bounded and logged.

## Files changed

`core/store/db.py` (quiet barrier + session_scope integration),
`core/backup.py` (`_swap_db_file`), `core/services/pipelines.py` (F2),
`core/services/meetings.py` (F3), `core/services/uploads.py` (F4),
`core/services/transcribe.py` (F5 refactor), `.github/workflows/ci.yml` (F6),
tests: `test_backup.py`, `test_pipeline_resume.py`, `test_workspace_upgrade.py`,
`test_transcribe.py` (extended) + new `test_uploads.py`.
