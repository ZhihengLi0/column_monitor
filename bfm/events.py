"""Cursor-based event watchers: things that happened since the last cycle
(CS2 system alerts, pump on/off, heater/heat-switch changes, valve changes).

Shared rules the legacy code lacked:
  * every query has a LIMIT (a cycle processes at most `LIMIT` rows and
    continues next cycle);
  * a cursor of 0 (never initialised, or the state file was lost) is set to
    the current MAX(id) instead of replaying the whole 11 GB history into
    Slack;
  * a cursor beyond MAX(id) (database restored from backup) is reset too.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from . import config

log = logging.getLogger("bfm.events")

LIMIT = 200

HEATER_MAPPINGS = [
    ("HEATSWITCH_STILL_ENABLED", "Still Heat Switch"),
    ("HEATSWITCH_MXC_ENABLED",   "MXC Heat Switch"),
    ("STILL_HEATER_ENABLED",     "Still Heater"),
    ("MXC_HEATER_ENABLED",       "MXC Heater"),
]
DEVICE_ALERT_MAPPINGS = HEATER_MAPPINGS + [
    ("PULSE_TUBE_ENABLED", "Pulse Tube"),
    ("B1A_ENABLED",        "B1A Turbo Pump"),
    ("B2_ENABLED",         "B2 Turbo Pump"),
]
MONITORED_VALVES = [("V112_ENABLED", "V112"), ("V113_ENABLED", "V113"), ("V114_ENABLED", "V114")]


def _cursor(st: dict, key: str, db, table: str, mappings) -> int | None:
    """Return a safe cursor; None means 'just initialised, nothing to report'."""
    cur = int(st.get(key) or 0)
    if cur <= 0:
        st[key] = db.max_id(table, mappings)
        log.warning("%s cursor was 0 — initialised to %s (no history replay)", key, st[key])
        return None
    return cur


class EventWatcher:
    name = "base"

    def evaluate(self, ctx, st: dict) -> list[str]:   # pragma: no cover
        raise NotImplementedError


class CS2Alerts(EventWatcher):
    """Rows of public.alerts with severity ≥ CS2_ALERT_MIN_SEVERITY, grouped by code."""
    name = "cs2_alerts"

    def evaluate(self, ctx, st):
        db = ctx.cs2
        last_id = int(st.get("last_cs2_alert_id") or 0)
        if last_id <= 0:
            r = db.one("SELECT COALESCE(MAX(id), 0) FROM public.alerts WHERE severity >= %s",
                       (config.CS2_ALERT_MIN_SEVERITY,))
            st["last_cs2_alert_id"] = int(r[0]) if r else 0
            log.warning("last_cs2_alert_id was 0 — initialised to %s", st["last_cs2_alert_id"])
            return []
        rows = db.query_dicts(
            "SELECT id, code, datetime, title, description, severity FROM public.alerts "
            "WHERE id > %s AND severity >= %s ORDER BY id LIMIT 50",
            (last_id, config.CS2_ALERT_MIN_SEVERITY))
        if not rows:
            return []
        st["last_cs2_alert_id"] = max(r["id"] for r in rows)   # advance even when disabled
        if not st.get("cs2_alerts_enabled", True):
            return []
        by_code = defaultdict(list)
        for r in rows:
            by_code[r["code"]].append(r)
        msgs = []
        for code, group in by_code.items():
            row = group[0]
            emoji = ":red_circle:" if row["severity"] >= 2 else ":large_yellow_circle:"
            kind = "Error" if row["severity"] >= 2 else "Warning"
            cnt = f" (×{len(group)})" if len(group) > 1 else ""
            msgs.append(f"{emoji} *CS2 {kind}* [code {code}]{cnt}\n"
                        f"*{row['title']}*\n{row['description'] or ''}\n"
                        f"First: {group[0]['datetime']}  Last: {group[-1]['datetime']}")
            log.warning("CS2 alert ×%d: [%s] %s", len(group), code, row["title"])
        return msgs


class R1APump(EventWatcher):
    """R1A enabled/error boolean changes + pump power crossing 0.1 W."""
    name = "r1a"
    BOOLS = ["R1A_ENABLED", "R1A_ERROR_VALUE"]

    def evaluate(self, ctx, st):
        db = ctx.cs2
        msgs = []
        cur = _cursor(st, "last_r1a_event_id", db, "boolean", self.BOOLS)
        if cur is not None:
            rows = db.events_since(cur, self.BOOLS, "boolean", LIMIT)
            if rows:
                st["last_r1a_event_id"] = rows[-1][0]
                for _id, mapping, value, ts in rows:
                    if mapping == "R1A_ENABLED":
                        status = ":large_green_circle: *ON*" if value else ":red_circle: *OFF*"
                        msgs.append(f":gear: *R1A Pump enabled changed* → {status}\nTime: {ts}")
                    else:
                        status = ":red_circle: *ERROR*" if value else ":white_check_mark: *Cleared*"
                        msgs.append(f":warning: *R1A Pump error changed* → {status}\nTime: {ts}")
                    log.info("R1A status change: %s = %s", mapping, value)

        cur = _cursor(st, "last_r1a_power_id", db, "double", ["R1A_PUMP_POWER"])
        if cur is not None:
            rows = db.events_since(cur, ["R1A_PUMP_POWER"], "double", LIMIT)
            if rows:
                st["last_r1a_power_id"] = rows[-1][0]
                latest_power, latest_time = float(rows[-1][2]), rows[-1][3]
                prev = st.get("last_r1a_power_value")
                was_running = prev is not None and prev > 0.1
                now_running = latest_power > 0.1
                if prev is not None and was_running != now_running:
                    if now_running:
                        msgs.append(f":large_green_circle: *R1A Pump power ON* → {latest_power:.1f} W\nTime: {latest_time}")
                    else:
                        msgs.append(f":red_circle: *R1A Pump power OFF* → {latest_power:.1f} W\nTime: {latest_time}")
                    log.info("R1A pump power transition: %.1fW → %.1fW", prev, latest_power)
                st["last_r1a_power_value"] = latest_power
        return msgs


class HeaterChanges(EventWatcher):
    """Heat switches, heaters, pulse tube, turbo pumps: ON/OFF change events."""
    name = "heater"
    MAPPINGS = [m for m, _ in DEVICE_ALERT_MAPPINGS]
    LABELS = dict(DEVICE_ALERT_MAPPINGS)

    def evaluate(self, ctx, st):
        db = ctx.cs2
        cur = _cursor(st, "last_heater_event_id", db, "boolean", self.MAPPINGS)
        if cur is None:
            return []
        rows = db.events_since(cur, self.MAPPINGS, "boolean", LIMIT)
        if not rows:
            return []
        st["last_heater_event_id"] = rows[-1][0]
        msgs = []
        for _id, mapping, value, ts in rows:
            label = self.LABELS.get(mapping, mapping)
            status = ":large_green_circle: *ON*" if value else ":red_circle: *OFF*"
            msgs.append(f":fire: *{label}* changed → {status}\nTime: {ts}")
            log.info("Heater change: %s = %s", mapping, value)
        return msgs


class ValveChanges(EventWatcher):
    name = "valves"
    MAPPINGS = [m for m, _ in MONITORED_VALVES]
    LABELS = dict(MONITORED_VALVES)

    def evaluate(self, ctx, st):
        db = ctx.cs2
        cur = _cursor(st, "last_valve_event_id", db, "boolean", self.MAPPINGS)
        if cur is None:
            return []
        rows = db.events_since(cur, self.MAPPINGS, "boolean", LIMIT)
        if not rows:
            return []
        st["last_valve_event_id"] = rows[-1][0]
        msgs = []
        for _id, mapping, value, ts in rows:
            label = self.LABELS.get(mapping, mapping)
            status = ":large_green_circle: *OPEN*" if value else ":red_circle: *CLOSED*"
            msgs.append(f":warning: *Valve {label}* changed → {status}\nTime: {ts}")
            log.info("Valve change: %s = %s", mapping, value)
        return msgs


ALL_EVENTS: list[EventWatcher] = [CS2Alerts(), R1APump(), HeaterChanges(), ValveChanges()]


def init_cursors(st: dict, db) -> None:
    """`--init`: set every cursor to the current MAX(id) so no history is replayed."""
    r = db.one("SELECT COALESCE(MAX(id), 0) FROM public.alerts WHERE severity >= %s",
               (config.CS2_ALERT_MIN_SEVERITY,))
    st["last_cs2_alert_id"] = int(r[0]) if r else 0
    st["last_r1a_event_id"] = db.max_id("boolean", R1APump.BOOLS)
    st["last_heater_event_id"] = db.max_id("boolean", HeaterChanges.MAPPINGS)
    st["last_valve_event_id"] = db.max_id("boolean", ValveChanges.MAPPINGS)
    row = db.one("SELECT id, value FROM public.double_value_change_events "
                 "WHERE mapping = 'R1A_PUMP_POWER' ORDER BY time DESC LIMIT 1")
    if row:
        st["last_r1a_power_id"] = int(row[0])
        st["last_r1a_power_value"] = float(row[1])
