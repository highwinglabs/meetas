"""Structured logging that never records confidential content.

Privacy rule: logs contain metadata / error codes only. Audio and transcript
content, prompts, and credentials are redacted.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

# Keys whose values must never appear in logs.
_SENSITIVE_KEYS = {
    "text", "raw_text", "prompt", "completion", "quote", "content",
    "api_key", "apikey", "token", "authorization", "password", "secret",
    "body", "samples", "audio",
}
_SECRET_VALUE = re.compile(
    r"(?i)(api[_-]?key|token|authorization|bearer)\s*[:=]\s*\S+"
)


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***REDACTED***" if str(k).lower() in _SENSITIVE_KEYS else _scrub(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(lambda m: m.group(1) + "=***", value)
    return value


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            if len(record.args) == 1 and isinstance(record.args[0], dict):
                record.args = (_scrub(record.args[0]),)
        except Exception:  # never let logging break the app
            pass
        msg = super().format(record)
        return _SECRET_VALUE.sub(lambda m: m.group(1) + "=***", msg)


def setup_logging(level: str = "INFO", log_path: Path | None = None,
                  to_stderr: bool = True) -> None:
    fmt = RedactingFormatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    if log_path is not None:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path)
        fh.setFormatter(fmt)
        root.addHandler(fh)

    if to_stderr:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # quiet noisy third-party loggers
    for noisy in ("urllib3", "httpx", "watchfiles"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
