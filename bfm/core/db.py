"""Database access: one persistent connection per database with automatic
reconnect, plus a per-cycle DataContext cache.

Every query against the 23-million-row change-event tables goes through
`latest()` / `max_time()` / `events_since()`, which are shaped to use the
(mapping, time) index with a LIMIT. The legacy code's `DISTINCT ON (mapping)
... WHERE mapping IN (...)` and `GROUP BY mapping` forms scanned the whole
table once the page cache was cold (5–7 minutes per query after a reboot).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

import psycopg2
import psycopg2.extras

log = logging.getLogger("bfm.db")

TABLES = {
    "double":  "public.double_value_change_events",
    "boolean": "public.boolean_value_change_events",
    "int":     "public.int_value_change_events",
}
STATEMENT_TIMEOUT_MS = 45_000
RECONNECT_BACKOFF = (2, 5, 10, 30, 60)


class DatabaseUnavailable(RuntimeError):
    pass


class Database:
    """A lazily-connected, self-healing psycopg2 connection (autocommit)."""

    def __init__(self, *, host: str, port: int, user: str, password: str | None,
                 dbname: str, name: str | None = None):
        self.kw = dict(host=host, port=port, user=user, dbname=dbname,
                       connect_timeout=5,
                       options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}")
        if password:
            self.kw["password"] = password
        self.name = name or dbname
        self._conn = None
        self._fail_count = 0
        self._next_try = 0.0
        self.last_error: str | None = None

    # ── connection lifecycle ─────────────────────────────────────────────────
    def connect(self):
        if self._conn is not None and not self._conn.closed:
            return self._conn
        if time.monotonic() < self._next_try:
            raise DatabaseUnavailable(f"{self.name}: {self.last_error or 'reconnect backoff'}")
        try:
            conn = psycopg2.connect(**self.kw)
            conn.autocommit = True        # no idle-in-transaction snapshot pinning
            self._conn = conn
            if self._fail_count:
                log.info("%s: reconnected after %d failure(s)", self.name, self._fail_count)
            self._fail_count = 0
            self.last_error = None
            return conn
        except Exception as e:  # psycopg2.OperationalError and friends
            self._fail_count += 1
            delay = RECONNECT_BACKOFF[min(self._fail_count, len(RECONNECT_BACKOFF)) - 1]
            self._next_try = time.monotonic() + delay
            self.last_error = str(e).strip().splitlines()[0][:200]
            raise DatabaseUnavailable(f"{self.name}: {self.last_error}") from e

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    @property
    def available(self) -> bool:
        try:
            self.connect()
            return True
        except DatabaseUnavailable:
            return False

    def _drop(self) -> None:
        self.close()

    # ── query primitives ─────────────────────────────────────────────────────
    def query(self, sql: str, params: tuple | list | None = None) -> list[tuple]:
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return []
                return cur.fetchall()
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            # Connection-level failure (server restarted, HDD unmounted, ...).
            self._drop()
            self.last_error = str(e).strip().splitlines()[0][:200]
            raise DatabaseUnavailable(f"{self.name}: {self.last_error}") from e
        # psycopg2.errors.QueryCanceled (statement_timeout) and other
        # ProgrammingErrors propagate: the connection is still fine.

    def query_dicts(self, sql: str, params=None) -> list[dict]:
        conn = self.connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            self._drop()
            self.last_error = str(e).strip().splitlines()[0][:200]
            raise DatabaseUnavailable(f"{self.name}: {self.last_error}") from e

    def one(self, sql: str, params=None) -> tuple | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params=None) -> None:
        self.query(sql, params)

    # ── change-event helpers (index-friendly) ────────────────────────────────
    def latest(self, mapping: str, table: str = "double") -> tuple[Any, datetime] | None:
        """(value, time) of the newest row for one mapping — one index probe."""
        return self.one(
            f"SELECT value, time FROM {TABLES[table]} "
            f"WHERE mapping = %s ORDER BY time DESC LIMIT 1", (mapping,))

    def latest_many(self, mappings: Iterable[str], table: str = "double") -> dict[str, tuple]:
        out = {}
        for mp in mappings:
            r = self.latest(mp, table)
            if r is not None:
                out[mp] = r
        return out

    def latest_before(self, mapping: str, before: datetime, table: str = "double"):
        return self.one(
            f"SELECT value, time FROM {TABLES[table]} "
            f"WHERE mapping = %s AND time <= %s ORDER BY time DESC LIMIT 1",
            (mapping, before))

    def max_time(self, mapping: str | None = None, table: str = "double") -> datetime | None:
        if mapping is None:
            r = self.one(f"SELECT MAX(time) FROM {TABLES[table]}")
        else:
            r = self.one(f"SELECT MAX(time) FROM {TABLES[table]} WHERE mapping = %s", (mapping,))
        return r[0] if r else None

    def max_id(self, table: str = "double", mappings: Iterable[str] | None = None) -> int:
        if mappings:
            r = self.one(f"SELECT COALESCE(MAX(id), 0) FROM {TABLES[table]} "
                         f"WHERE mapping = ANY(%s)", (list(mappings),))
        else:
            r = self.one(f"SELECT COALESCE(MAX(id), 0) FROM {TABLES[table]}")
        return int(r[0]) if r else 0

    def events_since(self, cursor_id: int, mappings: Iterable[str], table: str = "double",
                     limit: int = 200) -> list[tuple[int, str, Any, datetime]]:
        """Rows (id, mapping, value, time) with id > cursor, oldest first, LIMITed.
        A cursor of 0 means 'never initialised' — the caller must reset it to
        max_id() instead of replaying the entire history."""
        return self.query(
            f"SELECT id, mapping, value, time FROM {TABLES[table]} "
            f"WHERE mapping = ANY(%s) AND id > %s ORDER BY id LIMIT %s",
            (list(mappings), int(cursor_id), int(limit)))

    def series(self, mapping: str, start: datetime, end: datetime, table: str = "double",
               limit: int = 200_000) -> list[tuple[datetime, Any]]:
        return self.query(
            f"SELECT time, value FROM {TABLES[table]} "
            f"WHERE mapping = %s AND time >= %s AND time <= %s ORDER BY time LIMIT %s",
            (mapping, start, end, int(limit)))

    # ── device_states ────────────────────────────────────────────────────────
    def device_states(self) -> dict[str, dict]:
        """{device_id: {'datetime': ts, 'values': json}} — the table holds one
        row per device (PK device_id), ~50 rows, so reading it all is cheap."""
        rows = self.query("SELECT device_id, datetime, values FROM public.device_states")
        return {r[0]: {"datetime": r[1], "values": r[2]} for r in rows}


# ── Per-cycle cache ──────────────────────────────────────────────────────────

@dataclass
class DataContext:
    """Everything a check cycle needs, fetched at most once per cycle.

    The legacy run read device_states five or six times and the same latest
    values from several checks each minute; here each is fetched lazily on
    first use and reused by every alarm in the cycle.
    """
    cs2: Database
    ln2: Database | None = None
    _latest: dict = field(default_factory=dict)
    _latest_bool: dict = field(default_factory=dict)
    _devices: dict | None = None
    _ln2_latest: tuple | None | bool = False       # False = not fetched yet
    started: datetime = field(default_factory=datetime.now)

    def latest(self, mapping: str) -> tuple[Any, datetime] | None:
        if mapping not in self._latest:
            self._latest[mapping] = self.cs2.latest(mapping, "double")
        return self._latest[mapping]

    def latest_value(self, mapping: str) -> float | None:
        r = self.latest(mapping)
        return None if r is None else float(r[0])

    def latest_bool(self, mapping: str) -> tuple[Any, datetime] | None:
        if mapping not in self._latest_bool:
            self._latest_bool[mapping] = self.cs2.latest(mapping, "boolean")
        return self._latest_bool[mapping]

    def devices(self) -> dict[str, dict]:
        if self._devices is None:
            self._devices = self.cs2.device_states()
        return self._devices

    def device(self, device_id: str) -> dict | None:
        """The parsed `values` JSON for one device, or None."""
        d = self.devices().get(device_id)
        if not d:
            return None
        v = d.get("values")
        if isinstance(v, str):
            import json
            try:
                v = json.loads(v)
            except ValueError:
                return None
        return v

    def ln2_latest(self) -> tuple | None:
        """(time, weight, temp, humidity) from the LN2 scale, or None."""
        if self._ln2_latest is False:
            self._ln2_latest = None
            if self.ln2 is not None:
                try:
                    row = self.ln2.one("SELECT time, temp, humidity FROM scale_readings "
                                       "ORDER BY time DESC LIMIT 1")
                    if row:
                        w = self.ln2.one("SELECT weight FROM scale_readings "
                                         "WHERE weight >= 0 ORDER BY time DESC LIMIT 1")
                        self._ln2_latest = (row[0], w[0] if w else None, row[1], row[2])
                except DatabaseUnavailable as e:
                    log.warning("ln2 db unavailable: %s", e)
        return self._ln2_latest
