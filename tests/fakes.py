"""In-memory stand-ins for Database / DataContext and the outbox."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc(minutes_ago: float = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


class FakeDB:
    """Minimal Database replacement.

    latest:   {(table, mapping): (value, time)}
    events:   {table: [(id, mapping, value, time), ...]}
    alerts:   [dict(id, code, datetime, title, description, severity)]
    devices:  {device_id: {"datetime": ts, "values": {...}}}
    ln2_rows: [(time, weight, temp, humidity)] newest last
    """
    def __init__(self):
        self.latest_rows: dict = {}
        self.events: dict = {"double": [], "boolean": [], "int": []}
        self.alerts: list[dict] = []
        self.devices_: dict = {}
        self.ln2_rows: list = []
        self.max_time_: dict = {}
        self.global_max_time = None
        self.queries: list = []

    # Database API --------------------------------------------------------
    def latest(self, mapping, table="double"):
        return self.latest_rows.get((table, mapping))

    def latest_before(self, mapping, before, table="double"):
        r = self.latest_rows.get((table, f"{mapping}@before"))
        return r

    def max_time(self, mapping=None, table="double"):
        if mapping is None:
            return self.global_max_time
        if mapping in self.max_time_:
            return self.max_time_[mapping]
        r = self.latest_rows.get((table, mapping))
        return r[1] if r else None

    def max_id(self, table="double", mappings=None):
        rows = self.events.get(table, [])
        if mappings:
            rows = [r for r in rows if r[1] in set(mappings)]
        return max((r[0] for r in rows), default=0)

    def events_since(self, cursor_id, mappings, table="double", limit=200):
        ms = set(mappings)
        rows = [r for r in self.events.get(table, []) if r[1] in ms and r[0] > cursor_id]
        return sorted(rows)[:limit]

    def device_states(self):
        return dict(self.devices_)

    def one(self, sql, params=None):
        self.queries.append((sql, params))
        if "FROM public.alerts" in sql and "MAX(id)" in sql:
            sev = params[0]
            return (max((a["id"] for a in self.alerts if a["severity"] >= sev), default=0),)
        if "R1A_PUMP_POWER" in sql:
            rows = [r for r in self.events["double"] if r[1] == "R1A_PUMP_POWER"]
            if not rows:
                return None
            r = max(rows, key=lambda r: r[3])
            return (r[0], r[2])
        if "FROM scale_readings" in sql and "weight >= 0" in sql:
            ws = [r for r in self.ln2_rows if r[1] is not None and r[1] >= 0]
            return (ws[-1][1],) if ws else None
        if "FROM scale_readings" in sql:
            if not self.ln2_rows:
                return None
            t, w, temp, hum = self.ln2_rows[-1]
            return (t, temp, hum)
        return None

    def query_dicts(self, sql, params=None):
        self.queries.append((sql, params))
        if "FROM public.alerts" in sql:
            last_id, sev = params
            rows = [a for a in self.alerts if a["id"] > last_id and a["severity"] >= sev]
            return sorted(rows, key=lambda a: a["id"])[:50]
        return []


class FakeCtx:
    """DataContext replacement built on FakeDB (same lazy API, no caching needed)."""
    def __init__(self, cs2: FakeDB | None = None, ln2: FakeDB | None = None):
        self.cs2 = cs2 or FakeDB()
        self.ln2 = ln2

    def latest(self, mapping):
        return self.cs2.latest(mapping, "double")

    def latest_value(self, mapping):
        r = self.latest(mapping)
        return None if r is None else float(r[0])

    def latest_bool(self, mapping):
        return self.cs2.latest(mapping, "boolean")

    def devices(self):
        return self.cs2.device_states()

    def device(self, device_id):
        d = self.devices().get(device_id)
        return d["values"] if d else None

    def ln2_latest(self):
        if self.ln2 is None or not self.ln2.ln2_rows:
            return None
        t, w, temp, hum = self.ln2.ln2_rows[-1]
        ws = [r for r in self.ln2.ln2_rows if r[1] is not None and r[1] >= 0]
        return (t, ws[-1][1] if ws else None, temp, hum)


class Outbox:
    """Records enqueue() calls the way StateStore.enqueue would."""
    def __init__(self):
        self.msgs: list[dict] = []

    def __call__(self, text, *, color="danger", thread_ts=None, key=None, kind="event"):
        m = {"text": text, "color": color, "thread_ts": thread_ts, "key": key, "kind": kind}
        self.msgs.append(m)
        return m

    def texts(self):
        return [m["text"] for m in self.msgs]

    def keys(self, kind=None):
        return [m["key"] for m in self.msgs if kind is None or m["kind"] == kind]

    def clear(self):
        self.msgs.clear()


def base_state(mode="COLD") -> dict:
    from bfm.core.state import empty_state
    s = empty_state()
    s["current_mode"] = mode
    return s
