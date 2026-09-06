"""L12: user-facing date rendering uses the configurable display timezone."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from core.config import Config
from core.services import _common as C

# 23:00 UTC on 2024-01-15: Berlin (UTC+1 in winter) -> 2024-01-16 00:00,
# New York (UTC-5 in winter) -> 2024-01-15 18:00. The dates differ, so the
# test only passes when the display date actually follows the configured zone.
_TS = datetime(2024, 1, 15, 23, 0, tzinfo=timezone.utc)


def test_berlin_date_uses_config_timezone(config):
    config.timezone = "Europe/Berlin"
    assert C.berlin_date(_TS) == "2024-01-16"
    config.timezone = "America/New_York"
    assert C.berlin_date(_TS) == "2024-01-15"


def test_berlin_date_invalid_timezone_falls_back_to_berlin(config):
    config.timezone = "Not/AZone"
    assert C.berlin_date(_TS) == "2024-01-16"
    assert C.berlin_date(None) is None


def test_timezone_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MA_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("MA_TIMEZONE", "America/New_York")
    assert Config.load().timezone == "America/New_York"


def test_task_overdue_uses_config_timezone(config):
    # L12: the overdue flag is judged against "today" in the configured zone.
    config.timezone = "Europe/Berlin"
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    overdue = SimpleNamespace(status="offen", due_date=str(today - timedelta(days=1)))
    assert C._task_display_status(overdue) == "ueberfaellig"
    upcoming = SimpleNamespace(status="offen", due_date=str(today + timedelta(days=1)))
    assert C._task_display_status(upcoming) == "offen"
    # A done task is never flagged overdue, even with a past deadline.
    done = SimpleNamespace(status="erledigt", due_date=str(today - timedelta(days=1)))
    assert C._task_display_status(done) == "erledigt"
