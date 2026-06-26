#!/usr/bin/env python3
"""
BlueFors alert monitor — runs every minute via cron.

Operating modes (auto-detected from 50K_TEMPERATURE):
  IDLE          fridge at room temperature, not running
  TRANSITIONING cooling down or warming up — threshold alerts suppressed
  COLD          fridge operational at base temperature

Usage:
  python3 monitor.py         # normal run
  python3 monitor.py --init  # record current state, skip historical alerts

Slack commands (@BlueFors-Alert <command>):
  help
  pressure reading                  show latest P1–P7 pressure values
  list
  status
  mode                              show current mode
  set mode auto                     return to automatic mode detection
  set mode idle                     force IDLE mode
  set mode cold                     force COLD mode
  ack                               silence ALL sensor alerts 10 min
  change <sensor> to <val> for 5min / 10min / ever
  reset <sensor>
"""

import re
import sys
import time
import json
import logging
import requests
import psycopg2
import psycopg2.extras
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

INIT_MODE = "--init" in sys.argv

sys.path.insert(0, str(Path(__file__).parent))
import config

_handlers = [logging.FileHandler(Path(__file__).parent / "monitor.log")]
if sys.stdout.isatty():
    _handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [monitor] %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent / "monitor_state.json"

# ── Modes ─────────────────────────────────────────────────────────────────────
MODES = ("IDLE", "TRANSITIONING", "COLD")

MODE_EMOJI = {
    "IDLE":          "⚪",
    "TRANSITIONING": "🔵",
    "COLD":          "🟢",
}

MODE_DESC = {
    "IDLE":
        "System is at *room temperature* (not running).\n"
        "Monitoring: idle pressure checks + CS2 system alerts.",
    "TRANSITIONING":
        "System is *cooling down or warming up*.\n"
        "Threshold alerts suppressed — only CS2 system alerts forwarded.",
    "COLD":
        "System is *cold and operational*.\n"
        "Full sensor threshold monitoring active.",
}

# ── Sensor registry ───────────────────────────────────────────────────────────
SENSOR_LIST_COLD = [
    ("MXC_TEMPERATURE",     "MXC",    "Mixing chamber temperature"),
    ("MXC_TEMPERATURE_FAR", "MXCFAR", "Mixing chamber far-end temperature"),
    ("STILL_TEMPERATURE",   "STILL",  "Still temperature"),
    ("4K_TEMPERATURE",      "4K",     "4K plate temperature"),
    ("50K_TEMPERATURE",     "50K",    "50K plate temperature"),
    ("B1A_TEMPERATURE",     "B1A",    "B1A stage temperature"),
    ("B2_TEMPERATURE",      "B2",     "B2 stage temperature"),
    ("P1_PRESSURE",         "P1",     "P1 return pressure"),
    ("P2_PRESSURE",         "P2",     "P2 still pressure"),
    ("P5_PRESSURE",         "P5",     "P5 MXC pressure"),
    ("FLOW_VALUE",          "FLOW",   "Helium flow rate"),
]

SENSOR_LIST_IDLE = [
    ("P2_PRESSURE", "P2",   "P2 still pressure"),
    ("P5_PRESSURE", "P5",   "P5 MXC pressure"),
]

SENSOR_LOOKUP = {}
for _i, (_full, _short, _) in enumerate(SENSOR_LIST_COLD, 1):
    SENSOR_LOOKUP[str(_i)]        = _full
    SENSOR_LOOKUP[_short.upper()] = _full
    SENSOR_LOOKUP[_full.upper()]  = _full
for _full, _short, _ in SENSOR_LIST_IDLE:
    SENSOR_LOOKUP[_short.upper()] = _full
    SENSOR_LOOKUP[_full.upper()]  = _full

