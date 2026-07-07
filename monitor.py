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
  set mode transitioning            force TRANSITIONING mode (suppress threshold alerts)
  ack                               silence ALL sensor alerts 10 min
  (reply in an alert thread)        "silent for 2h" / "mute 30min" / "silence 3 days" / "silent forever"
  change <sensor> to <val> for 5min / 10min / ever
  reset <sensor>
"""

import re
import sys
import os
import time
import json
import fcntl
import logging
import tempfile
import requests
import numpy as np
import psycopg2
import psycopg2.extras
import matplotlib
matplotlib.use("Agg")   # non-interactive backend (no display needed)
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

INIT_MODE    = "--init"    in sys.argv
SUMMARY_MODE = "--summary" in sys.argv

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
        "Threshold alerts suppressed, but the Pulse Tube is checked —\n"
        "a *critical* alert fires if it turns OFF.",
    "COLD":
        "System is *cold and operational*.\n"
        "Full sensor threshold monitoring active.",
}

# Accept short / loose spellings for each mode.
_MODE_ALIASES = {
    "AUTO": "AUTO", "AUTOMATIC": "AUTO",
    "IDLE": "IDLE", "ROOM": "IDLE", "WARM": "IDLE",
    "COLD": "COLD", "BASE": "COLD", "OPERATIONAL": "COLD",
    "TRANSITIONING": "TRANSITIONING", "TRANSITION": "TRANSITIONING",
    "TRANS": "TRANSITIONING", "COOLING": "TRANSITIONING", "WARMING": "TRANSITIONING",
}


def _normalise_mode(word: str) -> str:
    """Map a user-typed mode word to its canonical form (or the upper-cased
    input if unrecognised, so the caller can reject it)."""
    return _MODE_ALIASES.get(word.strip().upper(), word.strip().upper())


def _mode_in_text(t: str):
    """Find a mode name mentioned anywhere in free text. Returns a canonical
    mode or None. Checks TRANSITIONING first so 'warming' beats 'warm'."""
    t = t.lower()
    if re.search(r"transition|\btrans\b|cooling|warming|过渡|降温|升温", t):
        return "TRANSITIONING"
    if re.search(r"cold|低温|冷", t):
        return "COLD"
    if re.search(r"idle|室温|\bwarm\b|暖", t):
        return "IDLE"
    return None

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
# Common alias: "MC" (mixing chamber) means MXC
SENSOR_LOOKUP["MC"]    = "MXC_TEMPERATURE"
SENSOR_LOOKUP["MCFAR"] = "MXC_TEMPERATURE_FAR"

UNITS = {
    "MXC_TEMPERATURE": "K",    "MXC_TEMPERATURE_FAR": "K",
    "STILL_TEMPERATURE": "K",  "4K_TEMPERATURE": "K",  "50K_TEMPERATURE": "K",
    "B1A_TEMPERATURE": "K",    "B2_TEMPERATURE": "K",
    "P1_PRESSURE": "bar",      "P2_PRESSURE": "bar",   "P5_PRESSURE": "bar",
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
        "threshold_overrides": {},          # legacy, migrated on load
        "threshold_overrides_cold": {},
        "threshold_overrides_idle": {},
        "last_slack_ts": "0",
        "last_freshness_alert": None,
        "current_mode": None,       # IDLE / TRANSITIONING / COLD
        "mode_override": None,      # if manually set via Slack
        "mode_since": None,
        "cs2_alerts_enabled": True, # can be toggled via 'sentinel on/off'
        "last_heater_event_id": 0,
    }


_STATE_LOCK_FILE = STATE_FILE.with_suffix(".lock")


def load_state() -> dict:
    base = _empty_state()
    try:
        with open(_STATE_LOCK_FILE, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_SH)   # shared: blocks only during a write
            if STATE_FILE.exists():
                text = STATE_FILE.read_text().strip()
                if text:
                    base.update(json.loads(text))
    except json.JSONDecodeError as e:
        log.error(f"State file corrupted ({e}), starting fresh")
    except Exception as e:
        log.error(f"load_state error ({e}), starting fresh")
    # Migrate legacy flat threshold_overrides into mode-specific dicts
    legacy = base.get("threshold_overrides", {})
    if legacy:
        mode = base.get("current_mode", "IDLE")
        target = "threshold_overrides_cold" if mode == "COLD" else "threshold_overrides_idle"
        base.setdefault(target, {}).update(legacy)
        base["threshold_overrides"] = {}
    return base


def save_state(state: dict):
    with open(_STATE_LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)   # exclusive: no other read or write during save
        tmp = STATE_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state, default=str, indent=2))
        tmp.replace(STATE_FILE)

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

def slack_upload_image(img_path: str, title: str, thread_ts: str = None) -> bool:
    """Upload an image file to Slack using the two-step upload API."""
    path = Path(img_path)
    size = path.stat().st_size
    try:
        # Step 1: get upload URL
        r1 = requests.post(
            "https://slack.com/api/files.getUploadURLExternal",
            headers=_headers(),
            data={"filename": path.name, "length": size},
            timeout=10)
        resp1 = r1.json()
        if not resp1.get("ok"):
            log.error(f"Slack upload URL error: {resp1.get('error')}")
            return False

        upload_url = resp1["upload_url"]
        file_id    = resp1["file_id"]

        # Step 2: upload the file bytes
        with open(img_path, "rb") as f:
            r2 = requests.post(upload_url, data=f.read(), timeout=30)
        if r2.status_code != 200:
            log.error(f"Slack upload failed: {r2.status_code}")
            return False

        # Step 3: complete the upload and share to channel
        payload = {
            "files": [{"id": file_id, "title": title}],
            "channel_id": config.SLACK_CHANNEL,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        r3 = requests.post(
            "https://slack.com/api/files.completeUploadExternal",
            headers=_headers(),
            json=payload,
            timeout=10)
        resp3 = r3.json()
        if not resp3.get("ok"):
            log.error(f"Slack complete upload error: {resp3.get('error')}")
            return False
        return True
    except Exception as e:
        log.error(f"Slack image upload failed: {e}")
        return False


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


def _mode_overrides(state: dict) -> dict:
    """Return the override dict for the current mode."""
    mode = state.get("current_mode", "IDLE")
    key  = "threshold_overrides_cold" if mode == "COLD" else "threshold_overrides_idle"
    return state.setdefault(key, {})


def get_threshold(name: str, state: dict):
    """Return (max_val, min_val) considering active mode-specific overrides."""
    overrides = _mode_overrides(state)
    if name in overrides:
        ov  = overrides[name]
        exp = ov.get("expires_at")
        if exp is None or datetime.fromisoformat(exp) > datetime.now():
            return ov.get("max_val"), ov.get("min_val")
        del overrides[name]
        log.info(f"Threshold override for {name} expired")
    thresholds = active_thresholds(state)
    entry = thresholds.get(name, (None, None, ""))
    return entry[0], entry[1]

# ── Duration parsing (shared: silence, threshold overrides, plots) ────────────

# A number followed by a unit: days / hours / minutes (English or Chinese).
_DURATION_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|"
    r"天|日|小时|钟头|时|分钟|分)",
    re.I,
)
# Words meaning "no expiry".
_FOREVER_RE = re.compile(
    r"(ever|forever|permanent(?:ly)?|indefinit\w*|always|"
    r"永久|永远|一直|长期)",
    re.I,
)
# Far-future sentinel stored for permanent silence, so every
# `fromisoformat(t) > now` check throughout the code keeps working.
FOREVER_TS = datetime.max.isoformat()

# Plot duration: a number + unit from minutes all the way up to years.
# Longer unit words are listed first so "month"/"mo" beats bare "m" (minute).
_PLOT_DUR_RE = re.compile(
    r"(?<![.\d])(\d+(?:\.\d+)?)\s*"
    r"(years?|yrs?|y|months?|mos?|mo|weeks?|wks?|w|days?|d|"
    r"hours?|hrs?|hr|h|minutes?|mins?|min|m)\b",
    re.IGNORECASE,
)


def _plot_unit_minutes(num: float, unit: str) -> float:
    """Convert (number, unit) to minutes. Supports minute→year."""
    u = unit.lower()
    if u.startswith("y"):
        return num * 525_600      # 365 days
    if u.startswith("mo"):
        return num * 43_200       # 30 days
    if u.startswith("w"):
        return num * 10_080       # 7 days
    if u.startswith("d"):
        return num * 1_440
    if u.startswith("h"):
        return num * 60
    return num                    # minutes


def _fmt_duration_minutes(m: float) -> str:
    """Human-friendly range label: minutes → hours → days."""
    if m < 60:
        return f"{m:g} min"
    if m < 1440:
        return f"{m/60:g} h"
    d = m / 1440
    return f"{d:g} day" + ("" if d == 1 else "s")


def parse_duration(text: str):
    """Parse a duration phrase.

    Returns:
      (minutes: float, label: str)  finite duration, e.g. (120.0, "2 hours")
      (None,          "permanently") for 'ever'/'forever'/'permanent'
      None                           if no duration is present
    """
    if _FOREVER_RE.search(text):
        return (None, "permanently")
    m = _DURATION_TOKEN_RE.search(text)
    if not m:
        return None
    val  = float(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("d") or unit in ("天", "日"):
        mins, word = val * 1440, "day"
    elif unit.startswith("h") or unit in ("小时", "钟头", "时"):
        mins, word = val * 60, "hour"
    else:
        mins, word = val, "minute"
    label = f"{val:g} {word}{'' if val == 1 else 's'}"
    return (mins, label)


# ── Slack polling: acks ───────────────────────────────────────────────────────

# Words in an alert thread that mean "stop notifying me about this sensor".
_SILENCE_RE = re.compile(
    r"\b(silent|silence|mute|quiet|snooze|hush|ignore|ack|acknowledge)\b|"
    r"静音|安静|消音|别响|闭嘴|忽略",
    re.I,
)


def _silence_request(text: str):
    """Given an alert-thread reply, decide whether it asks to silence the
    sensor and for how long. Returns (expires_at_iso, label) or None.

    Accepted forms:
      "ok"                        → 10 minutes (legacy default)
      "silent for 2h"             → 2 hours
      "mute 30 min" / "静音3天"    → parsed duration
      "silence forever"           → permanent
      a bare duration like "2h"   → parsed duration
    """
    low = _unescape_slack(text).strip().lower()
    if not low:
        return None

    parsed = parse_duration(low)
    has_kw = bool(_SILENCE_RE.search(low))

    if low in ("ok", "okay", "k"):
        expires = (datetime.now() + timedelta(minutes=10)).isoformat()
        return (expires, "10 minutes")

    # A silence keyword, or a reply that is essentially just a duration.
    core = re.sub(r"\b(for|please|pls|thanks?|thx|the|alert)\b", " ", low).strip(" .,!，。")
    core = re.sub(r"\s+", " ", core)
    pure_duration = bool(
        _FOREVER_RE.fullmatch(core)
        or re.fullmatch(r"\d+(?:\.\d+)?\s*\S+", core) and parsed is not None
    )
    if not (has_kw or pure_duration):
        return None

    if parsed is None:
        # keyword present but no duration given → default 10 minutes
        expires = (datetime.now() + timedelta(minutes=10)).isoformat()
        return (expires, "10 minutes")
    mins, label = parsed
    if mins is None:                 # "forever"
        return (FOREVER_TS, "permanently")
    return ((datetime.now() + timedelta(minutes=mins)).isoformat(), label)


def check_acknowledgements(state: dict):
    pending   = state.setdefault("pending_alert_msgs", {})
    acked     = state.setdefault("acked_sensors", {})

    for sensor in list(pending.keys()):
        ch = pending[sensor]["channel"]
        ts = pending[sensor]["ts"]
        silence = None   # (expires_at_iso, label)

        # 1. Emoji reaction → default 10-minute silence
        data = slack_get("reactions.get", {"channel": ch, "timestamp": ts})
        if data.get("ok"):
            rxns = data.get("message", {}).get("reactions", [])
            if any(r["name"] in ACK_REACTIONS for r in rxns):
                silence = ((datetime.now() + timedelta(minutes=10)).isoformat(), "10 minutes")

        # 2. Thread reply → "ok" / "silent for <duration>" / bare duration
        if silence is None:
            data = slack_get("conversations.replies", {"channel": ch, "ts": ts})
            if data.get("ok"):
                for reply in data.get("messages", [])[1:]:
                    if reply.get("user") == config.SLACK_BOT_USER_ID:
                        continue
                    req = _silence_request(reply.get("text", ""))
                    if req:
                        silence = req
                        break

        if silence:
            expires, label = silence
            acked[sensor] = expires
            del pending[sensor]
            when = "permanently" if label == "permanently" else f"for *{label}*"
            send_slack(f"🔕 Okay, silencing *{sensor}* {when}.",
                       color="good", thread_ts=ts)
            log.info(f"{sensor} silenced ({label})")


# ── Chilled water filter replacement reminder ─────────────────────────────────

# Phrases meaning "the filter has been replaced". NOTE: 'ok' / 'yes' do NOT count.
_FILTER_DONE_RE = re.compile(
    r"i\s*'?(?:ve|have)?\s*(?:just|already)?\s*(?:changed|replaced|swapped|renewed|done)"
    r"|(?:changed|replaced|swapped|renewed)\s*(?:it|the)?\s*(?:filter)?"
    r"|filter\s*(?:is\s*)?(?:changed|replaced|swapped|renewed|done|new)"
    r"|已经?(?:更换|换了|替换|换好)|换好了|换了新的?|更换完成|已换|换过了",
    re.IGNORECASE,
)
# For the @mention command path we additionally require the word "filter"
# so "I changed the threshold" can't reset the timer by accident.
_FILTER_WORD_RE = re.compile(r"filter|滤芯|滤网|滤器|过滤", re.IGNORECASE)


def _filter_state(state: dict) -> dict:
    f = state.get("filter")
    if not f:
        f = {
            "next_due": (datetime.now()
                         + timedelta(days=config.FILTER_INTERVAL_DAYS)).isoformat(),
            "active": False,
            "last_reminded": None,
            "reminder_ts": None,
        }
        state["filter"] = f
    return f


def reset_filter_timer(state: dict, reply_ts: str = None, announce: bool = True) -> None:
    """Mark the filter as freshly replaced and restart the countdown."""
    f = _filter_state(state)
    f["next_due"]      = (datetime.now()
                          + timedelta(days=config.FILTER_INTERVAL_DAYS)).isoformat()
    f["active"]        = False
    f["last_reminded"] = None
    f["reminder_ts"]   = None
    if announce:
        send_slack(
            ":white_check_mark: *Chilled water filter marked as replaced — thanks!*\n"
            f"Next replacement reminder in *{config.FILTER_INTERVAL_DAYS} days* "
            f"(around *{f['next_due'][:10]}*).",
            color="good", thread_ts=reply_ts)
    log.info(f"Filter timer reset — next due {f['next_due']}")


def check_filter_reminder(state: dict) -> None:
    """Every FILTER_INTERVAL_DAYS the chilled water filter must be replaced.
    Once due, remind every FILTER_REMINDER_INTERVAL_MIN minutes (in one thread)
    until someone replies that it was changed. Runs independently of alert
    pausing/acking — only an explicit 'I have changed it' stops it."""
    f   = _filter_state(state)
    now = datetime.now()

    if not f["active"]:
        if now >= datetime.fromisoformat(f["next_due"]):
            f["active"]        = True
            f["last_reminded"] = None
            f["reminder_ts"]   = None
        else:
            return

    # Active — first, look for a confirmation reply in the reminder thread.
    if f.get("reminder_ts"):
        data = slack_get("conversations.replies",
                         {"channel": config.SLACK_CHANNEL, "ts": f["reminder_ts"]})
        if data.get("ok"):
            for reply in data.get("messages", [])[1:]:
                if reply.get("user") == config.SLACK_BOT_USER_ID:
                    continue
                if _FILTER_DONE_RE.search(_unescape_slack(reply.get("text", "")).lower()):
                    reset_filter_timer(state, reply_ts=f["reminder_ts"])
                    return

    # Still due — send a reminder every FILTER_REMINDER_INTERVAL_MIN minutes.
    last = f.get("last_reminded")
    if last and (now - datetime.fromisoformat(last)) < timedelta(
            minutes=config.FILTER_REMINDER_INTERVAL_MIN):
        return

    msg = (":droplet: *Chilled water filter — replacement due*\n"
           f"It has been {config.FILTER_INTERVAL_DAYS} days since the last change. "
           "*Please replace the chilled water filter.*\n"
           "_Please reply `I have changed it` (or `filter replaced`) in this thread "
           "*after you have changed it*. I'll keep reminding every "
           f"{config.FILTER_REMINDER_INTERVAL_MIN} minutes until then — "
           "replying `ok` will *not* stop it._")
    ts = send_slack(msg, color="warning", thread_ts=f.get("reminder_ts"))
    f["last_reminded"] = now.isoformat()
    if not f.get("reminder_ts") and ts:
        f["reminder_ts"] = ts   # anchor all follow-up reminders in this one thread


# ── NLP self-learning: check thread replies for corrections ───────────────────

_CONFIRM_RE = re.compile(
    r"\b(yes|yep|yeah|yup|correct|right|sure|ok|okay|confirmed|"
    r"you'?re right|you are right|that'?s right|that is right|"
    r"对|是|是的|对的|没错|正确)\b"
)
_DENY_RE = re.compile(
    r"\b(wrong|no|nope|not right|incorrect|不对|错|错了|不是)\b"
)

def check_nlp_feedback(state: dict, conn=None):
    """Poll thread replies to recent NLP responses; learn from corrections."""
    pending = state.get("nlp_pending", [])
    if not pending:
        return

    try:
        from nlp_intent import classify_command, add_example, INTENT_LABELS
    except ImportError:
        return

    now = datetime.now().timestamp()
    still_pending = []

    def _try_correct(correction_text, thread_ts, entry):
        """Classify correction_text and propose the inferred intent for user confirmation.
        Does NOT save or execute — waits for the user to reply 'yes'.
        Returns True if an intent was understood."""
        correct_intent, _, c2 = classify_command(correction_text)
        if c2 > 0.25:
            label = INTENT_LABELS.get(correct_intent, correct_intent)
            send_slack(
                f"Did you mean *{label}*?\n"
                f"Reply `yes` to confirm or `wrong` to try again.",
                thread_ts=thread_ts)
            entry["orig_intent"]      = entry.get("orig_intent") or entry["intent"]
            entry["intent"]           = correct_intent
            entry["correction_text"]  = correction_text
            entry["awaiting_confirm"] = True
            entry["retry_count"]      = 0  # reset on each successful proposal
            log.info(f"NLP proposed: '{entry['input_text']}' → {correct_intent} (conf={c2:.2f})")
            return True
        return False

    for entry in pending:
        # Expire after 10 minutes
        if now - entry.get("created", 0) > 600:
            continue

        data = slack_get("conversations.replies",
                         {"channel": config.SLACK_CHANNEL, "ts": entry["user_msg_ts"]})
        if not data.get("ok"):
            still_pending.append(entry)
            continue

        msgs = data.get("messages", [])
        bot_ts = entry.get("bot_msg_ts") or entry["user_msg_ts"]
        # Only look at user replies AFTER the last message we processed
        # (use last_seen_ts so we don't re-process messages from previous polls)
        since_ts = entry.get("last_seen_ts") or bot_ts
        user_replies = [
            m for m in msgs[1:]
            if m.get("ts", "0") > since_ts
            and m.get("user") != config.SLACK_BOT_USER_ID
        ]
        if not user_replies:
            still_pending.append(entry)
            continue

        # Process the earliest unprocessed reply, not the latest
        next_msg  = user_replies[0]
        raw       = next_msg.get("text", "").strip()
        reply     = re.sub(r"<@[A-Z0-9]+>", "", raw).strip().lower()
        entry["last_seen_ts"] = next_msg["ts"]

        # ── "yes" / confirm ───────────────────────────────────────────────────
        if _CONFIRM_RE.search(reply) and not _DENY_RE.search(reply):
            src  = "corrected" if entry.get("orig_intent") else "confirmed"
            add_example(entry["input_text"], entry["intent"],
                        source=src, from_intent=entry.get("orig_intent"))
            label = INTENT_LABELS.get(entry["intent"], entry["intent"])
            send_slack(f"✅ Got it — running *{label}* now and I'll remember this next time.",
                       color="good", thread_ts=entry["user_msg_ts"])
            if conn:
                run_text = entry.get("correction_text") or entry["input_text"]
                _execute_command(run_text, entry["user_msg_ts"], state, conn)

        # ── "wrong" / deny ────────────────────────────────────────────────────
        elif _DENY_RE.search(reply):
            # Reset awaiting_confirm so the user can give a new correction
            entry["awaiting_confirm"] = False
            entry["correction_text"]  = None
            stripped = re.sub(
                r"^(?:wrong|no|nope|not right|incorrect|不对|错了)[,\s]*(?:i meant?|应该是|是)?\s*",
                "", reply).strip()
            if stripped:
                understood = _try_correct(stripped, entry["user_msg_ts"], entry)
                if not understood:
                    if not entry.get("asked_correction"):
                        send_slack(
                            "Got it, that was wrong. What did you mean?\n"
                            "Reply in your own words — I'll try to understand.",
                            thread_ts=entry["user_msg_ts"])
                    entry["asked_correction"] = True
            else:
                # "wrong" with no inline correction — ask what they meant
                if not entry.get("asked_correction"):
                    send_slack(
                        "Got it, that was wrong. What did you mean?\n"
                        "Reply in your own words — I'll try to understand.",
                        thread_ts=entry["user_msg_ts"])
                entry["asked_correction"] = True
            still_pending.append(entry)

        # ── user replied to "what did you mean?" ─────────────────────────────
        elif entry.get("asked_correction") and not entry.get("awaiting_confirm"):
            understood = _try_correct(reply, entry["user_msg_ts"], entry)
            if not understood:
                entry["retry_count"] = entry.get("retry_count", 0) + 1
                if entry["retry_count"] < 2:
                    send_slack(
                        f"Still not sure what you meant by *\"{entry['input_text']}\"*.\n"
                        "Try the exact command, e.g.:\n"
                        "`pump status` · `temperature reading` · `plot MXC` · `pause alerts`",
                        thread_ts=entry["user_msg_ts"])
                    still_pending.append(entry)
            else:
                still_pending.append(entry)  # understood → keep pending for yes/no

    state["nlp_pending"] = still_pending


# ── Slack polling: commands ───────────────────────────────────────────────────

def check_commands(state: dict, conn=None):
    check_nlp_feedback(state, conn)
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
            _execute_command(clean, ts, state, conn, user_id=msg.get("user", "default"))

    state["last_slack_ts"] = new_ts


def _unescape_slack(text: str) -> str:
    """Strip Slack auto-formatting: <tel:VALUE|DISPLAY> → VALUE, <URL|label> → label."""
    # <tel:2026-0622-0000|2026-0622-0000> → 2026-0622-0000
    text = re.sub(r"<tel:([^|>]+)\|[^>]*>", r"\1", text)
    # <http://...|label> → label
    text = re.sub(r"<https?://[^|>]+\|([^>]+)>", r"\1", text)
    # bare <URL> → URL
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    # commas acting as separators → spaces
    text = re.sub(r"\s*,\s*", " ", text)
    return text.strip()


_DENY_STANDALONE = re.compile(
    r"^(?:wrong|incorrect|not right|that(?:'s| was| is) wrong|不对|错了|不是这个意思)$"
)

def _execute_command(text: str, reply_ts: str, state: dict, conn=None, user_id: str = "default"):
    lower = _unescape_slack(text).lower().strip()

    # ── "wrong" on its own → correct the most recent NLP command ─────────────
    if _DENY_STANDALONE.match(lower):
        last = state.get("last_nlp_command")
        if last and (datetime.now().timestamp() - last.get("ts", 0)) < 600:
            send_slack(
                f"Got it, *\"{last['input_text']}\"* was wrong. What did you mean?\n"
                "Reply in your own words — I'll try to understand.",
                thread_ts=reply_ts)
            entry = {
                "input_text":       last["input_text"],
                "intent":           last["intent"],
                "conf":             last["conf"],
                "user_msg_ts":      reply_ts,
                "bot_msg_ts":       reply_ts,
                "created":          datetime.now().timestamp(),
                "asked_correction": True,
                "last_seen_ts":     reply_ts,
            }
            state.setdefault("nlp_pending", []).append(entry)
            state["nlp_pending"] = state["nlp_pending"][-20:]
        else:
            send_slack("No recent command to correct — what would you like to do?",
                       thread_ts=reply_ts)
        return

    # ── Context memory (10-minute window) ────────────────────────────────────
    ctx = state.get("ctx", {})
    ctx_age = datetime.now().timestamp() - ctx.get("ts", 0)
    ctx_valid = ctx_age < 600

    def _update_ctx(**kwargs):
        state["ctx"] = {**kwargs, "ts": datetime.now().timestamp()}

    # Context pronoun shortcuts:  "那画个图" / "plot it" / "longer"
    _PRONOUNS = re.compile(r"\b(那|它|it\b|same|that|前面|刚才|之前)\b")
    if _PRONOUNS.search(lower) and ctx_valid and ctx.get("sensor_key"):
        # Replace pronoun with remembered sensor key so later parsing picks it up
        lower = _PRONOUNS.sub(ctx["sensor_key"].lower(), lower, count=1)

    if any(w in lower for w in ("longer", "extend", "更长", "时间长", "久一点")) and ctx_valid and ctx.get("sensor_key"):
        new_min = ctx.get("minutes", 30) * 4
        _cmd_plot(ctx["sensor_key"], reply_ts, conn, minutes=new_min)
        _update_ctx(sensor_key=ctx["sensor_key"], intent="plot", minutes=new_min)
        return

    if lower in ("", "help"):
        _cmd_help(reply_ts)
        return

    if re.fullmatch(r"pressure\s+reading", lower):
        _cmd_pressure(reply_ts, conn)
        return

    if re.fullmatch(r"temperature\s+reading", lower):
        _cmd_temperature(reply_ts, conn)
        return

    if re.fullmatch(r"pump\s+status", lower):
        _cmd_pump_status(reply_ts, conn)
        return

    if re.fullmatch(r"heater\s+status", lower):
        _cmd_heater_status(reply_ts, conn)
        return

    if re.fullmatch(r"valve\s+status", lower):
        _cmd_valve_status(reply_ts, conn)
        return

    # Pulse tube compressor readings — "pulse tube status", "pulsetube ...", "pt status"
    if not lower.startswith("plot") and (
            re.search(r"pulse\s*tube|pulsetube", lower) or re.fullmatch(r"pt\s+status", lower)):
        _cmd_pulsetube_status(reply_ts, conn)
        return

    # ── Plot parser: handles 1 or multiple sensors + time range or duration ──
    if lower.startswith("plot"):
        _TIME_PAT_RE = re.compile(r"^(\d{6}_\d{4})$")
        tokens = lower.split()[1:]   # everything after "plot"
        sensor_keys, time_tokens, minutes = [], [], 30
        log.info(f"Plot parser: raw_text={repr(text)!r} lower={repr(lower)!r} tokens={tokens}")

        def _plot_key(tok: str):
            """Map a token to a PLOT_SENSORS key, honouring aliases (mc→MXC)."""
            k = PLOT_SENSOR_ALIASES.get(tok.upper(), tok.upper())
            return k if k in PLOT_SENSORS else None

        for tok in tokens:
            if _TIME_PAT_RE.fullmatch(tok):
                time_tokens.append(tok)
            elif _plot_key(tok):
                sensor_keys.append(_plot_key(tok))
        # Duration: search across full token string to handle "recent 2 hours",
        # "2h", "past 30 days", "3 weeks", "6 months", "1 year", etc.
        remaining = " ".join(t for t in tokens
                             if not _plot_key(t) and not _TIME_PAT_RE.fullmatch(t))
        dur_m = _PLOT_DUR_RE.search(remaining)
        if dur_m:
            minutes = _plot_unit_minutes(float(dur_m.group(1)), dur_m.group(2))

        if not sensor_keys and ctx_valid and ctx.get("sensor_key"):
            sensor_keys = [ctx["sensor_key"]]   # "plot 12h" uses last sensor

        if not sensor_keys:
            send_slack("Which sensor? e.g. `plot P2 12h`, `plot P2 P5 2h`", thread_ts=reply_ts)
            return

        if len(time_tokens) >= 2:
            t0 = _parse_plot_time(time_tokens[0])
            t1 = _parse_plot_time(time_tokens[1])
            if t0 and t1 and t0 < t1:
                if len(sensor_keys) == 1:
                    _cmd_plot(sensor_keys[0], reply_ts, conn, start=t0, end=t1)
                else:
                    _cmd_plot_multi(sensor_keys, reply_ts, conn, start=t0, end=t1)
                _update_ctx(sensor_key=sensor_keys[-1], intent="plot", minutes=minutes)
            else:
                send_slack("Invalid time range. Format: `plot P1 260622_0000 260622_0130` (CDT)",
                           thread_ts=reply_ts)
        else:
            if len(sensor_keys) == 1:
                _cmd_plot(sensor_keys[0], reply_ts, conn, minutes=minutes)
            else:
                _cmd_plot_multi(sensor_keys, reply_ts, conn, minutes=minutes)
            _update_ctx(sensor_key=sensor_keys[-1], intent="plot", minutes=minutes)
        return

    m = re.fullmatch(r"pause\s+alerts?", lower)
    if m:
        state["alerts_paused"] = True
        send_slack(":no_bell: *All alerts paused.* Monitoring continues in the background.\n"
                   "Send `resume alerts` to re-enable.", color="warning", thread_ts=reply_ts)
        log.info("All alerts paused via Slack")
        return

    m = re.fullmatch(r"resume\s+alerts?", lower)
    if m:
        state["alerts_paused"] = False
        send_slack(":bell: *Alerts resumed.* All threshold and system alerts are active again.",
                   color="good", thread_ts=reply_ts)
        log.info("Alerts resumed via Slack")
        return

    m = re.fullmatch(r"sentinel\s+(on|off)", lower)
    if m:
        enabled = m.group(1) == "on"
        state["cs2_alerts_enabled"] = enabled
        if enabled:
            send_slack(":large_green_circle: *Sentinel alerts ON* — CS2 alerts will be forwarded to Slack.",
                       color="good", thread_ts=reply_ts)
        else:
            send_slack(":white_circle: *Sentinel alerts OFF* — CS2 alerts paused until `sentinel on`.",
                       color="warning", thread_ts=reply_ts)
        log.info(f"CS2 sentinel alerts {'enabled' if enabled else 'disabled'} via Slack")
        return

    if lower == "list":
        _cmd_list(state, reply_ts)
        return

    if lower in ("status", "mode"):
        _cmd_status(state, reply_ts)
        return

    # What alarms are configured, and what triggers them? (optionally scoped)
    #   "what is the alarm", "list alarms", "alarm in cold mode",
    #   "alarm criteria", "what is current alarm criteria", "当前报警条件", ...
    # No \b around the alarm word — it fails when a Chinese char is adjacent.
    _alarm_word = re.search(r"alarms?|alerts?|报警|警报", lower)
    _query_word = re.search(r"\b(what|which|list|show|display|tell)\b"
                            r"|有什么|哪些|什么|列出|有哪些|告诉", lower)
    _crit_word  = re.search(r"criteri|trigger|condition|条件|标准|触发", lower)
    _cur_word   = re.search(r"\bcurrent(?:ly)?\b|\bnow\b|当前|现在|目前", lower)
    _alarm_mode = _mode_in_text(lower)
    _bare       = lower.strip() in ("alarms", "alarm", "alarm list", "alarm listing",
                                    "alarm criteria", "alarm criterion")
    if _alarm_word and (_query_word or _alarm_mode or _bare or _crit_word or _cur_word):
        only = _alarm_mode
        if only is None and _cur_word:      # "current" → the current operating mode
            only = state.get("current_mode")
        _cmd_list_alarms(state, reply_ts, only_mode=only)
        return

    # ── Chilled water filter ──────────────────────────────────────────────────
    if _FILTER_WORD_RE.search(lower):
        # "I have changed the filter" / "filter replaced" → reset the countdown
        if _FILTER_DONE_RE.search(lower):
            reset_filter_timer(state, reply_ts=reply_ts)
            return
        # "filter status" / "filter" → how long until the next replacement
        if re.fullmatch(r"(chilled\s+water\s+)?filter(\s+status)?|滤芯状态|过滤器状态", lower):
            f   = _filter_state(state)
            due = datetime.fromisoformat(f["next_due"])
            days_left = (due - datetime.now()).total_seconds() / 86400
            if f.get("active"):
                body = (":droplet: *Chilled water filter is DUE for replacement.*\n"
                        "Reply `I have changed it` in the reminder thread once done.")
            else:
                body = (f":droplet: *Chilled water filter* — next replacement in "
                        f"*{days_left:.1f} days* (around *{f['next_due'][:10]}*).")
            send_slack(body, color="#0066cc", thread_ts=reply_ts)
            return

    # set mode auto / idle / cold / transitioning
    m = re.fullmatch(r"set\s+mode\s+(\S+)", text, re.IGNORECASE)
    if m:
        val = _normalise_mode(m.group(1))
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
                f"Unknown mode `{m.group(1)}`. Valid options: `auto`, `idle`, `cold`, `transitioning`.",
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

    def _parse_mode_prefix(txt: str):
        """Extract optional 'cold'/'idle' prefix. Returns (mode_key, rest_of_text)."""
        t = txt.strip()
        for prefix, key in [("cold ", "cold"), ("idle ", "idle")]:
            if t.lower().startswith(prefix):
                return key, t[len(prefix):].strip()
        return None, t  # use current mode

    def _overrides_for(mode_key: str) -> dict:
        if mode_key == "cold":
            return state.setdefault("threshold_overrides_cold", {})
        elif mode_key == "idle":
            return state.setdefault("threshold_overrides_idle", {})
        else:
            return _mode_overrides(state)  # current mode

    def _thresholds_for(mode_key: str) -> dict:
        if mode_key == "cold":  return config.THRESHOLDS_COLD
        if mode_key == "idle":  return config.THRESHOLDS_IDLE
        return active_thresholds(state)

    m = re.fullmatch(r"(cold\s+|idle\s+)?reset\s+(\S+)", text, re.IGNORECASE)
    if m:
        mode_key = m.group(1).strip().lower() if m.group(1) else None
        sensor   = resolve_sensor(m.group(2))
        if not sensor:
            send_slack(f"Unknown sensor `{m.group(2)}`. Use `list` to see all sensors.",
                       thread_ts=reply_ts)
            return
        _overrides_for(mode_key).pop(sensor, None)
        mode_label = f"[{(mode_key or state.get('current_mode','current')).upper()}] "
        t_dict = _thresholds_for(mode_key)
        all_t  = {**config.THRESHOLDS_COLD, **config.THRESHOLDS_IDLE}
        entry  = t_dict.get(sensor) or all_t.get(sensor, (None, None, ""))
        default_val = entry[0] if entry[0] is not None else entry[1]
        send_slack(f"✅ {mode_label}*{sensor}* reset to default: `{default_val} {UNITS.get(sensor, '')}`",
                   color="good", thread_ts=reply_ts)
        log.info(f"{mode_label}{sensor} threshold reset to default")
        return

    m = re.fullmatch(r"(cold\s+|idle\s+)?change\s+(\S+)\s+to\s+([\d.e+\-]+)\s+for\s+(.+)",
                     text, re.IGNORECASE)
    if m:
        mode_key = m.group(1).strip().lower() if m.group(1) else None
        sensor   = resolve_sensor(m.group(2))
        if not sensor:
            send_slack(f"Unknown sensor `{m.group(2)}`. Use `list` to see all sensors.",
                       thread_ts=reply_ts)
            return
        try:
            new_val = float(m.group(3))
        except ValueError:
            send_slack(f"Invalid value `{m.group(3)}`.", thread_ts=reply_ts)
            return

        raw_dur = m.group(4).strip().lower()
        if raw_dur in ("ever", "forever", "permanent", "permanently"):
            expires_at = None
            dur_text   = "*permanently*"
        else:
            h_m   = re.fullmatch(r"(\d+)\s*h(?:ours?)?", raw_dur)
            min_m = re.fullmatch(r"(\d+)\s*min(?:utes?)?", raw_dur)
            if h_m:
                mins = int(h_m.group(1)) * 60
                dur_text = f"for *{h_m.group(1)} hours*"
            elif min_m:
                mins = int(min_m.group(1))
                dur_text = f"for *{mins} minutes*"
            else:
                send_slack(
                    f"Unknown duration `{m.group(4)}`.\n"
                    "Use `for 30min`, `for 2h`, `for 24h`, or `for ever`.",
                    thread_ts=reply_ts)
                return
            expires_at = (datetime.now() + timedelta(minutes=mins)).isoformat()

        all_t = {**config.THRESHOLDS_COLD, **config.THRESHOLDS_IDLE}
        entry = all_t.get(sensor, (None, None, ""))
        if entry[0] is not None:
            ov = {"max_val": new_val, "min_val": entry[1], "expires_at": expires_at}
        else:
            ov = {"max_val": entry[0], "min_val": new_val, "expires_at": expires_at}
        _overrides_for(mode_key)[sensor] = ov

        mode_label = f"[{(mode_key or state.get('current_mode','current')).upper()}] "
        unit = UNITS.get(sensor, "")
        send_slack(f"✅ {mode_label}*{sensor}* threshold → `{new_val} {unit}` {dur_text}.",
                   color="good", thread_ts=reply_ts)
        log.info(f"{mode_label}{sensor} threshold → {new_val} {dur_text}")
        return

    # ── NLP fallback: natural language understanding ──────────────────────────
    try:
        from nlp_intent import classify_command, INTENT_LABELS, add_example
        intent, ent, conf = classify_command(text)
        log.info(f"NLP: intent={intent} conf={conf:.2f} entities={ent}")

        if conf < 0.12:
            send_slack(
                f"I didn't understand: `{text}`\n"
                "Try natural language or type `help` to see all commands.",
                thread_ts=reply_ts)
            return

        # Track this NLP interaction for self-learning feedback
        _nlp_track = {
            "input_text":   text,
            "intent":       intent,
            "conf":         conf,
            "user_msg_ts":  reply_ts,
            "bot_msg_ts":   None,   # filled in after send_slack returns ts
            "created":      datetime.now().timestamp(),
            "asked_correction": False,
        }

        if intent == "plot":
            # Resolve all extracted sensors → PLOT_SENSORS keys
            mapping_to_key = {v[0]: k for k, v in PLOT_SENSORS.items()}
            sensor_keys_nlp = [mapping_to_key[m] for m in ent.get("sensors", [])
                                if m in mapping_to_key]
            # Fall back to context if nothing extracted
            if not sensor_keys_nlp and ctx_valid and ctx.get("sensor_key"):
                sensor_keys_nlp = [ctx["sensor_key"]]
            if not sensor_keys_nlp:
                send_slack("Which sensor would you like to plot? (e.g. P2, MXC, FLOW)",
                           thread_ts=reply_ts)
                return
            plot_min = ent.get("minutes") or (ctx.get("minutes", 30) if ctx_valid else 30)
            if len(sensor_keys_nlp) == 1:
                _cmd_plot(sensor_keys_nlp[0], reply_ts, conn, minutes=plot_min)
            else:
                _cmd_plot_multi(sensor_keys_nlp, reply_ts, conn, minutes=plot_min)
            _update_ctx(sensor_key=sensor_keys_nlp[-1], intent="plot", minutes=plot_min)

        elif intent == "temperature_reading":
            _cmd_temperature(reply_ts, conn)

        elif intent == "pause_alerts":
            state["alerts_paused"] = True
            send_slack(":no_bell: *All alerts paused.* Monitoring continues in the background.\n"
                       "Send `resume alerts` to re-enable.", color="warning", thread_ts=reply_ts)
            log.info("All alerts paused via Slack (NLP)")

        elif intent == "resume_alerts":
            state["alerts_paused"] = False
            send_slack(":bell: *Alerts resumed.* All threshold and system alerts are active again.",
                       color="good", thread_ts=reply_ts)
            log.info("Alerts resumed via Slack (NLP)")

        elif intent == "pressure_reading":
            _cmd_pressure(reply_ts, conn)

        elif intent == "pump_status":
            _cmd_pump_status(reply_ts, conn, highlight=ent.get("device"))

        elif intent == "heater_status":
            _cmd_heater_status(reply_ts, conn)

        elif intent == "valve_status":
            _cmd_valve_status(reply_ts, conn, highlight=ent.get("valve"))

        elif intent == "status":
            # If a specific device is mentioned, route to the right command
            from nlp_intent import _extract_pump_device, _extract_valve_name
            pump = _extract_pump_device(text)
            valve = _extract_valve_name(text)
            if pump:
                _cmd_pump_status(reply_ts, conn, highlight=pump)
            elif valve:
                _cmd_valve_status(reply_ts, conn, highlight=valve)
            else:
                _cmd_status(state, reply_ts)

        elif intent == "daily_summary":
            from monitor import generate_summary
            msg = generate_summary(conn)
            send_slack(msg, color="good", thread_ts=reply_ts)

        elif intent == "help":
            _cmd_help(reply_ts)

        elif intent == "ack":
            until = (datetime.now() + timedelta(minutes=10)).isoformat()
            for s in {**config.THRESHOLDS_COLD, **config.THRESHOLDS_IDLE}:
                state.setdefault("acked_sensors", {})[s] = until
            state.setdefault("pending_alert_msgs", {}).clear()
            send_slack("✅ All active alerts acknowledged — silenced for 10 minutes.",
                       color="good", thread_ts=reply_ts)

        elif intent == "sentinel":
            on_off = ent.get("on_off")
            if on_off == "on":
                state["cs2_alerts_enabled"] = True
                send_slack(":large_green_circle: *Sentinel ON* — CS2 alerts resumed.",
                           color="good", thread_ts=reply_ts)
            elif on_off == "off":
                state["cs2_alerts_enabled"] = False
                send_slack(":white_circle: *Sentinel OFF* — CS2 alerts paused.",
                           thread_ts=reply_ts)
            else:
                send_slack("Say `sentinel on` or `sentinel off`.", thread_ts=reply_ts)

        elif intent == "set_mode":
            val = _normalise_mode(ent.get("mode") or "")
            if val == "AUTO":
                state["mode_override"] = None
                send_slack("✅ Mode set to *auto* — detecting from 50K temperature.",
                           color="good", thread_ts=reply_ts)
            elif val in MODES:
                state["last_alert_time"] = {}
                state["acked_sensors"]   = {}
                state["mode_override"]   = val
                state["current_mode"]    = val
                state["mode_since"]      = datetime.now().isoformat()
                send_slack(f"✅ Mode manually set to *{val}*. {MODE_EMOJI[val]}",
                           color="good", thread_ts=reply_ts)
            else:
                send_slack("Which mode? Say `cold`, `idle`, `transitioning`, or `auto`.",
                           thread_ts=reply_ts)

        elif intent == "change_threshold":
            sensor  = ent.get("sensor")
            value   = ent.get("value")
            mk      = ent.get("mode_prefix")
            minutes = ent.get("minutes")
            if not sensor:
                send_slack("Which sensor? (e.g. MXC, P2, STILL)", thread_ts=reply_ts)
                return
            if value is None:
                send_slack(f"What value should the threshold be for *{sensor}*?",
                           thread_ts=reply_ts)
                return
            all_t = {**config.THRESHOLDS_COLD, **config.THRESHOLDS_IDLE}
            entry = all_t.get(sensor, (None, None, ""))
            if entry[0] is not None:
                ov = {"max_val": value, "min_val": entry[1],
                      "expires_at": None if minutes is None else
                      (datetime.now() + timedelta(minutes=minutes)).isoformat()}
            else:
                ov = {"max_val": entry[0], "min_val": value,
                      "expires_at": None if minutes is None else
                      (datetime.now() + timedelta(minutes=minutes)).isoformat()}
            key = ("threshold_overrides_cold" if mk == "cold"
                   else "threshold_overrides_idle" if mk == "idle"
                   else ("threshold_overrides_cold"
                         if state.get("current_mode") == "COLD"
                         else "threshold_overrides_idle"))
            state.setdefault(key, {})[sensor] = ov
            mode_label = f"[{(mk or state.get('current_mode','current')).upper()}] "
            dur_text = "permanently" if minutes is None else f"for {minutes} min"
            send_slack(f"✅ {mode_label}*{sensor}* → `{value}` {dur_text}",
                       color="good", thread_ts=reply_ts)

        elif intent == "reset_threshold":
            sensor = ent.get("sensor")
            mk     = ent.get("mode_prefix")
            if not sensor:
                send_slack("Which sensor to reset?", thread_ts=reply_ts)
                return
            key = ("threshold_overrides_cold" if mk == "cold"
                   else "threshold_overrides_idle" if mk == "idle"
                   else ("threshold_overrides_cold"
                         if state.get("current_mode") == "COLD"
                         else "threshold_overrides_idle"))
            state.get(key, {}).pop(sensor, None)
            send_slack(f"✅ *{sensor}* reset to default.", color="good", thread_ts=reply_ts)

        else:
            send_slack(
                f"I didn't understand: `{text}`\n"
                "Try natural language or type `help` to see all commands.",
                thread_ts=reply_ts)
            return

        # ── Always record last NLP command so "wrong" can reference it ──────
        state["last_nlp_command"] = {
            "input_text": text,
            "intent":     intent,
            "conf":       conf,
            "ts":         datetime.now().timestamp(),
        }

        # ── For uncertain predictions, send hint and track for feedback ───────
        if conf < 0.45:
            label = INTENT_LABELS.get(intent, intent)
            hint_ts = send_slack(
                f"_(Auto-detected as: *{label}*. "
                f"Reply \"yes\" to confirm or \"wrong\" if incorrect — I'll learn from it.)_",
                color="#888888", thread_ts=reply_ts)
            _nlp_track["bot_msg_ts"] = hint_ts or reply_ts
            state.setdefault("nlp_pending", []).append(_nlp_track)
            state["nlp_pending"] = state["nlp_pending"][-20:]

    except Exception as e:
        log.error(f"NLP dispatch error: {e}")
        send_slack(
            f"I didn't understand: `{text}`\n"
            "Type `help` to see all commands.",
            thread_ts=reply_ts)


PLOT_SENSORS = {
    "P1": ("P1_PRESSURE", "P1 Pressure", "bar"),
    "P2": ("P2_PRESSURE", "P2 Pressure", "bar"),
    "P3": ("P3_PRESSURE", "P3 Pressure", "bar"),
    "P4": ("P4_PRESSURE", "P4 Pressure", "bar"),
    "P5": ("P5_PRESSURE", "P5 Pressure", "bar"),
    "P6": ("P6_PRESSURE", "P6 Pressure", "bar"),
    "P7": ("P7_PRESSURE", "P7 Pressure", "bar"),
    "MXC":     ("MXC_TEMPERATURE",     "MXC Temperature",      "K"),
    "STILL":   ("STILL_TEMPERATURE",   "Still Temperature",    "K"),
    "4K":      ("4K_TEMPERATURE",      "4K Plate Temperature", "K"),
    "50K":     ("50K_TEMPERATURE",     "50K Plate Temperature","K"),
    "B1A":     ("B1A_TEMPERATURE",     "B1A Temperature",      "K"),
    "B2":      ("B2_TEMPERATURE",      "B2 Temperature",       "K"),
    "FLOW":    ("FLOW_VALUE",          "He Flow",              "mmol/s"),
}

# User-typed short aliases → canonical PLOT_SENSORS key
PLOT_SENSOR_ALIASES = {
    "MC":      "MXC",   # mixing chamber
    "MIXING":  "MXC",
}

PRESSURE_MAPPINGS_SET = {"P1_PRESSURE","P2_PRESSURE","P3_PRESSURE",
                          "P4_PRESSURE","P5_PRESSURE","P6_PRESSURE","P7_PRESSURE"}


_CDT = timezone(timedelta(hours=-5))

def _parse_plot_time(s: str) -> datetime | None:
    """Parse YYMMDD_HHMM (CDT) → UTC datetime. E.g. '260622_0130'."""
    m = re.fullmatch(r"(\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})", s)
    if not m:
        return None
    try:
        dt = datetime(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)),
                      int(m.group(4)), int(m.group(5)), tzinfo=_CDT)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _cmd_plot(sensor_key: str, reply_ts: str, conn=None,
              minutes: float = 30, start: datetime = None, end: datetime = None):
    if conn is None:
        send_slack("Cannot plot: no database connection.", thread_ts=reply_ts)
        return

    key = sensor_key.upper()
    if key not in PLOT_SENSORS:
        opts = ", ".join(PLOT_SENSORS.keys())
        send_slack(f"Unknown sensor `{sensor_key}`. Available: {opts}", thread_ts=reply_ts)
        return

    mapping, label, unit = PLOT_SENSORS[key]

    if start and end:
        t_from, t_to = start, end
        range_label = (f"{t_from.astimezone(_CDT).strftime('%Y-%m-%d %H:%M')}"
                       f" – {t_to.astimezone(_CDT).strftime('%Y-%m-%d %H:%M')} CDT")
    else:
        t_to   = datetime.now(timezone.utc)
        t_from = t_to - timedelta(minutes=minutes)
        range_label = f"last {_fmt_duration_minutes(minutes)}"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT time, value FROM public.double_value_change_events "
            "WHERE mapping = %s AND time >= %s AND time <= %s ORDER BY time",
            (mapping, t_from, t_to))
        rows = cur.fetchall()

    if not rows:
        send_slack(f"No data for *{label}* ({range_label}).", thread_ts=reply_ts)
        return

    times  = [r[0] for r in rows]
    values = [float(r[1]) for r in rows]

    is_pressure = mapping in PRESSURE_MAPPINGS_SET
    if is_pressure:
        values = [v * 1000 for v in values]
        unit   = "mbar"

    # Auto-convert to mK when all values are sub-kelvin (MXC, Still in cold operation)
    if unit == "K" and max(values) < 1.0:
        values = [v * 1000 for v in values]
        unit   = "mK"

    # Wider figure for multi-hour ranges
    duration_h = (t_to - t_from).total_seconds() / 3600
    fig_w = max(10, min(16, int(duration_h * 1.5)))
    fig, ax = plt.subplots(figsize=(fig_w, 4))
    ax.plot(times, values, linewidth=1.0, color="#1f77b4")
    ax.set_title(f"{label}  —  {range_label}  ({len(rows)} points)", fontsize=13)
    ax.set_xlabel("Time (CDT)")
    ax.set_ylabel(unit)

    # Tick format depends on range length
    if duration_h <= 2:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=_CDT))
    elif duration_h <= 48:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=_CDT))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d", tz=_CDT))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=30)
    ax.grid(True, linestyle="--", alpha=0.5)

    if is_pressure and max(values) < 0.1:
        ax.yaxis.set_major_formatter(plt.ScalarFormatter(useMathText=True))
        ax.ticklabel_format(axis="y", style="sci", scilimits=(0, 0))

    plt.tight_layout()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False,
                                    dir="/home/cdms/.claude/jobs/b7b666c4/tmp") as f:
        tmp_path = f.name
    fig.savefig(tmp_path, dpi=120, bbox_inches="tight")
    plt.close(fig)

    title = f"{label} — {range_label}"
    ok = slack_upload_image(tmp_path, title, thread_ts=reply_ts)
    Path(tmp_path).unlink(missing_ok=True)

    if ok:
        log.info(f"Sent plot for {mapping} ({len(rows)} points) [{range_label}]")
    else:
        send_slack(
            f"Could not upload plot — bot may need `files:write` scope.\n"
            f"Go to api.slack.com/apps → OAuth & Permissions → add `files:write` → Reinstall.",
            thread_ts=reply_ts)


def _cmd_plot_multi(sensor_keys: list, reply_ts: str, conn=None,
                    minutes: float = 30, start: datetime = None, end: datetime = None):
    """Plot 2+ sensors on one figure; dual y-axis when units differ."""
    if conn is None:
        send_slack("Cannot plot: no database connection.", thread_ts=reply_ts)
        return

    keys = [k.upper() for k in sensor_keys if k.upper() in PLOT_SENSORS]
    if not keys:
        send_slack(f"No valid sensors. Available: {', '.join(PLOT_SENSORS.keys())}", thread_ts=reply_ts)
        return

    if start and end:
        t_from, t_to = start, end
        range_label = (f"{t_from.astimezone(_CDT).strftime('%Y-%m-%d %H:%M')}"
                       f" – {t_to.astimezone(_CDT).strftime('%Y-%m-%d %H:%M')} CDT")
    else:
        t_to   = datetime.now(timezone.utc)
        t_from = t_to - timedelta(minutes=minutes)
        range_label = f"last {_fmt_duration_minutes(minutes)}"

    # Fetch and convert each sensor's data
    sensor_data = {}
    for key in keys:
        mapping, label, unit = PLOT_SENSORS[key]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT time, value FROM public.double_value_change_events "
                "WHERE mapping = %s AND time >= %s AND time <= %s ORDER BY time",
                (mapping, t_from, t_to))
            rows = cur.fetchall()
        if not rows:
            continue
        vals = [float(r[1]) for r in rows]
        disp_unit = unit
        if mapping in PRESSURE_MAPPINGS_SET:
            vals = [v * 1000 for v in vals]
            disp_unit = "mbar"
        elif mapping == "MXC_TEMPERATURE":
            vals = [v * 1000 for v in vals]
            disp_unit = "mK"
        sensor_data[key] = {"times": [r[0] for r in rows], "values": vals,
                             "label": label, "unit": disp_unit}

    if not sensor_data:
        send_slack(f"No data for {' + '.join(keys)} ({range_label}).", thread_ts=reply_ts)
        return

    # Group by display unit for axis assignment
    units_order = []
    for k in keys:
        if k in sensor_data:
            u = sensor_data[k]["unit"]
            if u not in units_order:
                units_order.append(u)

    colors = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]
    duration_h = (t_to - t_from).total_seconds() / 3600
    fig_w = max(10, min(16, int(duration_h * 1.5)))
    fig, ax1 = plt.subplots(figsize=(fig_w, 4))
    ax2 = ax1.twinx() if len(units_order) > 1 else None

    lines, col_i = [], 0
    for key in keys:
        if key not in sensor_data:
            continue
        d = sensor_data[key]
        ax = ax1 if d["unit"] == units_order[0] else ax2
        line, = ax.plot(d["times"], d["values"], linewidth=1.0,
                        color=colors[col_i % len(colors)], label=d["label"])
        lines.append(line)
        col_i += 1

    ax1.set_ylabel(units_order[0])
    if ax2 and len(units_order) > 1:
        ax2.set_ylabel(units_order[1])

    sensor_names = " + ".join(k for k in keys if k in sensor_data)
    total_pts = sum(len(d["times"]) for d in sensor_data.values())
    ax1.set_title(f"{sensor_names}  —  {range_label}  ({total_pts} pts)", fontsize=12)
    ax1.set_xlabel("Time (CDT)")
    ax1.legend(lines, [l.get_label() for l in lines], loc="upper left", fontsize=9)

    if duration_h <= 2:
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=_CDT))
    elif duration_h <= 48:
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=_CDT))
    else:
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d", tz=_CDT))
    ax1.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=30)
    ax1.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False,
                                     dir="/home/cdms/.claude/jobs/b7b666c4/tmp") as f:
        tmp_path = f.name
    fig.savefig(tmp_path, dpi=120, bbox_inches="tight")
    plt.close(fig)

    ok = slack_upload_image(tmp_path, f"{sensor_names} — {range_label}", thread_ts=reply_ts)
    Path(tmp_path).unlink(missing_ok=True)
    if ok:
        log.info(f"Sent multi-plot for {sensor_names} ({total_pts} pts) [{range_label}]")
    else:
        send_slack("Could not upload plot.", thread_ts=reply_ts)


def _cmd_pump_status(reply_ts: str, conn=None, highlight: str = None):
    """Show all pump statuses. If highlight is set (e.g. 'R2'), pin that device first."""
    if conn is None:
        send_slack("Cannot read pump status: no database connection.", thread_ts=reply_ts)
        return

    def latest_bool(mapping):
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM public.boolean_value_change_events "
                        "WHERE mapping = %s ORDER BY time DESC LIMIT 1", (mapping,))
            r = cur.fetchone()
            return bool(r[0]) if r else None

    def latest_double(mapping):
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM public.double_value_change_events "
                        "WHERE mapping = %s ORDER BY time DESC LIMIT 1", (mapping,))
            r = cur.fetchone()
            return float(r[0]) if r else None

    def latest_int(mapping):
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM public.int_value_change_events "
                        "WHERE mapping = %s ORDER BY time DESC LIMIT 1", (mapping,))
            r = cur.fetchone()
            return int(r[0]) if r else None

    def status_icon(enabled, error):
        if error:   return ":warning:"
        if enabled: return ":large_green_circle:"
        return ":white_circle:"

    hl = (highlight or "").upper().replace("TURBO", "B1A")
    title = f":gear: *Pump Status — {highlight}*\n" if hl else ":gear: *Pump Status*\n"
    lines = [title]

    def fmt_turbo(name):
        enabled = latest_bool(f"{name}_ENABLED")
        error   = latest_bool(f"{name}_ERROR_VALUE")
        temp    = latest_double(f"{name}_TEMPERATURE")
        power   = latest_int(f"{name}_POWER")
        speed   = latest_int(f"{name}_SPEED")
        icon    = status_icon(enabled, error)
        state   = "ON" if enabled else "OFF"
        err_str = "  :warning: ERROR" if error else ""
        details = []
        if speed  is not None: details.append(f"speed {speed}%")
        if power  is not None: details.append(f"power {power}%")
        if temp   is not None: details.append(f"temp {temp:.1f} K")
        detail_str = "  |  " + ",  ".join(details) if details else ""
        star = " ◄" if hl == name else ""
        return f"{icon} *{name}* (turbo): *{state}*{err_str}{detail_str}{star}"

    def fmt_scroll(name):
        enabled = latest_bool(f"{name}_ENABLED")
        error   = latest_bool(f"{name}_ERROR_VALUE")
        power   = latest_double(f"{name}_PUMP_POWER")
        icon    = status_icon(enabled, error)
        state   = "ON" if enabled else "OFF"
        err_str = "  :warning: ERROR" if error else ""
        pw_str  = f"  |  power {power:.2f} W" if power is not None else ""
        star = " ◄" if hl == name else ""
        return f"{icon} *{name}* (scroll): *{state}*{err_str}{pw_str}{star}"

    # If a specific device is highlighted, show it first
    all_devices = {
        "B1A": lambda: fmt_turbo("B1A"),
        "B2":  lambda: fmt_turbo("B2"),
        "R1A": lambda: fmt_scroll("R1A"),
        "R2":  lambda: fmt_scroll("R2"),
    }
    if hl and hl in all_devices:
        lines.append(all_devices[hl]())
        lines.append("_Other pumps:_")
        for name, fn in all_devices.items():
            if name != hl:
                lines.append(fn())
    else:
        for name in ("B1A", "B2"):
            lines.append(fmt_turbo(name))
        lines.append("")
        for name in ("R1A", "R2"):
            lines.append(fmt_scroll(name))

    lines.append("")
    # Compressor: COM
    enabled = latest_bool("COM_ENABLED")
    error   = latest_bool("COM_ERROR_VALUE")
    power   = latest_double("COM_PUMP_POWER")
    icon    = status_icon(enabled, error)
    state   = "ON" if enabled else "OFF"
    err_str = "  :warning: ERROR" if error else ""
    pw_str  = f"  |  power {power:.2f} W" if power is not None else ""
    star = " ◄" if hl == "COM" else ""
    lines.append(f"{icon} *COM* (compressor): *{state}*{err_str}{pw_str}{star}")

    send_slack("\n".join(lines), color="#0066cc", thread_ts=reply_ts)
    log.info(f"Sent pump status reply to Slack" + (f" [highlight={hl}]" if hl else ""))


def _fmt_pressure(val_bar: float) -> str:
    mbar = val_bar * 1000
    if mbar >= 0.1:
        return f"{mbar:.4g} mbar"
    else:
        ubar = mbar * 1000
        return f"{ubar:.3g} μbar"


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

    lines = [":compression: *Current Pressure Readings*\n"]
    with conn.cursor() as cur:
        for mapping, label in PRESSURE_MAPPINGS:
            cur.execute(
                "SELECT value, time FROM public.double_value_change_events "
                "WHERE mapping = %s ORDER BY time DESC LIMIT 1",
                (mapping,))
            row = cur.fetchone()
            if row:
                val_bar, ts = float(row[0]), row[1]
                lines.append(f"  *{label}*: `{_fmt_pressure(val_bar)}`  _(at {str(ts)[:19]})_")
            else:
                lines.append(f"  *{label}*: _no data_")

        # Cold Cathode gauge (P1 = Pfeiffer MPT200, has on/off switch)
        cur.execute("SELECT value FROM public.boolean_value_change_events "
                    "WHERE mapping = 'P1_ENABLED' ORDER BY time DESC LIMIT 1")
        cc_row = cur.fetchone()
        if cc_row is not None:
            cc_on  = bool(cc_row[0])
            cc_str = ":large_green_circle: ON" if cc_on else ":white_circle: OFF"
            lines.append(f"\n  *Cold Cathode (P1)*: {cc_str}")

    send_slack("\n".join(lines), color="#0066cc", thread_ts=reply_ts)
    log.info("Sent pressure reading reply to Slack")


def _cmd_temperature(reply_ts: str, conn=None):
    TEMP_SENSORS = [
        ("MXC_TEMPERATURE",     "MXC1",  True),
        ("MXC_TEMPERATURE_FAR", "MXC2",  True),
        ("STILL_TEMPERATURE",   "Still", True),
        ("4K_TEMPERATURE",      "4K",    False),
        ("50K_TEMPERATURE",     "50K",   False),
        ("B1A_TEMPERATURE",     "B1A",   False),
        ("B2_TEMPERATURE",      "B2",    False),
    ]
    if conn is None:
        send_slack("Cannot read temperatures: no database connection.", thread_ts=reply_ts)
        return

    lines = [":thermometer: *Current Temperature Readings*\n"]
    with conn.cursor() as cur:
        for mapping, label, prefer_mk in TEMP_SENSORS:
            cur.execute(
                "SELECT value, time FROM public.double_value_change_events "
                "WHERE mapping = %s ORDER BY time DESC LIMIT 1", (mapping,))
            row = cur.fetchone()
            if not row:
                lines.append(f"  *{label}*: _no data_")
                continue
            v, ts = float(row[0]), str(row[1])[:19]
            if prefer_mk and v < 1.0:
                lines.append(f"  *{label}*: `{v*1000:.2f} mK`  _(at {ts})_")
            else:
                lines.append(f"  *{label}*: `{v:.4g} K` (`{v-273.15:.1f} °C`)  _(at {ts})_")

    send_slack("\n".join(lines), color="#2196F3", thread_ts=reply_ts)
    log.info("Sent temperature reading reply to Slack")


def _cmd_help(reply_ts=None):
    send_slack(
        "*BlueFors Monitor — Commands*\n\n"
        "*Acknowledge an alert (silences sensor 10 min):*\n"
        "  React ✅  👏  👍  🤙 on the alert, or reply `ok` / `OK` in the thread\n\n"
        "*@mention commands* (`@BlueFors-Alert <command>`):\n"
        "`help` — show this message\n"
        "`temperature reading` — show current temperatures (MXC1, MXC2, Still, 4K, 50K, B1A, B2)\n"
        "`pressure reading` — show latest P1–P7 pressure values\n"
        "`pump status` — show on/off, power, speed for all 5 pumps (B1A, B2, R1A, R2, COM)\n"
        "`heater status` — show on/off and power for Still/MXC heat switches and heaters\n"
        "`pulse tube status` — compressor coolant/oil/He temps, high/low pressure, current + faults\n"
        "`plot <sensor>` — plot last 30 min of data as image (e.g. `plot P1`, `plot MXC`)\n"
        "`plot <sensor> <N>min` — plot last N minutes (e.g. `plot P5 60min`)\n"
        "`plot <sensor> YYMMDD_HHMM YYMMDD_HHMM` — plot custom time range in CDT (e.g. `plot P1 260622_0000 260622_1200`)\n"
        "`mode` — show current operating mode and what is being monitored\n"
        "`set mode auto` — automatic mode detection (based on 50K temperature)\n"
        "`set mode idle` — force IDLE mode (room temperature monitoring)\n"
        "`set mode cold` — force COLD mode (operational monitoring)\n"
        "`set mode transitioning` — force TRANSITIONING mode (cooling/warming — threshold alerts off)\n"
        "`list` — sensor numbers, short names, current thresholds\n"
        "`what is the alarm` — list every configured alarm + its trigger criteria, grouped by mode\n"
        "`what is current alarm criteria` — trigger criteria for the *current* mode only\n"
        "`what alarms in cold mode` — list alarms for one mode (idle/cold/transitioning)\n"
        "`filter status` — days until the chilled water filter is due for replacement\n"
        "`I have changed the filter` — confirm filter replaced, restart the 3-week timer\n"
        "`status` — active overrides and silenced sensors\n"
        "`ack` — silence ALL sensors for 10 min\n"
        "`(reply under an alert)` `silent for 2h` / `mute 30min` / `silence 3 days` / `silent forever` — snooze that one sensor\n"
        "`pause alerts` — stop ALL alert messages (monitoring still runs in background)\n"
        "`resume alerts` — re-enable all alerts\n"
        "`sentinel on` — resume CS2 alert forwarding\n"
        "`sentinel off` — pause CS2 alert forwarding\n"
        "`change <sensor> to <value> for ever` — override threshold for *current* mode\n"
        "`cold change <sensor> to <value> for ever` — override COLD mode threshold\n"
        "`idle change <sensor> to <value> for ever` — override IDLE mode threshold\n"
        "`reset <sensor>` / `cold reset <sensor>` / `idle reset <sensor>` — restore default\n\n"
        "_<sensor> = number (see `list`), short name, or full mapping name_",
        color="#0066cc", thread_ts=reply_ts)


def _format_threshold_alarms(thresholds: dict) -> list:
    """Turn a THRESHOLDS_* dict into human-readable alarm bullet lines,
    using the description text already stored in each tuple."""
    out = []
    for full, entry in thresholds.items():
        max_v, min_v = entry[0], entry[1]
        if len(entry) >= 4:            # (max, min, max_desc, min_desc)
            if max_v is not None and entry[2]:
                out.append(f"  • {entry[2]}")
            if min_v is not None and entry[3]:
                out.append(f"  • {entry[3]}")
        elif entry[2]:                 # (max, min, desc) — one bound set
            out.append(f"  • {entry[2]}")
    return out


def _cmd_list_alarms(state: dict, reply_ts=None, only_mode=None):
    """List every configured alarm together with the exact criterion that
    triggers it — grouped by operating mode, plus the always-on alarms.
    If only_mode is given, show just that mode's threshold alarms."""
    cur_mode = state.get("current_mode", "IDLE")
    header = ":rotating_light: *Configured Alarms — what triggers each one*"
    if only_mode:
        header += f"\n_{MODE_EMOJI.get(only_mode, '')} {only_mode} mode only_"
    lines = [header, ""]

    def add_mode_section(mode):
        emoji = MODE_EMOJI.get(mode, "")
        star  = "   ← *current*" if mode == cur_mode else ""
        lines.append(f"*{emoji} {mode} mode — alarm triggers when:*{star}")
        if mode == "COLD":
            body = _format_threshold_alarms(config.THRESHOLDS_COLD)
            body.append("  • Cold cathode (P1) is OFF (should be ON while cold)")
        elif mode == "IDLE":
            body = _format_threshold_alarms(config.THRESHOLDS_IDLE)
            body.append("  • Cold cathode (P1) is ON (should be OFF at room temperature)")
        else:  # TRANSITIONING
            body = ["  _Sensor threshold alarms are suppressed while cooling/warming._",
                    "  Direction is auto-detected from the 50K plate trend."]
            for direction, devices in TRANSITIONING_REQUIRED.items():
                names = ", ".join(lbl for _, lbl in devices)
                body.append(f"  *{direction.title()}:* CRITICAL if OFF → {names}")
        lines.extend(body or ["  _(none)_"])
        lines.append("")

    for md in ([only_mode] if only_mode else ["IDLE", "TRANSITIONING", "COLD"]):
        add_mode_section(md)

    # Always-on criteria, built from the real constants so they stay in sync.
    device_labels = ", ".join(lbl for _, lbl in DEVICE_ALERT_MAPPINGS)
    valve_labels  = " / ".join(lbl for _, lbl in MONITORED_VALVES)
    lines.append("*Always on (every mode) — alarm triggers when:*")
    lines.append(f"  • *Any device switches ON or OFF* — {device_labels}")
    lines.append("  • *R1A pump* — enable toggles, error flag set/cleared, "
                 "or pump power crosses 0.1 W (stop/restart)")
    lines.append(f"  • *Valves* — {valve_labels} open or close")
    lines.append(f"  • *CS2 system alert* — new entry with severity ≥ "
                 f"{config.CS2_ALERT_MIN_SEVERITY} (error)")
    lines.append("  • *Data sync stalled* — no new sensor reading for > 5 min")
    for cfg in getattr(config, "AIR_PRESSURE_ALARMS", {}).values():
        u = cfg["unit"]; s = cfg.get("scale", 1)
        parts = []
        if cfg.get("warn_below") is not None:
            parts.append(f"warning < {cfg['warn_below']*s:g}")
        if cfg.get("crit_below") is not None:
            parts.append(f"*CRITICAL* < {cfg['crit_below']*s:g}")
        lines.append(f"  • *{cfg['label']}* low — {', '.join(parts)} {u} "
                     f"(normal ~{cfg['normal']*s:g} {u})")
    lines.append("  • *Pulse-tube compressor* (checked only while running) — each of "
                 "*coolant-in, coolant-out, oil, helium temperature; high & low pressure; "
                 "motor current* has factory CRITICAL and WARNING limits (Cryomech built-in "
                 "fault flags): error → *CRITICAL*, warning → WARNING")
    lines.append("  • *Any other device fault* — every device is checked; "
                 "an error → *CRITICAL*, a warning → WARNING (helium compressor, "
                 "pumps, pressure gauges, valves, GHS, …)")

    lines.append(f"\n_Same alarm repeats at most every {config.ALERT_COOLDOWN_MINUTES} min "
                 "(cooldown). Reply `silent for 2h` under an alert, or `ack` to hush all._")
    lines.append("_`what alarms in cold mode` for one mode · "
                 "`what is the alarm` for everything · `list` for exact threshold numbers_")
    send_slack("\n".join(lines), color="#cc0000", thread_ts=reply_ts)


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

    mode_str = f"{emoji} `{mode}`"
    if mode == "TRANSITIONING" and state.get("transition_direction"):
        mode_str += f" — *{state['transition_direction']}*"
    lines = [
        f"*Monitor Status*\n",
        f"*Mode:* {mode_str}"
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
            when = "permanently" if until.startswith("9999") else f"until `{until[:16]}`"
            lines.append(f"  • `{s}` {when}")

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
        entry = thresholds[name]
        is_max = max_v is not None and value > max_v
        is_min = min_v is not None and value < min_v

        if not (is_max or is_min):
            continue

        # Support 4-element tuples: (max_v, min_v, max_desc, min_desc)
        if len(entry) >= 4:
            desc = entry[3] if is_min else entry[2]
        else:
            desc = entry[2]

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
    # Always advance the cursor so we don't re-process old alerts when re-enabled
    state["last_cs2_alert_id"] = max(r["id"] for r in rows)
    if not state.get("cs2_alerts_enabled", True):
        return []

    by_code = defaultdict(list)
    for row in rows:
        by_code[row["code"]].append(row)

    msgs = []
    for code, group in by_code.items():
        row   = group[0]
        emoji = ":red_circle:" if row["severity"] >= 2 else ":large_yellow_circle:"
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


HEATER_MAPPINGS = [
    ("HEATSWITCH_STILL_ENABLED", "Still Heat Switch"),
    ("HEATSWITCH_MXC_ENABLED",   "MXC Heat Switch"),
    ("STILL_HEATER_ENABLED",     "Still Heater"),
    ("MXC_HEATER_ENABLED",       "MXC Heater"),
]

# Additional devices monitored for state-change alerts (not shown in heater status)
DEVICE_ALERT_MAPPINGS = HEATER_MAPPINGS + [
    ("PULSE_TUBE_ENABLED", "Pulse Tube"),
    ("B1A_ENABLED",        "B1A Turbo Pump"),
    ("B2_ENABLED",         "B2 Turbo Pump"),
]

# Devices that MUST stay ON during TRANSITIONING, split by direction (cool down
# vs warm up) because the two phases can have different requirements. If any
# listed device is OFF, raise a CRITICAL alert. (Heat switches still get normal
# on/off change alerts via check_heater_status.)
TRANSITIONING_REQUIRED = {
    "cool down": [("PULSE_TUBE_ENABLED", "Pulse Tube")],
    "warm up":   [("PULSE_TUBE_ENABLED", "Pulse Tube")],
}

# How far the 50K plate must move (K) over the trend window to call a direction.
_TRANSITION_TREND_WINDOW_MIN = 15
_TRANSITION_TREND_DEADBAND_K = 0.5


def _transitioning_direction(conn) -> str | None:
    """Is the fridge cooling down or warming up? Decided from the 50K plate
    trend over the last ~15 min. Returns 'cool down', 'warm up', or None
    (flat/unknown — e.g. not enough history)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value, time FROM public.double_value_change_events "
                "WHERE mapping = %s ORDER BY time DESC LIMIT 1",
                (config.MODE_DETECTION_SENSOR,))
            latest = cur.fetchone()
            if not latest:
                return None
            cutoff = latest[1] - timedelta(minutes=_TRANSITION_TREND_WINDOW_MIN)
            cur.execute(
                "SELECT value FROM public.double_value_change_events "
                "WHERE mapping = %s AND time <= %s ORDER BY time DESC LIMIT 1",
                (config.MODE_DETECTION_SENSOR, cutoff))
            past = cur.fetchone()
    except Exception:
        return None
    if not past:
        return None
    delta = latest[0] - past[0]
    if delta <= -_TRANSITION_TREND_DEADBAND_K:
        return "cool down"
    if delta >= _TRANSITION_TREND_DEADBAND_K:
        return "warm up"
    return None


def check_heater_status(conn, state: dict) -> list:
    last_id = state.get("last_heater_event_id", 0)
    mappings = tuple(m for m, _ in DEVICE_ALERT_MAPPINGS)
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT id, mapping, value, time FROM public.boolean_value_change_events "
            "WHERE mapping = ANY(%s) AND id > %s ORDER BY id",
            (list(mappings), last_id))
        rows = cur.fetchall()
    if not rows:
        return []

    state["last_heater_event_id"] = max(r["id"] for r in rows)
    label_map = dict(DEVICE_ALERT_MAPPINGS)
    msgs = []
    for row in rows:
        label  = label_map.get(row["mapping"], row["mapping"])
        status = ":large_green_circle: *ON*" if row["value"] else ":red_circle: *OFF*"
        msgs.append(f":fire: *{label}* changed → {status}\nTime: {row['time']}")
        log.info(f"Heater change: {row['mapping']} = {row['value']}")
    return msgs


def check_transitioning_devices(conn, state: dict) -> list:
    """During TRANSITIONING, the devices required for the current direction
    (cool down / warm up) MUST stay ON. If any is OFF, raise a CRITICAL alert.
    State-based (not change-based), so it fires even if a device was already off
    before the mode was entered, and keeps reminding every cooldown while still
    off. Respects per-device ack."""
    if state.get("current_mode") != "TRANSITIONING":
        state.pop("transition_direction", None)
        return []

    direction = _transitioning_direction(conn)
    state["transition_direction"] = direction        # for status display
    # Unknown direction (flat) → still enforce, using the cool-down requirement.
    required   = TRANSITIONING_REQUIRED[direction or "cool down"]
    dir_label  = f" ({direction})" if direction else ""
    dir_verb   = {"cool down": "cooling down",
                  "warm up": "warming up"}.get(direction, "cooling down or warming up")

    results  = []
    now      = datetime.now()
    cooldown = timedelta(minutes=config.ALERT_COOLDOWN_MINUTES)
    acked    = state.setdefault("acked_sensors", {})
    mappings = [m for m, _ in required]

    ph = ",".join(["%s"] * len(mappings))
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            f"SELECT DISTINCT ON (mapping) mapping, value, time "
            f"FROM public.boolean_value_change_events "
            f"WHERE mapping IN ({ph}) ORDER BY mapping, time DESC",
            mappings)
        rows = {r["mapping"]: r for r in cur.fetchall()}

    label_map = dict(required)
    for mapping in mappings:
        row = rows.get(mapping)
        if row is None or row["value"]:
            continue                       # no data, or ON → fine

        ack_until = acked.get(mapping)
        if ack_until and datetime.fromisoformat(ack_until) > now:
            continue
        last = state["last_alert_time"].get(mapping)
        if last and now - datetime.fromisoformat(last) < cooldown:
            continue

        state["last_alert_time"][mapping] = now.isoformat()
        label = label_map[mapping]
        msg = (f":rotating_light: *CRITICAL — {label} is OFF during TRANSITIONING{dir_label}* :rotating_light:\n"
               f"The {label.lower()} must stay *ON* while the fridge is {dir_verb}.\n"
               f"Turned OFF at: {row['time']}\n"
               f"_React ✅ or reply `ok` / `silent for 2h` in thread to silence_")
        results.append((mapping, msg))
        log.critical(f"TRANSITIONING device OFF ({direction}): {mapping}")

    return results


def _cmd_heater_status(reply_ts: str, conn=None):
    if conn is None:
        send_slack("Cannot read heater status: no database connection.", thread_ts=reply_ts)
        return

    def latest_bool(mapping):
        with conn.cursor() as cur:
            cur.execute("SELECT value, time FROM public.boolean_value_change_events "
                        "WHERE mapping = %s ORDER BY time DESC LIMIT 1", (mapping,))
            r = cur.fetchone()
            return (bool(r[0]), r[1]) if r else (None, None)

    def latest_power(mapping):
        with conn.cursor() as cur:
            cur.execute("SELECT value, time FROM public.double_value_change_events "
                        "WHERE mapping = %s ORDER BY time DESC LIMIT 1", (mapping,))
            r = cur.fetchone()
            return float(r[0]) if r else None

    lines = [":fire: *Heater Status*\n"]

    for mapping, label in HEATER_MAPPINGS:
        val, ts = latest_bool(mapping)
        if val is None:
            lines.append(f"  *{label}*: _no data_")
            continue
        icon = ":large_green_circle:" if val else ":white_circle:"
        state_str = "*ON*" if val else "*OFF*"
        lines.append(f"{icon} *{label}*: {state_str}  _(updated {str(ts)[:19]})_")

    lines.append("")

    still_pwr = latest_power("STILL_HEATING_POWER")
    mxc_pwr   = latest_power("MXC_HEATING_POWER")
    if still_pwr is not None:
        lines.append(f"  *Still Heater power*: `{still_pwr*1000:.2f} mW`")
    if mxc_pwr is not None:
        lines.append(f"  *MXC Heater power*: `{mxc_pwr*1000:.2f} mW`")

    send_slack("\n".join(lines), color="#cc6600", thread_ts=reply_ts)
    log.info("Sent heater status reply to Slack")


def _cmd_pulsetube_status(reply_ts: str, conn=None):
    """Show the pulse-tube compressor's live readings and any active faults."""
    if conn is None:
        send_slack("Cannot read pulse tube status: no database connection.", thread_ts=reply_ts)
        return
    js = _latest_device_state(conn, "plc.Pulsetube1")
    if not js:
        send_slack("No pulse tube data available.", thread_ts=reply_ts)
        return

    running = bool(js.get("bCompressorRunning"))
    icon    = ":large_green_circle:" if running else ":red_circle:"

    def _C(f):   # Kelvin → Celsius
        v = js.get(f);  return f"{v - 273.15:.1f} °C" if v is not None else "—"
    def _psi(f):  # Pa → psi
        v = js.get(f);  return f"{v / 6894.757:.1f} psi" if v is not None else "—"
    def _A(f):
        v = js.get(f);  return f"{v:.1f} A"    if v is not None else "—"

    hours = js.get("fHoursOfOperation")
    lines = [
        ":cyclone: *Pulse Tube Compressor*\n",
        f"{icon} *{'RUNNING' if running else 'OFF'}*"
        + (f"   ·   {hours:.0f} h total operation" if hours is not None else ""),
        "",
        "*Temperatures:*",
        f"  • Coolant In: `{_C('fCoolantInTemp')}`    Coolant Out: `{_C('fCoolantOutTemp')}`",
        f"  • Oil: `{_C('fOilTemp')}`    Helium: `{_C('fHeliumTemp')}`",
        "*Pressures:*",
        f"  • High: `{_psi('fHighPressure')}`    Low: `{_psi('fLowPressure')}`",
        "*Motor:*",
        f"  • Current: `{_A('fMotorCurrent')}`",
    ]

    errors, warnings = _device_faults(js)
    if errors:
        lines.append("\n:rotating_light: *Faults (critical):* " + ", ".join(errors))
    if warnings:
        lines.append(":warning: *Warnings:* " + ", ".join(warnings))
    if not errors and not warnings:
        note = "within factory limits" if running else "compressor off — limits not checked"
        lines.append(f"\n:white_check_mark: No active faults ({note}).")

    color = "good" if (running and not errors and not warnings) else ("danger" if errors else "#cc6600")
    send_slack("\n".join(lines), color=color, thread_ts=reply_ts)
    log.info("Sent pulse tube status reply to Slack")


# ── Valve status command ──────────────────────────────────────────────────────

VALVE_MAPPINGS = [
    "V001", "V003", "V004", "V005",
    "V101", "V102", "V104", "V105", "V106", "V107", "V108",
    "V110", "V111", "V112", "V113", "V114",
    "V201G", "V202", "V203",
    "V401", "V402", "V403",
    "V501H", "V502H", "V503H",
]

def _cmd_valve_status(reply_ts: str, conn=None, highlight: str = None):
    """Show all valve statuses. If highlight is set (e.g. 'V112'), pin that valve first."""
    if conn is None:
        send_slack("Cannot read valve status: no database connection.", thread_ts=reply_ts)
        return

    hl = (highlight or "").upper()
    title = f":valve: *Valve Status — {hl}*\n" if hl else ":valve: *Valve Status*\n"
    lines = [title]
    open_valves, closed_valves, highlighted = [], [], []

    for v in VALVE_MAPPINGS:
        mapping = f"{v}_ENABLED"
        with conn.cursor() as cur:
            cur.execute("SELECT value, time FROM public.boolean_value_change_events "
                        "WHERE mapping = %s ORDER BY time DESC LIMIT 1", (mapping,))
            row = cur.fetchone()
        if row is None:
            continue
        is_open = bool(row[0])
        ts_str  = str(row[1])[:16]
        star = " ◄" if v == hl else ""
        if v == hl:
            state = "OPEN" if is_open else "CLOSED"
            icon  = ":large_green_circle:" if is_open else ":red_circle:"
            highlighted.append(f"{icon} *{v}* — *{state}*  _(at {ts_str})_{star}")
        elif is_open:
            open_valves.append(f":large_green_circle: *{v}* — OPEN  _(at {ts_str})_")
        else:
            closed_valves.append(f":white_circle: {v} — closed")

    if highlighted:
        lines.extend(highlighted)
        lines.append("")
        lines.append("_All valves:_")

    if open_valves:
        lines.append("*Open:*")
        lines.extend(open_valves)
        lines.append("")

    lines.append("*Closed:*")
    lines.extend(closed_valves)

    send_slack("\n".join(lines), color="#2E86AB", thread_ts=reply_ts)
    log.info(f"Sent valve status reply to Slack" + (f" [highlight={hl}]" if hl else ""))


# ── Valve change alerts (V112, V113, V114) ───────────────────────────────────

MONITORED_VALVES = [
    ("V112_ENABLED", "V112"),
    ("V113_ENABLED", "V113"),
    ("V114_ENABLED", "V114"),
]

def check_valve_changes(conn, state: dict) -> list:
    """Alert on any state change in V112, V113, V114 — both COLD and IDLE modes."""
    last_id = state.get("last_valve_event_id", 0)
    mappings = [m for m, _ in MONITORED_VALVES]
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT id, mapping, value, time FROM public.boolean_value_change_events "
            "WHERE mapping = ANY(%s) AND id > %s ORDER BY id",
            (mappings, last_id))
        rows = cur.fetchall()
    if not rows:
        return []

    state["last_valve_event_id"] = max(r["id"] for r in rows)
    label_map = dict(MONITORED_VALVES)
    msgs = []
    for row in rows:
        label  = label_map.get(row["mapping"], row["mapping"])
        status = ":large_green_circle: *OPEN*" if row["value"] else ":red_circle: *CLOSED*"
        msgs.append(f":warning: *Valve {label}* changed → {status}\nTime: {row['time']}")
        log.info(f"Valve change: {row['mapping']} = {row['value']}")
    return msgs


