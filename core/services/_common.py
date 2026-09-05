"""Shared helpers for the MeetingService and its domain mixins.

Kept import-free of core.service so the mixin modules can depend on them
without circular imports.
"""
from __future__ import annotations

import urllib.request
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from core.analysis import schema
from core.store.models import Task

class ConsentRequiredError(RuntimeError):
    pass


class ActiveMeetingError(RuntimeError):
    pass


class UnknownMeetingError(KeyError):
    pass


class UnknownTaskError(KeyError):
    pass

class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the local maintenance endpoint confined to the checked URL.

    ``urllib.request.urlopen`` follows redirects by default.  A malicious or
    compromised local endpoint could otherwise redirect the cache-release
    request to an arbitrary host after the loopback check had passed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # pragma: no cover - stdlib path
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)

def utc_iso(value: datetime | None) -> str | None:
    """Serialize stored naive-UTC datetimes with an explicit UTC offset."""
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat()


def berlin_date(value: datetime | None) -> str | None:
    """Return the calendar date in the UI's local Berlin timezone."""
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()


def parse_utc_datetime(value: str | None) -> datetime | None:
    """Parse an API timestamp and store it as naive UTC for SQLite.

    ``replace(tzinfo=None)`` would silently reinterpret an offset timestamp as
    local time.  Normalize aware values first so uploads from Berlin, UTC and
    other clients all refer to the same instant.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Ungültiges Datum für den Upload.") from exc
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def redact_endpoint(url: str) -> str:
    """Return an endpoint suitable for API/UI output without credentials."""
    try:
        parsed = urlsplit(url or "")
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        # Query/fragment values can also carry credentials or tokens.
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return "<ungültiger Endpunkt>"


def _task_display_status(task: Task) -> str:
    """Return a non-destructive UI status derived from the stored task."""
    if task.status == "erledigt":
        return "erledigt"
    due = str(task.due_date or "").strip()
    if schema.is_missing(due):
        return "ohne_deadline"
    try:
        due_day = datetime.fromisoformat(due.replace("Z", "+00:00")).date()
    except ValueError:
        return task.status
    berlin_today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    return "ueberfaellig" if due_day < berlin_today else (task.status or "offen")


def _analysis_markdown(content: Optional[str], lang: Optional[str] = None) -> str:
    """Render a stored analysis (JSON) as Markdown; fall back to the raw text if
    it is not valid structured JSON (e.g. a very old record).

    ``lang`` localises the section headings / "not specified" placeholder to the
    analysis output language (defaults to German, the historical default)."""
    if not content:
        return ""
    data = schema.extract_json(content)
    if isinstance(data, dict):
        return schema.render_markdown(data, lang=lang)
    return content