UNITS = {
    "MXC_TEMPERATURE": "K",    "MXC_TEMPERATURE_FAR": "K",
    "STILL_TEMPERATURE": "K",  "4K_TEMPERATURE": "K",  "50K_TEMPERATURE": "K",
    "B1A_TEMPERATURE": "K",    "B2_TEMPERATURE": "K",
    "P1_PRESSURE": "mbar",     "P2_PRESSURE": "mbar",  "P5_PRESSURE": "mbar",
    "FLOW_VALUE": "mmol/s",
}

ACK_REACTIONS = {"white_check_mark", "heavy_check_mark", "clap", "+1", "ok_hand"}

# ── State ─────────────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {
        "last_alert_time": {},
        "last_cs2_alert_id": 0,
        "last_r1a_event_id": 0,
        "last_r1a_power_id": 0,
        "last_r1a_power_value": None,
        "acked_sensors": {},
        "pending_alert_msgs": {},
        "threshold_overrides": {},
        "last_slack_ts": "0",
        "last_freshness_alert": None,
        "current_mode": None,       # IDLE / TRANSITIONING / COLD
        "mode_override": None,      # if manually set via Slack
        "mode_since": None,
    }


def load_state() -> dict:
    base = _empty_state()
    if STATE_FILE.exists():
        text = STATE_FILE.read_text().strip()
        if text:
            saved = json.loads(text)
            base.update(saved)
    return base


def save_state(state: dict):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, default=str, indent=2))
    tmp.replace(STATE_FILE)  # atomic: never leaves the file half-written

# ── DB ────────────────────────────────────────────────────────────────────────

def local_conn():
    kw = dict(host=config.LOCAL_PG_HOST, port=config.LOCAL_PG_PORT,
               user=config.LOCAL_PG_USER, dbname=config.LOCAL_PG_DB, connect_timeout=5)
    if config.LOCAL_PG_PASSWORD:
        kw["password"] = config.LOCAL_PG_PASSWORD
    return psycopg2.connect(**kw)

# ── Slack helpers ─────────────────────────────────────────────────────────────

def _headers():
    return {"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"}


def slack_get(endpoint: str, params: dict) -> dict:
    try:
        r = requests.get(f"https://slack.com/api/{endpoint}",
                         params=params, headers=_headers(), timeout=10)
        return r.json()
    except Exception as e:
        log.error(f"Slack GET {endpoint}: {e}")
        return {}