def check_cold_cathode(conn, state: dict):
    mode = state.get("current_mode")
    if mode not in ("IDLE", "COLD"):
        return None

    with conn.cursor() as cur:
        cur.execute("SELECT value FROM public.boolean_value_change_events "
                    "WHERE mapping = 'P1_ENABLED' ORDER BY time DESC LIMIT 1")
        row = cur.fetchone()
    if row is None:
        return None

    cc_on = bool(row[0])
    key   = "COLD_CATHODE"

    # IDLE: cold cathode should be OFF
    # COLD: cold cathode should be ON
    problem = (mode == "IDLE" and cc_on) or (mode == "COLD" and not cc_on)
    if not problem:
        state.setdefault("last_alert_time", {}).pop(key, None)
        return None

    last = state.get("last_alert_time", {}).get(key)
    if last and datetime.now() - datetime.fromisoformat(last) < timedelta(minutes=config.ALERT_COOLDOWN_MINUTES):
        return None

    state.setdefault("last_alert_time", {})[key] = datetime.now().isoformat()

    if mode == "IDLE":
        msg = (":warning: *Cold Cathode (P1) is ON at room temperature!*\n"
               "System is in IDLE mode — cold cathode gauge should be OFF.")
    else:
        msg = (":warning: *Cold Cathode (P1) is OFF while fridge is COLD!*\n"
               "System is in COLD mode — cold cathode gauge should be ON.")

    log.warning(f"Cold cathode alert: mode={mode}, P1_ENABLED={cc_on}")
    return msg


