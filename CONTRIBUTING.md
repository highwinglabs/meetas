# Contributing

Thanks for helping. This repository follows a simple, local-first design; please read
[README.md](./README.md) for the project layout, commands, and architecture notes.

## Development setup

```bash
uv sync --extra dev                  # base deps + pytest/httpx
uv run pytest -q                     # run the backend test suite
cd ui && npm install && npm run build  # build the UI (tsc --noEmit && vite build)
```

The test suite is **headless and offline**: capture uses synthetic sources (never a real
microphone), and ASR/LLM run on mocks. Please keep it that way.

## Guidelines

- Match the existing style: Python uses type hints and `snake_case`; TypeScript is strict
  with `camelCase` and two-space indent. No formatter is configured — follow the
  surrounding code.
- Keep REST schemas (`core/api/schemas.py`), the API handlers, and the UI API types in
  sync.
- Any schema change needs an **Alembic migration** plus a regression test. Never
  destructively rewrite existing user data.
- Preserve the privacy guarantees: loopback-only binding, `network_allowed=false` by
  default, confirmation-gated downloads, and `0600`/`0700` permissions. Do not log
  secrets or bypass the download gate.
- Run the full suite for service/DB/migration/API changes and `npm run build` for UI
  changes.

## Commits & pull requests

- Use concise conventional prefixes (`feat:`, `fix:`, `chore:`, …) with an imperative
  summary.
- Explain user impact and implementation, list the commands you ran and their results,
  and call out any migrations or security implications.