def send_slack(text: str, color: str = "danger", thread_ts: str = None) -> str | None:
    if not config.SLACK_BOT_TOKEN or not config.SLACK_CHANNEL:
        log.warning(f"[SLACK NOT CONFIGURED] {text}")
        return None
    payload = {
        "channel": config.SLACK_CHANNEL,
        "attachments": [{
            "color": color,
            "text": text,
            "footer": f"BlueFors Monitor | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        }],
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        r = requests.post("https://slack.com/api/chat.postMessage",
                          json=payload, headers=_headers(), timeout=10)
        resp = r.json()
        if resp.get("ok"):
            return resp.get("ts")
        log.error(f"Slack error: {resp.get('error')}")
    except Exception as e:
        log.error(f"Slack send failed: {e}")
    return None

# ── Mode detection ────────────────────────────────────────────────────────────

def detect_mode(conn) -> str:
    """Determine operating mode from 50K plate temperature."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM public.double_value_change_events "
                "WHERE mapping = %s ORDER BY time DESC LIMIT 1",
                (config.MODE_DETECTION_SENSOR,)
            )
            row = cur.fetchone()
    except Exception:
        return "IDLE"

    if row is None:
        return "IDLE"

    temp = row[0]
    if temp > config.MODE_IDLE_ABOVE_K:
        return "IDLE"
    elif temp < config.MODE_COLD_BELOW_K:
        return "COLD"
    else:
        return "TRANSITIONING"


def update_mode(conn, state: dict):
    """Detect mode, handle transitions, notify Slack if mode changed."""
    # If manually overridden, use that
    if state.get("mode_override"):
        new_mode = state["mode_override"]
    else:
        new_mode = detect_mode(conn)

    old_mode = state.get("current_mode")

    if new_mode == old_mode:
        return  # no change

    state["current_mode"] = new_mode
    state["mode_since"]   = datetime.now().isoformat()

    emoji = MODE_EMOJI[new_mode]
    msg = (
        f"{emoji} *Mode changed: {old_mode or 'STARTUP'} → {new_mode}*\n"
        f"{MODE_DESC[new_mode]}"
    )

    color_map = {"IDLE": "#aaaaaa", "TRANSITIONING": "#0066cc", "COLD": "good"}
    send_slack(msg, color=color_map[new_mode])
    log.info(f"Mode changed: {old_mode} → {new_mode}")

    # Clear per-sensor alert cooldowns so the new mode's thresholds start fresh
    state["last_alert_time"] = {}
    state["acked_sensors"]   = {}

# ── Threshold helpers ─────────────────────────────────────────────────────────

def resolve_sensor(key: str):
    return SENSOR_LOOKUP.get(key.upper().strip())


def active_thresholds(state: dict) -> dict:
    """Return the threshold dict for the current mode."""
    mode = state.get("current_mode", "IDLE")
    if mode == "COLD":
        return config.THRESHOLDS_COLD
    elif mode == "IDLE":
        return config.THRESHOLDS_IDLE
    else:
        return {}   # TRANSITIONING: no threshold checks


def get_threshold(name: str, state: dict):
    """Return (max_val, min_val) considering active overrides."""
    overrides = state.get("threshold_overrides", {})
    if name in overrides:
        ov = overrides[name]
        exp = ov.get("expires_at")
        if exp is None or datetime.fromisoformat(exp) > datetime.now():
            return ov.get("max_val"), ov.get("min_val")
        del overrides[name]
        log.info(f"Threshold override for {name} expired")
    thresholds = active_thresholds(state)
    entry = thresholds.get(name, (None, None, ""))
    return entry[0], entry[1]

# ── Slack polling: acks ───────────────────────────────────────────────────────

def check_acknowledgements(state: dict):
    pending   = state.setdefault("pending_alert_msgs", {})
    acked     = state.setdefault("acked_sensors", {})
    ack_until = (datetime.now() + timedelta(minutes=10)).isoformat()

    for sensor in list(pending.keys()):
        ch = pending[sensor]["channel"]
        ts = pending[sensor]["ts"]
        found = False

        data = slack_get("reactions.get", {"channel": ch, "timestamp": ts})
        if data.get("ok"):
            rxns = data.get("message", {}).get("reactions", [])
            if any(r["name"] in ACK_REACTIONS for r in rxns):
                found = True

        if not found:
            data = slack_get("conversations.replies", {"channel": ch, "ts": ts})
            if data.get("ok"):
                for reply in data.get("messages", [])[1:]:
                    if reply.get("text", "").strip().lower() == "ok":
                        found = True
                        break

        if found:
            acked[sensor] = ack_until
            del pending[sensor]
            log.info(f"{sensor} acknowledged — silenced 10 min")

# ── Slack polling: commands ───────────────────────────────────────────────────

def check_commands(state: dict, conn=None):
    last_ts = state.get("last_slack_ts", "0")
    data = slack_get("conversations.history",
                     {"channel": config.SLACK_CHANNEL, "oldest": last_ts, "limit": 50})
    if not data.get("ok"):
        log.debug(f"conversations.history: {data.get('error')}")
        return

    bot_tag = f"<@{config.SLACK_BOT_USER_ID}>"
    new_ts  = last_ts

    for msg in reversed(data.get("messages", [])):
        ts = msg.get("ts", "0")
        if ts <= last_ts:
            continue
        if ts > new_ts:
            new_ts = ts
        text = msg.get("text", "")
        if bot_tag in text:
            clean = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
            _execute_command(clean, ts, state, conn)

    state["last_slack_ts"] = new_ts


def _execute_command(text: str, reply_ts: str, state: dict, conn=None):
    lower = text.lower().strip()

    if lower in ("", "help"):
        _cmd_help(reply_ts)
        return

    if re.fullmatch(r"pressure\s+reading", lower):
        _cmd_pressure(reply_ts, conn)
        return

    if lower == "list":
        _cmd_list(state, reply_ts)
        return

    if lower in ("status", "mode"):
        _cmd_status(state, reply_ts)
        return

    # set mode auto / idle / cold / transitioning
    m = re.fullmatch(r"set\s+mode\s+(\S+)", text, re.IGNORECASE)
    if m:
        val = m.group(1).upper()
        if val == "AUTO":
            state["mode_override"] = None
            send_slack("✅ Mode detection set to *automatic* (based on 50K temperature).",
                       color="good", thread_ts=reply_ts)
            log.info("Mode override cleared — back to auto")
        elif val in MODES:
            state["mode_override"]   = val
            state["current_mode"]    = val
            state["mode_since"]      = datetime.now().isoformat()
            state["last_alert_time"] = {}
            state["acked_sensors"]   = {}
            emoji = MODE_EMOJI[val]
            send_slack(f"✅ Mode manually set to *{val}*. {emoji}\n{MODE_DESC[val]}",
                       color="good", thread_ts=reply_ts)
            log.info(f"Mode manually set to {val}")
        else:
            send_slack(
                f"Unknown mode `{m.group(1)}`. Valid options: `auto`, `idle`, `cold`.",
                thread_ts=reply_ts)
        return

    if lower == "ack":
        until = (datetime.now() + timedelta(minutes=10)).isoformat()
        for s in {**config.THRESHOLDS_COLD, **config.THRESHOLDS_IDLE}:
            state.setdefault("acked_sensors", {})[s] = until
        state.setdefault("pending_alert_msgs", {}).clear()
        send_slack("✅ All active alerts acknowledged — silenced for 10 minutes.",
                   color="good", thread_ts=reply_ts)
        log.info("All alerts acked via Slack")
        return

    m = re.fullmatch(r"reset\s+(\S+)", text, re.IGNORECASE)
    if m:
        sensor = resolve_sensor(m.group(1))
        if not sensor:
            send_slack(f"Unknown sensor `{m.group(1)}`. Use `list` to see all sensors.",
                       thread_ts=reply_ts)
            return
        state.setdefault("threshold_overrides", {}).pop(sensor, None)
        mode_t = active_thresholds(state)
        all_t  = {**config.THRESHOLDS_COLD, **config.THRESHOLDS_IDLE}
        entry  = mode_t.get(sensor) or all_t.get(sensor, (None, None, ""))
        default_val = entry[0] if entry[0] is not None else entry[1]
        send_slack(f"✅ *{sensor}* reset to default: `{default_val} {UNITS.get(sensor, '')}`",
                   color="good", thread_ts=reply_ts)
        log.info(f"{sensor} threshold reset to default")
        return

    m = re.fullmatch(r"change\s+(\S+)\s+to\s+([\d.e+\-]+)\s+for\s+(.+)",
                     text, re.IGNORECASE)
    if m:
        sensor = resolve_sensor(m.group(1))
        if not sensor:
            send_slack(f"Unknown sensor `{m.group(1)}`. Use `list` to see all sensors.",
                       thread_ts=reply_ts)
            return
        try:
            new_val = float(m.group(2))
        except ValueError:
            send_slack(f"Invalid value `{m.group(2)}`.", thread_ts=reply_ts)
            return

        raw_dur = m.group(3).strip().lower()
        if raw_dur in ("ever", "forever", "permanent", "permanently"):
            expires_at = None
            dur_text   = "*permanently*"
        else:
            mins_m = re.match(r"(\d+)\s*min", raw_dur)
            if not mins_m:
                send_slack(
                    f"Unknown duration `{m.group(3)}`. "
                    "Use `for 5min`, `for 10min`, or `for ever`.",
                    thread_ts=reply_ts)
                return
            mins       = int(mins_m.group(1))
            expires_at = (datetime.now() + timedelta(minutes=mins)).isoformat()
            dur_text   = f"for *{mins} minutes*"

        all_t = {**config.THRESHOLDS_COLD, **config.THRESHOLDS_IDLE}
        entry = all_t.get(sensor, (None, None, ""))
        if entry[0] is not None:
            ov = {"max_val": new_val, "min_val": entry[1], "expires_at": expires_at}
        else:
            ov = {"max_val": entry[0], "min_val": new_val, "expires_at": expires_at}
        state.setdefault("threshold_overrides", {})[sensor] = ov

        unit = UNITS.get(sensor, "")
        send_slack(f"✅ *{sensor}* threshold → `{new_val} {unit}` {dur_text}.",
                   color="good", thread_ts=reply_ts)
        log.info(f"{sensor} threshold → {new_val} {dur_text}")
        return

    send_slack(
        f"I didn't understand: `{text}`\n"
        "Type `@BlueFors-Alert help` to see all commands.",
        thread_ts=reply_ts)


def _cmd_pressure(reply_ts: str, conn=None):
    PRESSURE_MAPPINGS = [
        ("P1_PRESSURE", "P1"),
        ("P2_PRESSURE", "P2"),
        ("P3_PRESSURE", "P3"),
        ("P4_PRESSURE", "P4"),
        ("P5_PRESSURE", "P5"),
        ("P6_PRESSURE", "P6"),
        ("P7_PRESSURE", "P7"),
    ]
    if conn is None:
        send_slack("Cannot read pressures: no database connection.", thread_ts=reply_ts)
        return

    lines = [":thermometer: *Current Pressure Readings*\n"]
    with conn.cursor() as cur:
        for mapping, label in PRESSURE_MAPPINGS:
            cur.execute(
                "SELECT value, time FROM public.double_value_change_events "
                "WHERE mapping = %s ORDER BY time DESC LIMIT 1",
                (mapping,))
            row = cur.fetchone()
            if row:
                val, ts = float(row[0]), row[1]
                lines.append(f"  *{label}*: `{val:.4g} mbar`  _(at {str(ts)[:19]})_")
            else:
                lines.append(f"  *{label}*: _no data_")

    send_slack("\n".join(lines), color="#0066cc", thread_ts=reply_ts)
    log.info("Sent pressure reading reply to Slack")


def _cmd_help(reply_ts=None):
    send_slack(
        "*BlueFors Monitor — Commands*\n\n"
        "*Acknowledge an alert (silences sensor 10 min):*\n"
        "  React ✅  👏  👍  🤙 on the alert, or reply `ok` / `OK` in the thread\n\n"
        "*@mention commands* (`@BlueFors-Alert <command>`):\n"
        "`help` — show this message\n"
        "`pressure reading` — show latest P1–P7 pressure values\n"
        "`mode` — show current operating mode and what is being monitored\n"
        "`set mode auto` — automatic mode detection (based on 50K temperature)\n"
        "`set mode idle` — force IDLE mode (room temperature monitoring)\n"
        "`set mode cold` — force COLD mode (operational monitoring)\n"
        "`list` — sensor numbers, short names, current thresholds\n"
        "`status` — active overrides and silenced sensors\n"
        "`ack` — silence ALL sensors for 10 min\n"
        "`change <sensor> to <value> for 5min` — 5-min threshold override\n"
        "`change <sensor> to <value> for 10min` — 10-min threshold override\n"
        "`change <sensor> to <value> for ever` — permanent threshold change\n"
        "`reset <sensor>` — restore default threshold\n\n"
        "_<sensor> = number (see `list`), short name, or full mapping name_",
        color="#0066cc", thread_ts=reply_ts)


def _cmd_list(state: dict, reply_ts=None):
    mode = state.get("current_mode", "IDLE")
    emoji = MODE_EMOJI.get(mode, "")
    lines = [f"*Sensor List — current mode: {emoji} {mode}*\n"]

    lines.append("*COLD mode thresholds:*")
    for i, (full, short, desc) in enumerate(SENSOR_LIST_COLD, 1):
        entry = config.THRESHOLDS_COLD.get(full, (None, None, ""))
        max_v, min_v = entry[0], entry[1]
        unit = UNITS.get(full, "")
        thr  = f"> `{max_v} {unit}`" if max_v is not None else f"< `{min_v} {unit}`"
        ov   = " *(overridden)*" if full in state.get("threshold_overrides", {}) else ""
        lines.append(f"  `{i:2d}` `{short:<7}` {desc} — alert if {thr}{ov}")

    lines.append("\n*IDLE mode thresholds (room temperature):*")
    for full, short, desc in SENSOR_LIST_IDLE:
        entry = config.THRESHOLDS_IDLE.get(full, (None, None, ""))
        max_v, min_v = entry[0], entry[1]
        unit = UNITS.get(full, "")
        thr  = f"> `{max_v} {unit}`" if max_v is not None else f"< `{min_v} {unit}`"
        ov   = " *(overridden)*" if full in state.get("threshold_overrides", {}) else ""
        lines.append(f"  `{short:<7}` {desc} — alert if {thr}{ov}")

    lines.append(
        "\n_Example: `@BlueFors-Alert change 1 to 0.05 for 5min`_\n"
        "_Example: `@BlueFors-Alert set mode cold`_")
    send_slack("\n".join(lines), color="#0066cc", thread_ts=reply_ts)


def _cmd_status(state: dict, reply_ts=None):
    mode      = state.get("current_mode", "unknown")
    since     = state.get("mode_since", "")[:16]
    override  = state.get("mode_override")
    emoji     = MODE_EMOJI.get(mode, "")

    lines = [
        f"*Monitor Status*\n",
        f"*Mode:* {emoji} `{mode}`"
        + (f" *(manually set)*" if override else f" *(auto-detected)*"),
        f"*Since:* {since}",
        f"\n_{MODE_DESC.get(mode, '')}_",
    ]

    overrides = state.get("threshold_overrides", {})
    if overrides:
        lines.append("\n*Active threshold overrides:*")
        for sensor, ov in overrides.items():
            val = ov.get("max_val") if ov.get("max_val") is not None else ov.get("min_val")
            exp = ov.get("expires_at")
            lines.append(f"  • `{sensor}`: `{val} {UNITS.get(sensor, '')}` "
                         f"({'permanent' if exp is None else 'until ' + exp[:16]})")

    acked = state.get("acked_sensors", {})
    now   = datetime.now()
    active_acks = {s: t for s, t in acked.items()
                   if datetime.fromisoformat(t) > now}
    if active_acks:
        lines.append("\n*Silenced sensors:*")
        for s, until in active_acks.items():
            lines.append(f"  • `{s}` until `{until[:16]}`")

    send_slack("\n".join(lines), color="#0066cc", thread_ts=reply_ts)

# ── Alert checks ──────────────────────────────────────────────────────────────

def check_sensor_thresholds(conn, state: dict) -> list:
    thresholds = active_thresholds(state)
    if not thresholds:
        return []   # TRANSITIONING — no threshold alerts

    results  = []
    now      = datetime.now()
    cooldown = timedelta(minutes=config.ALERT_COOLDOWN_MINUTES)
    acked    = state.setdefault("acked_sensors", {})

    mappings = list(thresholds.keys())
    ph = ",".join(["%s"] * len(mappings))
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            f"SELECT DISTINCT ON (mapping) mapping, value, time "
            f"FROM public.double_value_change_events "
            f"WHERE mapping IN ({ph}) ORDER BY mapping, time DESC",
            mappings)
        rows = cur.fetchall()

    for row in rows:
        name  = row["mapping"]
        value = row["value"]
        ts    = row["time"]
        max_v, min_v = get_threshold(name, state)
        _, _, desc = thresholds[name]

        if not ((max_v is not None and value > max_v) or
                (min_v is not None and value < min_v)):
            continue

        ack_until = acked.get(name)
        if ack_until and datetime.fromisoformat(ack_until) > now:
            continue

        last = state["last_alert_time"].get(name)
        if last and now - datetime.fromisoformat(last) < cooldown:
            continue

        state["last_alert_time"][name] = now.isoformat()
        unit = UNITS.get(name, "")
        mode = state.get("current_mode", "")
        mode_tag = f" _[{mode} mode]_" if mode else ""
        msg = (f":warning: *{desc}*{mode_tag}\n"
               f"Current: `{value:.4g} {unit}` | Time: {ts}\n"
               f"_React ✅ or reply `ok` in thread to silence 10 min_")
        results.append((name, msg))
        log.warning(f"Threshold alert [{mode}]: {name} = {value:.4g}")

    return results


def check_cs2_alerts(conn, state: dict) -> list:
    last_id = state.get("last_cs2_alert_id", 0)
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT id, code, datetime, title, description, severity "
            "FROM public.alerts WHERE id > %s AND severity >= %s ORDER BY id LIMIT 50",
            (last_id, config.CS2_ALERT_MIN_SEVERITY))
        rows = cur.fetchall()
    if not rows:
        return []

    state["last_cs2_alert_id"] = max(r["id"] for r in rows)
    by_code = defaultdict(list)
    for row in rows:
        by_code[row["code"]].append(row)

    msgs = []
    for code, group in by_code.items():
        row   = group[0]
        emoji = ":red_circle:" if row["severity"] >= 2 else ":yellow_circle:"
        kind  = "Error" if row["severity"] >= 2 else "Warning"
        cnt   = f" (×{len(group)})" if len(group) > 1 else ""
        msgs.append(
            f"{emoji} *CS2 {kind}* [code {code}]{cnt}\n"
            f"*{row['title']}*\n{row['description'] or ''}\n"
            f"First: {group[0]['datetime']}  Last: {group[-1]['datetime']}")
        log.warning(f"CS2 alert ×{len(group)}: [{code}] {row['title']}")
    return msgs


def check_r1a_status(conn, state: dict) -> list:
    msgs = []

    # Boolean status changes (enabled / error)
    last_id = state.get("last_r1a_event_id", 0)
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT id, mapping, value, time FROM public.boolean_value_change_events "
            "WHERE mapping IN ('R1A_ENABLED', 'R1A_ERROR_VALUE') AND id > %s "
            "ORDER BY id",
            (last_id,))
        bool_rows = cur.fetchall()

    if bool_rows:
        state["last_r1a_event_id"] = max(r["id"] for r in bool_rows)
        for row in bool_rows:
            if row["mapping"] == "R1A_ENABLED":
                status = ":large_green_circle: *ON*" if row["value"] else ":red_circle: *OFF*"
                msg = f":gear: *R1A Pump enabled changed* → {status}\nTime: {row['time']}"
            else:
                status = ":red_circle: *ERROR*" if row["value"] else ":white_check_mark: *Cleared*"
                msg = f":warning: *R1A Pump error changed* → {status}\nTime: {row['time']}"
            msgs.append(msg)
            log.info(f"R1A status change: {row['mapping']} = {row['value']}")

    # Pump power: alert when crossing zero (pump stopped / restarted)
    last_power_id    = state.get("last_r1a_power_id", 0)
    last_power_value = state.get("last_r1a_power_value")
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT id, value, time FROM public.double_value_change_events "
            "WHERE mapping = 'R1A_PUMP_POWER' AND id > %s ORDER BY id",
            (last_power_id,))
        power_rows = cur.fetchall()

    if power_rows:
        state["last_r1a_power_id"] = max(r["id"] for r in power_rows)
        latest_power = float(power_rows[-1]["value"])
        latest_time  = power_rows[-1]["time"]
        prev = last_power_value

        was_running = prev is not None and prev > 0.1
        now_running = latest_power > 0.1

        if prev is not None and was_running != now_running:
            if now_running:
                msg = (f":large_green_circle: *R1A Pump power ON* → {latest_power:.1f} W\n"
                       f"Time: {latest_time}")
            else:
                msg = (f":red_circle: *R1A Pump power OFF* → {latest_power:.1f} W\n"
                       f"Time: {latest_time}")
            msgs.append(msg)
            log.info(f"R1A pump power transition: {prev:.1f}W → {latest_power:.1f}W")

        state["last_r1a_power_value"] = latest_power

    return msgs


def check_data_freshness(conn, state: dict):
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(time) FROM public.double_value_change_events")
        latest = cur.fetchone()[0]
    if latest is None:
        return ":sos: No sensor data in local database!"
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - latest
    if age <= timedelta(minutes=5):
        return None
    last = state.get("last_freshness_alert")
    if last and datetime.now() - datetime.fromisoformat(last) < timedelta(minutes=30):
        return None
    state["last_freshness_alert"] = datetime.now().isoformat()
    return (f":sos: *Data sync may have stopped!* "
            f"Latest reading is {int(age.total_seconds()/60)} min old.")

# ── Init ──────────────────────────────────────────────────────────────────────

def init_state(conn) -> dict:
    state = _empty_state()
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(id) FROM public.alerts WHERE severity >= %s",
                    (config.CS2_ALERT_MIN_SEVERITY,))
        state["last_cs2_alert_id"] = cur.fetchone()[0] or 0
        cur.execute("SELECT MAX(id) FROM public.boolean_value_change_events "
                    "WHERE mapping IN ('R1A_ENABLED', 'R1A_ERROR_VALUE')")
        state["last_r1a_event_id"] = cur.fetchone()[0] or 0
        cur.execute("SELECT id, value FROM public.double_value_change_events "
                    "WHERE mapping = 'R1A_PUMP_POWER' ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if row:
            state["last_r1a_power_id"]    = row[0]
            state["last_r1a_power_value"] = float(row[1])
    now = datetime.now()
    for mapping in {**config.THRESHOLDS_COLD, **config.THRESHOLDS_IDLE}:
        state["last_alert_time"][mapping] = now.isoformat()
    state["last_slack_ts"]        = f"{time.time():.6f}"
    state["last_freshness_alert"] = now.isoformat()
    log.info(f"Initialised: last_cs2_alert_id={state['last_cs2_alert_id']}, "
             f"last_r1a_event_id={state['last_r1a_event_id']}, "
             f"last_r1a_power={state['last_r1a_power_value']:.1f}W, "
             f"skipped historical alerts")
    return state

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    state = load_state()

    try:
        conn = local_conn()
    except Exception as e:
        log.error(f"DB connect failed: {e}")
        if not INIT_MODE:
            send_slack(f":sos: *BlueFors Monitor* cannot connect to database: {e}")
        return

    try:
        if INIT_MODE:
            state = init_state(conn)
            save_state(state)
            log.info("--init complete.")
            return

        # 1. Poll Slack for acks and commands
        check_acknowledgements(state)
        check_commands(state, conn)

        # 2. Detect / update operating mode
        update_mode(conn, state)

        # 3. Run checks
        all_alerts = []

        freshness = check_data_freshness(conn, state)
        if freshness:
            all_alerts.append((None, freshness))

        all_alerts.extend(check_sensor_thresholds(conn, state))

        for msg in check_cs2_alerts(conn, state):
            all_alerts.append((None, msg))

        for msg in check_r1a_status(conn, state):
            all_alerts.append((None, msg))

    finally:
        conn.close()

    # 4. Send alerts and track message timestamps for ack tracking
    pending = state.setdefault("pending_alert_msgs", {})
    for sensor_name, msg in all_alerts:
        ts = send_slack(msg)
        if ts and sensor_name:
            pending[sensor_name] = {"ts": ts, "channel": config.SLACK_CHANNEL}

    save_state(state)
    if all_alerts:
        log.info(f"Sent {len(all_alerts)} alert(s)")
    else:
        log.debug("No alerts this cycle")


if __name__ == "__main__":
    run()
