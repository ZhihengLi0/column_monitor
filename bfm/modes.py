"""Operating modes (IDLE / TRANSITIONING / COLD), mode override, thresholds
with per-mode overrides, and the cool-down/warm-up direction estimate.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from . import config
from .core import timeutil as tu

log = logging.getLogger("bfm.modes")

MODES = ("IDLE", "TRANSITIONING", "COLD")

MODE_EMOJI = {"IDLE": "⚪", "TRANSITIONING": "🔵", "COLD": "🟢"}
MODE_COLOR = {"IDLE": "#aaaaaa", "TRANSITIONING": "#0066cc", "COLD": "good"}
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

_MODE_ALIASES = {
    "AUTO": "AUTO", "AUTOMATIC": "AUTO",
    "IDLE": "IDLE", "ROOM": "IDLE", "WARM": "IDLE",
    "COLD": "COLD", "BASE": "COLD", "OPERATIONAL": "COLD",
    "TRANSITIONING": "TRANSITIONING", "TRANSITION": "TRANSITIONING",
    "TRANS": "TRANSITIONING", "COOLING": "TRANSITIONING", "WARMING": "TRANSITIONING",
}


def normalise_mode(word: str) -> str:
    """User-typed mode word → canonical form (or the upper-cased input so the
    caller can reject it)."""
    w = (word or "").strip().upper()
    return _MODE_ALIASES.get(w, w)


def mode_in_text(t: str) -> str | None:
    """A mode mentioned anywhere in free text (TRANSITIONING first so
    'warming' beats 'warm')."""
    t = t.lower()
    if re.search(r"transition|\btrans\b|cooling|warming|过渡|降温|升温", t):
        return "TRANSITIONING"
    if re.search(r"cold|低温|冷", t):
        return "COLD"
    if re.search(r"idle|室温|\bwarm\b|暖", t):
        return "IDLE"
    return None


# ── detection ────────────────────────────────────────────────────────────────

def detect_mode(ctx) -> str:
    """Mode from the 50K plate; IDLE if there is no reading."""
    v = ctx.latest_value(config.MODE_DETECTION_SENSOR)
    if v is None:
        return "IDLE"
    if v > config.MODE_IDLE_ABOVE_K:
        return "IDLE"
    if v < config.MODE_COLD_BELOW_K:
        return "COLD"
    return "TRANSITIONING"


def update_mode(st: dict, ctx, outbox) -> str | None:
    """Apply override or detected mode; on change post a notice and wipe the
    per-sensor cooldowns + silences (legacy semantics). Returns the new mode
    if it changed, else None."""
    new_mode = st.get("mode_override") or detect_mode(ctx)
    old_mode = st.get("current_mode")
    if new_mode == old_mode:
        return None
    st["current_mode"] = new_mode
    st["mode_since"] = tu.now().isoformat()
    outbox(f"{MODE_EMOJI[new_mode]} *Mode changed: {old_mode or 'STARTUP'} → {new_mode}*\n"
           f"{MODE_DESC[new_mode]}", color=MODE_COLOR[new_mode], key="MODE", kind="alarm")
    log.info("Mode changed: %s → %s", old_mode, new_mode)
    st["last_alert_time"] = {}
    st["acked_sensors"] = {}
    return new_mode


# ── thresholds + overrides ───────────────────────────────────────────────────

def active_thresholds(st: dict) -> dict:
    mode = st.get("current_mode", "IDLE")
    if mode == "COLD":
        return config.THRESHOLDS_COLD
    if mode == "IDLE":
        return config.THRESHOLDS_IDLE
    return {}   # TRANSITIONING: no threshold checks


def override_key(mode: str | None) -> str:
    return "threshold_overrides_cold" if mode == "COLD" else "threshold_overrides_idle"


def mode_overrides(st: dict, mode: str | None = None) -> dict:
    return st.setdefault(override_key(mode or st.get("current_mode", "IDLE")), {})


def get_threshold(name: str, st: dict) -> tuple[float | None, float | None]:
    """(max, min) for a sensor in the current mode, honouring an unexpired override."""
    ov = mode_overrides(st)
    if name in ov:
        entry = ov[name]
        exp = entry.get("expires_at")
        if exp is None or tu.is_future(exp):
            return entry.get("max_val"), entry.get("min_val")
        del ov[name]
        log.info("Threshold override for %s expired", name)
    t = active_thresholds(st).get(name, (None, None, ""))
    return t[0], t[1]


# ── transition direction ─────────────────────────────────────────────────────
TREND_WINDOW_MIN = 15
TREND_DEADBAND_K = 0.5


def transition_direction(ctx) -> str | None:
    """'cool down' / 'warm up' / None from the 50K plate trend over ~15 min."""
    latest = ctx.latest(config.MODE_DETECTION_SENSOR)
    if not latest:
        return None
    cutoff = latest[1] - timedelta(minutes=TREND_WINDOW_MIN)
    try:
        past = ctx.cs2.latest_before(config.MODE_DETECTION_SENSOR, cutoff)
    except Exception:
        return None
    if not past:
        return None
    delta = float(latest[0]) - float(past[0])
    if delta <= -TREND_DEADBAND_K:
        return "cool down"
    if delta >= TREND_DEADBAND_K:
        return "warm up"
    return None
