"""Time helpers.

Rules used throughout bfm:
  * State timestamps (cooldowns, silences, reminders) are NAIVE local
    wall-clock ISO strings, exactly like the legacy monitor_state.json, so the
    live state file migrates without rewriting. `now()` returns naive local.
  * Anything that must survive DST or be shown on a plot axis goes through
    LOCAL_TZ (America/Chicago). The legacy code used a fixed UTC-5 offset,
    which was one hour off every winter.
  * Database rows are timezone-aware (TIMESTAMPTZ). `to_local()` converts them
    for display.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Chicago")

# Stored for "silence forever": every `parse_iso(x) > now()` check keeps working.
FOREVER_TS = datetime.max.isoformat()


def now() -> datetime:
    """Naive local wall-clock time (state-file compatible)."""
    return datetime.now()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO string from the state file. Returns naive local time; aware
    strings are converted. None / garbage → None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return dt


def is_future(s: str | None) -> bool:
    """True if the state timestamp `s` is still ahead of now (unexpired)."""
    dt = parse_iso(s)
    return dt is not None and dt > now()


def age_minutes(ts: datetime | None) -> float | None:
    """Minutes between an aware (DB) or naive (local) timestamp and now."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return (now() - ts).total_seconds() / 60.0
    return (utcnow() - ts).total_seconds() / 60.0


def to_local(ts: datetime) -> datetime:
    """Aware or naive → aware local time."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=LOCAL_TZ)
    return ts.astimezone(LOCAL_TZ)


def fmt_local(ts: datetime | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    if ts is None:
        return "n/a"
    return to_local(ts).strftime(fmt)


def tz_abbrev(when: datetime | None = None) -> str:
    """'CST' or 'CDT' for the given (default: current) moment."""
    dt = to_local(when) if when else datetime.now(LOCAL_TZ)
    return dt.strftime("%Z")


def parse_compact_local(s: str) -> datetime | None:
    """'YYMMDD_HHMM' typed by a user in local time → aware UTC datetime."""
    import re
    m = re.fullmatch(r"(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})", s)
    if not m:
        return None
    try:
        dt = datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)),
                      int(m.group(4)), int(m.group(5)), tzinfo=LOCAL_TZ)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc)


def fmt_compact_local(ts: datetime) -> str:
    return to_local(ts).strftime("%y%m%d_%H%M")


def minutes_from_now(minutes: float) -> str:
    """ISO string for `minutes` from now (naive local) — for silences/overrides."""
    return (now() + timedelta(minutes=minutes)).isoformat()
