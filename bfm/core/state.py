"""StateStore — the single owner of monitor_state.json.

Legacy problem: cron `monitor.py` and the persistent responder each did
load → modify → save on the same file, so whoever saved last won (observed:
`set mode` executed twice, plots parsed twice, acks reverted). In the daemon
there is exactly one StateStore; every thread mutates it under `lock` and the
check cycle persists it atomically.

File format is unchanged (same keys, naive-local ISO timestamps) so the live
file migrates in place. `migrate()` only prunes garbage and fixes known bugs.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from . import timeutil as tu

log = logging.getLogger("bfm.state")

OUTBOX_EVENT_CAP = 200          # non-keyed (event) messages kept while Slack is down


def empty_state() -> dict:
    return {
        "last_alert_time": {},
        "last_cs2_alert_id": 0,
        "last_r1a_event_id": 0,
        "last_r1a_power_id": 0,
        "last_r1a_power_value": None,
        "last_heater_event_id": 0,
        "last_valve_event_id": 0,
        "acked_sensors": {},
        "pending_alert_msgs": {},
        "active_alarms": {},
        "threshold_overrides_cold": {},
        "threshold_overrides_idle": {},
        "last_slack_ts": "0",
        "current_mode": None,
        "mode_override": None,
        "mode_since": None,
        "cs2_alerts_enabled": True,
        "alerts_paused": False,
        "outbox": [],
        "slack": {"ok": True, "down_since": None, "dropped_events": 0,
                  "skipped_commands": 0},
    }


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")
        self.lock = threading.RLock()
        self.data: dict = empty_state()
        self.dirty = False
        self._outbox_seq = 0

    # ── persistence ──────────────────────────────────────────────────────────
    def load(self) -> "StateStore":
        base = empty_state()
        try:
            with open(self.lock_path, "a") as lf:
                fcntl.flock(lf, fcntl.LOCK_SH)
                if self.path.exists():
                    text = self.path.read_text().strip()
                    if text:
                        base.update(json.loads(text))
        except json.JSONDecodeError as e:
            # Do NOT silently start fresh: cursors at 0 would replay history.
            backup = self.path.with_suffix(f".corrupt.{int(tu.now().timestamp())}")
            log.error("state file corrupt (%s); keeping a copy at %s", e, backup)
            try:
                self.path.replace(backup)
            except OSError:
                pass
        with self.lock:
            self.data = base
            self.migrate()
        return self

    def save(self) -> None:
        with self.lock:
            payload = json.dumps(self.data, default=str, indent=2)
            self.dirty = False
        with open(self.lock_path, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(payload)
            tmp.replace(self.path)

    @contextmanager
    def locked(self) -> Iterator[dict]:
        with self.lock:
            self.dirty = True
            yield self.data

    # ── migration / hygiene ──────────────────────────────────────────────────
    def migrate(self) -> None:
        """Bring a legacy state file up to date. Idempotent."""
        d = self.data
        # Legacy flat overrides → mode-specific (and the old key is dropped so
        # `list`/`status` stop reading an always-empty dict).
        legacy = d.pop("threshold_overrides", None)
        if legacy:
            mode = d.get("current_mode", "IDLE")
            target = "threshold_overrides_cold" if mode == "COLD" else "threshold_overrides_idle"
            d.setdefault(target, {}).update(legacy)
        d.pop("pulsetube_flags", None)          # orphan key, never read
        d.pop("last_freshness_alert", None)     # freshness now uses last_alert_time
        d.setdefault("outbox", [])
        d.setdefault("slack", empty_state()["slack"])
        d.setdefault("active_alarms", {})
        self.prune()

    def prune(self) -> None:
        """Drop expired silences and stale pending entries (legacy: 30 pending
        with 0 active alarms → 60 Slack GETs per minute)."""
        d = self.data
        now = tu.now()
        acked = d.setdefault("acked_sensors", {})
        for k in list(acked):
            if not tu.is_future(acked[k]):
                del acked[k]
        pending = d.setdefault("pending_alert_msgs", {})
        active = d.get("active_alarms", {}) or {}
        for k in list(pending):
            ts = pending[k].get("ts") if isinstance(pending[k], dict) else None
            try:
                posted = datetime.fromtimestamp(float(ts))   # Slack ts = epoch seconds
            except (TypeError, ValueError):
                posted = now
            if k not in active and now - posted > timedelta(hours=24):
                del pending[k]
        ov_cold = d.setdefault("threshold_overrides_cold", {})
        ov_idle = d.setdefault("threshold_overrides_idle", {})
        for ov in (ov_cold, ov_idle):
            for k in list(ov):
                exp = ov[k].get("expires_at") if isinstance(ov[k], dict) else None
                if exp is not None and not tu.is_future(exp):
                    del ov[k]

    # ── convenience accessors ────────────────────────────────────────────────
    def get(self, key: str, default: Any = None) -> Any:
        with self.lock:
            return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self.locked() as d:
            d[key] = value

    # ── outbox ───────────────────────────────────────────────────────────────
    def enqueue(self, text: str, *, color: str = "danger", thread_ts: str | None = None,
                key: str | None = None, kind: str = "event") -> dict:
        """Queue an outgoing Slack message.

        kind: "alarm"  keyed condition alarm — collapsed: a newer message with
                       the same key REPLACES the queued one (keeps created ts)
              "clear"  keyed '✅ Cleared' — if the alarm it clears is still
                       queued (never seen by anyone) both are dropped
              "event"  changelog (CS2 alert, pump on/off ...) — capped
              "reply"  command reply / reminder — never collapsed
        """
        with self.locked() as d:
            ob: list = d.setdefault("outbox", [])
            now_iso = tu.now().isoformat()
            if kind == "alarm" and key:
                for m in ob:
                    if m.get("kind") == "alarm" and m.get("key") == key:
                        m.update(text=text, color=color, thread_ts=thread_ts,
                                 updated=now_iso, collapsed=m.get("collapsed", 0) + 1)
                        return m
            if kind == "clear" and key:
                before = len(ob)
                ob[:] = [m for m in ob
                         if not (m.get("kind") == "alarm" and m.get("key") == key)]
                if len(ob) < before:
                    log.info("outbox: alarm %s cleared before it was ever posted — dropped both", key)
                    return {"dropped": True, "key": key}
            if kind == "event":
                n_events = sum(1 for m in ob if m.get("kind") == "event")
                if n_events >= OUTBOX_EVENT_CAP:
                    d.setdefault("slack", {})["dropped_events"] = \
                        d.get("slack", {}).get("dropped_events", 0) + 1
                    return {"dropped": True}
            self._outbox_seq += 1
            m = {"id": f"{int(tu.now().timestamp())}-{self._outbox_seq}", "kind": kind,
                 "key": key, "text": text, "color": color, "thread_ts": thread_ts,
                 "created": now_iso, "attempts": 0}
            ob.append(m)
            return m

    def outbox_peek(self) -> dict | None:
        with self.lock:
            ob = self.data.get("outbox", [])
            return dict(ob[0]) if ob else None

    def outbox_done(self, msg_id: str, ts: str | None) -> dict | None:
        """Remove a sent message; return it (with 'ts') for post-send hooks."""
        with self.locked() as d:
            ob = d.get("outbox", [])
            for i, m in enumerate(ob):
                if m.get("id") == msg_id:
                    sent = ob.pop(i)
                    sent["ts"] = ts
                    return sent
        return None

    def outbox_failed(self, msg_id: str) -> None:
        with self.locked() as d:
            for m in d.get("outbox", []):
                if m.get("id") == msg_id:
                    m["attempts"] = m.get("attempts", 0) + 1
                    return

    def outbox_len(self) -> int:
        with self.lock:
            return len(self.data.get("outbox", []))