def _latest_device_state(conn, device_id: str) -> dict | None:
    """Return the latest device_states JSON blob for a device, or None."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT values FROM public.device_states "
                        "WHERE device_id = %s ORDER BY datetime DESC LIMIT 1",
                        (device_id,))
            row = cur.fetchone()
    except Exception as e:
        log.error(f"device_states read failed for {device_id}: {e}")
        return None
    if not row or row[0] is None:
        return None
    val = row[0]
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            return None
    return val if isinstance(val, dict) else None


def _humanize_flag(flag: str) -> str:
    """'bErrorOilRunningHigh' -> 'Oil Running High'."""
    name = re.sub(r"^b(?:Error|Warning)", "", flag)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).strip()


def check_air_pressure(conn, state: dict) -> list:
    """GHS compressed-air pressures (device_states JSON) — checked in ALL modes.
    Two levels: WARNING below warn_below, CRITICAL below crit_below (thresholds in
    Pa). Escalation (warning → critical) fires immediately, bypassing cooldown."""
    alarms = getattr(config, "AIR_PRESSURE_ALARMS", {})
    if not alarms:
        return []
    js = _latest_device_state(conn, config.AIR_PRESSURE_DEVICE)
    if not js:
        return []

    results  = []
    now      = datetime.now()
    cooldown = timedelta(minutes=config.ALERT_COOLDOWN_MINUTES)
    acked    = state.setdefault("acked_sensors", {})
    levels   = state.setdefault("air_pressure_level", {})

    for field, cfg in alarms.items():
        val = js.get(field)
        if val is None:
            continue
        crit = cfg.get("crit_below")
        warn = cfg.get("warn_below")
        if crit is not None and val < crit:
            level = "CRITICAL"
        elif warn is not None and val < warn:
            level = "WARNING"
        else:                               # normal / recovered
            levels.pop(field, None)
            state["last_alert_time"].pop(field, None)
            continue

        ack_until = acked.get(field)
        if ack_until and datetime.fromisoformat(ack_until) > now:
            continue
        escalated = (level == "CRITICAL" and levels.get(field) != "CRITICAL")
        last = state["last_alert_time"].get(field)
        if not escalated and last and now - datetime.fromisoformat(last) < cooldown:
            continue

        state["last_alert_time"][field] = now.isoformat()
        levels[field] = level
        scale = cfg.get("scale", 1)
        unit  = cfg["unit"]
        thr   = (crit if level == "CRITICAL" else warn) * scale
        emoji = ":rotating_light:" if level == "CRITICAL" else ":warning:"
        tail  = " :rotating_light:" if level == "CRITICAL" else ""
        msg = (f"{emoji} *{level} — {cfg['label']} is LOW*{tail}\n"
               f"Current: `{val*scale:g} {unit}` (normal ~{cfg['normal']*scale:g} {unit}; "
               f"{level.lower()} below {thr:g} {unit}).\n"
               f"_React ✅ or reply `ok` / `silent for 2h` in thread to silence_")
        results.append((field, msg))
        log.warning(f"Air pressure {level}: {field} = {val} Pa")
    return results


def _device_running(js: dict):
    """Return True/False if the device exposes an on/off indicator, else None.
    Used to only evaluate operational fault flags while the device is running
    (e.g. the pulse-tube compressor's coolant/oil/He/pressure/current limits
    only make sense with the compressor ON)."""
    for k in ("bCompressorRunning", "bPumpOnOff", "bRunning"):
        if k in js:
            return bool(js[k])
    return None


def _device_faults(js: dict) -> tuple:
    """From a device_states JSON blob, return (errors, warnings) as lists of
    human-readable strings, combining statusInfo and per-device bError/bWarning
    flags (skipping benign flags in config.DEVICE_FLAG_IGNORE). The operational
    bError/bWarning flags are only evaluated while the device is running — when
    it is explicitly off, only device-level statusInfo faults are reported."""
    ignore = getattr(config, "DEVICE_FLAG_IGNORE", ())
    def _benign(flag):
        low = flag.lower()
        return any(sub in low for sub in ignore)

    si = js.get("statusInfo") or {}
    errors   = [str(e) for e in (si.get("errors")   or [])]
    warnings = [str(w) for w in (si.get("warnings") or [])]
    if _device_running(js) is not False:       # running, or no on/off indicator
        for k, v in js.items():
            if v is not True or _benign(k):
                continue
            if k.startswith("bError"):
                errors.append(_humanize_flag(k))
            elif k.startswith("bWarning"):
                warnings.append(_humanize_flag(k))
    if si.get("errorBit") and not errors:
        errors.append("error bit set")
    if si.get("warningBit") and not warnings:
        warnings.append("warning bit set")
    return sorted(set(errors)), sorted(set(warnings))


def check_device_health(conn, state: dict) -> list:
    """Health of EVERY device in device_states (all modes). Any device reporting
    an error → CRITICAL, a warning → WARNING, using each device's own status flags
    (pulse tube, compressor, pumps, gauges, valves, GHS, …). Re-alerts when the
    active-fault set changes or after the cooldown; clears when back to healthy.
    Per-device+severity ack/silence."""
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT DISTINCT ON (device_id) device_id, values "
                        "FROM public.device_states ORDER BY device_id, datetime DESC")
            rows = cur.fetchall()
    except Exception as e:
        log.error(f"device_states read failed: {e}")
        return []

    results  = []
    now      = datetime.now()
    cooldown = timedelta(minutes=config.ALERT_COOLDOWN_MINUTES)
    acked    = state.setdefault("acked_sensors", {})
    prev     = dict(state.get("device_health", {}))
    current  = {}

    for row in rows:
        dev = row["device_id"]
        js  = row["values"]
        if isinstance(js, str):
            try:
                js = json.loads(js)
            except Exception:
                continue
        if not isinstance(js, dict):
            continue
        name = (js.get("instrumentInfo") or {}).get("name") or js.get("identifier") or dev
        errors, warnings = _device_faults(js)

        for sev, items, emoji, level in (
            ("error",   errors,   ":rotating_light:", "CRITICAL"),
            ("warning", warnings, ":warning:",        "WARNING"),
        ):
            key = f"DEVICE::{dev}::{sev}"
            if not items:
                state["last_alert_time"].pop(key, None)
                continue
            current[key] = items
            ack_until = acked.get(key)
            if ack_until and datetime.fromisoformat(ack_until) > now:
                continue
            changed = set(items) != set(prev.get(key, []))
            last    = state["last_alert_time"].get(key)
            if not changed and last and now - datetime.fromisoformat(last) < cooldown:
                continue
            state["last_alert_time"][key] = now.isoformat()
            body   = "\n".join(f"    • {x}" for x in items)
            tail   = f" {emoji}" if level == "CRITICAL" else ""
            plural = "s" if len(items) > 1 else ""
            msg = (f"{emoji} *{level} — {name}: {sev}{plural}*{tail}\n{body}\n"
                   f"_React ✅ or reply `ok` / `silent for 2h` in thread to silence_")
            results.append((key, msg))
            log.warning(f"Device {sev} [{dev}]: {items}")

    state["device_health"] = current
    return results


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


def check_cooldown_milestone(conn, state: dict) -> list:
    """Informational one-time notifications when a plate cools past a threshold
    (downward crossing only, i.e. during cool-down). Re-arms after warming back
    above threshold + hysteresis. Not a repeating alarm."""
    milestones = getattr(config, "COOLDOWN_MILESTONES", [])
    if not milestones:
        return []
    hyst  = getattr(config, "COOLDOWN_MILESTONE_HYSTERESIS_K", 1.0)
    flags = state.setdefault("cooldown_milestones", {})
    results = []
    with conn.cursor() as cur:
        for mapping, below_k, label in milestones:
            cur.execute("SELECT value FROM public.double_value_change_events "
                        "WHERE mapping = %s ORDER BY time DESC LIMIT 1", (mapping,))
            row = cur.fetchone()
            if not row or row[0] is None:
                continue
            v   = float(row[0])
            key = f"{mapping}<{below_k:g}"
            if key not in flags:
                flags[key] = (v < below_k)      # first sight → arm, don't alert
                continue
            if v < below_k and not flags[key]:
                flags[key] = True
                results.append((None,
                    f":snowflake: *{label} is now below {below_k:g} K* — currently "
                    f"`{v:.2f} K`.\n_Cool-down milestone._"))
                log.info(f"Cooldown milestone: {label} below {below_k} K ({v:.3g} K)")
            elif v >= below_k + hyst and flags[key]:
                flags[key] = False              # re-arm after warming back up
    return results


# ── Init ──────────────────────────────────────────────────────────────────────

def init_state(conn) -> dict:
    state = _empty_state()
    # Preserve user preferences that survive a re-init
    existing = load_state()
    if existing:
        state["cs2_alerts_enabled"] = existing.get("cs2_alerts_enabled", True)
        state["current_mode"]       = existing.get("current_mode")
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(id) FROM public.alerts WHERE severity >= %s",
                    (config.CS2_ALERT_MIN_SEVERITY,))
        state["last_cs2_alert_id"] = cur.fetchone()[0] or 0
        cur.execute("SELECT MAX(id) FROM public.boolean_value_change_events "
                    "WHERE mapping IN ('R1A_ENABLED', 'R1A_ERROR_VALUE')")
        state["last_r1a_event_id"] = cur.fetchone()[0] or 0
        cur.execute("SELECT MAX(id) FROM public.boolean_value_change_events "
                    "WHERE mapping IN ('HEATSWITCH_STILL_ENABLED','HEATSWITCH_MXC_ENABLED',"
                    "'STILL_HEATER_ENABLED','MXC_HEATER_ENABLED','PULSE_TUBE_ENABLED')")
        state["last_heater_event_id"] = cur.fetchone()[0] or 0
        cur.execute("SELECT MAX(id) FROM public.boolean_value_change_events "
                    "WHERE mapping IN ('V112_ENABLED','V113_ENABLED','V114_ENABLED')")
        state["last_valve_event_id"] = cur.fetchone()[0] or 0
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

def generate_summary(conn) -> str:
    now_utc  = datetime.now(timezone.utc)
    since    = now_utc - timedelta(hours=12)
    now_cdt  = now_utc.astimezone(_CDT)
    period   = f"{(now_cdt - timedelta(hours=12)).strftime('%m-%d %H:%M')} – {now_cdt.strftime('%m-%d %H:%M')} CDT"

    lines = [f"*BlueFors 12-Hour Summary* | {now_cdt.strftime('%Y-%m-%d %H:%M')} CDT", f"_{period}_", ""]

    # ── Current mode & key readings ──────────────────────────────────────────
    state = load_state()
    mode  = state.get("current_mode", "unknown")
    mode_emoji = {"IDLE": ":white_circle:", "COLD": ":large_blue_circle:", "TRANSITIONING": ":large_yellow_circle:"}.get(mode, ":grey_question:")
    lines.append(f"*Mode:* {mode_emoji} {mode}")

    def latest(mapping):
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM public.double_value_change_events "
                        "WHERE mapping=%s ORDER BY time DESC LIMIT 1", (mapping,))
            r = cur.fetchone()
        return float(r[0]) if r else None

    lines.append("*Current readings:*")
    temp_parts = []
    for mapping, label in [("MXC_TEMPERATURE","MXC"), ("STILL_TEMPERATURE","Still"),
                            ("4K_TEMPERATURE","4K"), ("50K_TEMPERATURE","50K")]:
        v = latest(mapping)
        if v is not None:
            if mapping == "MXC_TEMPERATURE":
                temp_parts.append(f"{label}: {v*1000:.2f} mK")
            else:
                temp_parts.append(f"{label}: {v:.3f} K")
    if temp_parts:
        lines.append("• Temp — " + "  |  ".join(temp_parts))

    pres_parts = []
    for i in range(1, 8):
        v = latest(f"P{i}_PRESSURE")
        if v is not None:
            pres_parts.append(f"P{i}: {_fmt_pressure(v)}")
    if pres_parts:
        lines.append("• Pressure — " + "  |  ".join(pres_parts))

    v = latest("FLOW_VALUE")
    if v is not None:
        lines.append(f"• Flow — {v:.3f} mmol/s")

    with conn.cursor() as cur:
        cur.execute("SELECT value FROM public.boolean_value_change_events "
                    "WHERE mapping='PULSE_TUBE_ENABLED' ORDER BY time DESC LIMIT 1")
        r = cur.fetchone()
    if r is not None:
        pt_state = "ON" if r[0] else "OFF"
        lines.append(f"• Pulse Tube — {pt_state}")
    lines.append("")

    # ── Device state changes in last 12h ────────────────────────────────────
    TRACKED = [
        ("R1A_ENABLED",             "R1A Pump"),
        ("R1A_ERROR_VALUE",         "R1A Error"),
        ("HEATSWITCH_STILL_ENABLED","Still Heat Switch"),
        ("HEATSWITCH_MXC_ENABLED",  "MXC Heat Switch"),
        ("STILL_HEATER_ENABLED",    "Still Heater"),
        ("MXC_HEATER_ENABLED",      "MXC Heater"),
        ("PULSE_TUBE_ENABLED",      "Pulse Tube"),
        ("P1_ENABLED",              "Cold Cathode (P1)"),
        ("V112_ENABLED",            "Valve V112"),
        ("V113_ENABLED",            "Valve V113"),
        ("V114_ENABLED",            "Valve V114"),
    ]
    change_lines = []
    for mapping, label in TRACKED:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT time, value FROM public.boolean_value_change_events "
                "WHERE mapping=%s AND time>=%s ORDER BY time", (mapping, since))
            evts = cur.fetchall()
        for t, val in evts:
            t_cdt = t.astimezone(_CDT)
            state_str = "ON" if val else "OFF"
            change_lines.append(f"• {label}: → *{state_str}* at {t_cdt.strftime('%H:%M')}")

    lines.append("*Device changes (12h):*")
    if change_lines:
        lines.extend(change_lines)
    else:
        lines.append("• No device state changes")
    lines.append("")

    # ── CS2 alerts in last 12h ───────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            "SELECT code, title, severity, COUNT(*) as cnt "
            "FROM public.alerts WHERE datetime>=%s GROUP BY code, title, severity ORDER BY cnt DESC",
            (since,))
        alert_rows = cur.fetchall()

    lines.append("*CS2 alerts (12h):*")
    if alert_rows:
        for code, title, sev, cnt in alert_rows:
            emoji = ":red_circle:" if sev >= 2 else ":large_yellow_circle:"
            lines.append(f"• {emoji} [{code}] {title}" + (f" ×{cnt}" if cnt > 1 else ""))
    else:
        lines.append("• No CS2 alerts")
    lines.append("")

    # ── Threshold alerts fired (from monitor.log) ────────────────────────────
    # Count threshold alert lines in last 12h by scanning log
    threshold_hits = []
    try:
        log_path = Path(__file__).parent / "monitor.log"
        cutoff_str = since.astimezone(_CDT).strftime("%Y-%m-%d %H:%M")
        with open(log_path) as f:
            for line in f:
                if "Sending alert:" in line or "Sent" in line and "alert" in line.lower():
                    ts_part = line[:19]
                    try:
                        if ts_part >= cutoff_str[:16]:
                            threshold_hits.append(line.strip())
                    except Exception:
                        pass
    except Exception:
        pass

    def trend_arrow(mapping, scale=1.0):
        """Least-squares slope direction over 12h. Returns '↑', '↓', or '→'."""
        with conn.cursor() as cur:
            cur.execute("SELECT time, value FROM public.double_value_change_events "
                        "WHERE mapping=%s AND time>=%s ORDER BY time", (mapping, since))
            rows = cur.fetchall()
        if len(rows) < 3:
            return None
        t0 = rows[0][0].timestamp()
        xs = np.array([(r[0].timestamp() - t0) / 3600 for r in rows])
        ys = np.array([float(r[1]) * scale for r in rows])
        slope, _ = np.polyfit(xs, ys, 1)
        # flat if total drift over 12h < 1% of mean
        mean_val = np.mean(ys)
        if mean_val != 0 and abs(slope * 12) / abs(mean_val) < 0.01:
            return "➡️"
        return "📈" if slope > 0 else "📉"

    # Linear trend direction (least-squares fit through all 12h data points)
    lines.append("*12h linear trend:*")

    temp_parts = []
    for mapping, label in [("MXC_TEMPERATURE","MXC"), ("STILL_TEMPERATURE","Still"),
                            ("4K_TEMPERATURE","4K"), ("50K_TEMPERATURE","50K")]:
        arr = trend_arrow(mapping)
        if arr:
            temp_parts.append(f"{label} {arr}")
    if temp_parts:
        lines.append("• Temp — " + "  |  ".join(temp_parts))

    pres_parts = []
    for i in range(1, 8):
        arr = trend_arrow(f"P{i}_PRESSURE")
        if arr:
            pres_parts.append(f"P{i} {arr}")
    if pres_parts:
        lines.append("• Pressure — " + "  |  ".join(pres_parts))

    arr = trend_arrow("FLOW_VALUE")
    if arr:
        lines.append(f"• Flow {arr}")

    lines.append("")

    return "\n".join(lines)


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

        if SUMMARY_MODE:
            msg = generate_summary(conn)
            send_slack(msg, color="good")
            log.info("Daily summary sent to Slack")
            return

        # 1. Poll Slack for acks (commands handled by slack_responder.py)
        check_acknowledgements(state)

        # 2. Detect / update operating mode
        update_mode(conn, state)

        # Maintenance: chilled water filter reminder (independent of pause/ack)
        check_filter_reminder(state)

        # 3. Run checks
        all_alerts = []

        freshness = check_data_freshness(conn, state)
        if freshness:
            all_alerts.append((None, freshness))

        all_alerts.extend(check_sensor_thresholds(conn, state))

        # TRANSITIONING: pulse tube must stay ON (critical)
        all_alerts.extend(check_transitioning_devices(conn, state))

        # GHS compressed-air pressure (all modes, warning + critical)
        all_alerts.extend(check_air_pressure(conn, state))

        # Health of every device in device_states (all modes, warning + critical)
        all_alerts.extend(check_device_health(conn, state))

        # Informational cool-down milestones (e.g. 4K plate below 10 K)
        all_alerts.extend(check_cooldown_milestone(conn, state))

        for msg in check_cs2_alerts(conn, state):
            all_alerts.append((None, msg))

        for msg in check_r1a_status(conn, state):
            all_alerts.append((None, msg))

        for msg in check_heater_status(conn, state):
            all_alerts.append((None, msg))

        for msg in check_valve_changes(conn, state):
            all_alerts.append((None, msg))

        msg = check_cold_cathode(conn, state)
        if msg:
            all_alerts.append((None, msg))

    finally:
        conn.close()

    # 4. Send alerts and track message timestamps for ack tracking
    if state.get("alerts_paused"):
        save_state(state)
        if all_alerts:
            log.info(f"Alerts paused — suppressed {len(all_alerts)} alert(s)")
        return

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
