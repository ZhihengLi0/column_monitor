#!/usr/bin/env python3
"""Record what the LEGACY monitor.py *would* decide this minute, without side
effects. Used as the baseline for the 24 h shadow-run diff against the new
daemon (plan phase 5).

Runs every minute from cron, appends one JSON line to
/mnt/harddrive/pi/logs/legacy_decisions.jsonl:

  {"ts": ..., "mode": ..., "alerts": [[key, text], ...], "cursors": {...},
   "errors": {check_name: str}, "secs": float}

Side effects are blocked by monkey-patching: send_slack / slack_get / upload
become no-ops, record_pulsetube is skipped, and the state is a deep copy that
is never saved. The DB connection is read-only (no commit is ever issued).
"""
import copy
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402  (legacy module)

OUT = Path("/mnt/harddrive/pi/logs/legacy_decisions.jsonl")

# ── neutralise every side effect ──────────────────────────────────────────────
monitor.send_slack = lambda *a, **k: "0.0"           # pretend every post worked
monitor.slack_get = lambda *a, **k: {}                # no ack/reply polling
monitor.slack_upload_image = lambda *a, **k: False
monitor.save_state = lambda *a, **k: None
monitor.record_pulsetube = lambda *a, **k: None

CHECKS = [
    ("freshness",     lambda c, s: [("FRESHNESS", m)] if (m := monitor.check_data_freshness(c, s)) else []),
    ("staleness",     monitor.check_sensor_staleness),
    ("thresholds",    monitor.check_sensor_thresholds),
    ("transitioning", monitor.check_transitioning_devices),
    ("air_pressure",  monitor.check_air_pressure),
    ("device_health", monitor.check_device_health),
    ("milestones",    monitor.check_cooldown_milestone),
    ("dewpoint",      monitor.check_coolant_dewpoint),
    ("pt_temps",      monitor.check_pulsetube_temps),
    ("cs2_alerts",    lambda c, s: [(None, m) for m in monitor.check_cs2_alerts(c, s)]),
    ("r1a",           lambda c, s: [(None, m) for m in monitor.check_r1a_status(c, s)]),
    ("heater",        lambda c, s: [(None, m) for m in monitor.check_heater_status(c, s)]),
    ("valves",        lambda c, s: [(None, m) for m in monitor.check_valve_changes(c, s)]),
    ("cold_cathode",  lambda c, s: [("COLD_CATHODE", m)] if (m := monitor.check_cold_cathode(c, s)) else []),
]
CURSOR_KEYS = ("last_cs2_alert_id", "last_r1a_event_id", "last_r1a_power_id",
               "last_heater_event_id", "last_valve_event_id")


def main() -> None:
    t0 = time.time()
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "alerts": [],
           "errors": {}, "cleared": []}
    state = copy.deepcopy(monitor.load_state())
    try:
        conn = monitor.local_conn()
    except Exception as e:
        rec["errors"]["db"] = str(e)[:200]
        _write(rec, t0)
        return
    cleared = []
    monitor._alarm_clear = (lambda st, key, note=None:
                            cleared.append(key) if key in st.get("active_alarms", {}) else None)
    try:
        try:
            monitor.update_mode(conn, state)
        except Exception as e:
            rec["errors"]["mode"] = str(e)[:200]
        rec["mode"] = state.get("current_mode")
        for name, fn in CHECKS:
            try:
                for key, msg in fn(conn, state) or []:
                    rec["alerts"].append([key, msg])
            except Exception as e:
                rec["errors"][name] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        conn.close()
    rec["cleared"] = cleared
    rec["cursors"] = {k: state.get(k) for k in CURSOR_KEYS}
    rec["active"] = sorted(state.get("active_alarms", {}))
    _write(rec, t0)


def _write(rec: dict, t0: float) -> None:
    rec["secs"] = round(time.time() - t0, 2)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


if __name__ == "__main__":
    main()
