"""Control commands: pause/resume, sentinel, set mode, ack, threshold change /
reset, filter confirmation + status."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from .. import config
from ..core import timeutil as tu
from ..core import units
from ..modes import MODE_DESC, MODE_EMOJI, MODES, active_thresholds, normalise_mode, override_key
from ..reminders import FilterReminder
from .context import CommandContext

log = logging.getLogger("bfm.commands")

ALL_THRESHOLDS = {**config.THRESHOLDS_COLD, **config.THRESHOLDS_IDLE}


def cmd_pause(cc: CommandContext, via_nlp: bool = False) -> None:
    cc.st["alerts_paused"] = True
    cc.reply(":no_bell: *All alerts paused.* Monitoring continues in the background.\n"
             "Send `resume alerts` to re-enable.", color="warning")
    log.info("All alerts paused via Slack%s", " (NLP)" if via_nlp else "")


def cmd_resume(cc: CommandContext, via_nlp: bool = False) -> None:
    cc.st["alerts_paused"] = False
    cc.reply(":bell: *Alerts resumed.* All threshold and system alerts are active again.", color="good")
    log.info("Alerts resumed via Slack%s", " (NLP)" if via_nlp else "")


def cmd_sentinel(cc: CommandContext, on: bool | None) -> None:
    if on is None:
        cc.reply("Say `sentinel on` or `sentinel off`.")
        return
    cc.st["cs2_alerts_enabled"] = on
    if on:
        cc.reply(":large_green_circle: *Sentinel alerts ON* — CS2 alerts will be forwarded to Slack.", color="good")
    else:
        cc.reply(":white_circle: *Sentinel alerts OFF* — CS2 alerts paused until `sentinel on`.", color="warning")
    log.info("CS2 sentinel alerts %s via Slack", "enabled" if on else "disabled")


def cmd_set_mode(cc: CommandContext, word: str) -> None:
    st = cc.st
    val = normalise_mode(word)
    if val == "AUTO":
        st["mode_override"] = None
        cc.reply("✅ Mode detection set to *automatic* (based on 50K temperature).", color="good")
        log.info("Mode override cleared — back to auto")
    elif val in MODES:
        st["mode_override"] = val
        st["current_mode"] = val
        st["mode_since"] = tu.now().isoformat()
        st["last_alert_time"] = {}
        st["acked_sensors"] = {}
        cc.reply(f"✅ Mode manually set to *{val}*. {MODE_EMOJI[val]}\n{MODE_DESC[val]}", color="good")
        log.info("Mode manually set to %s", val)
    else:
        cc.reply(f"Unknown mode `{word}`. Valid options: `auto`, `idle`, `cold`, `transitioning`.")


def cmd_ack(cc: CommandContext) -> None:
    """Silence every threshold sensor for 10 min and forget pending alert
    threads. (Legacy asymmetry kept on purpose: only *threshold* keys are
    silenced, but ALL pending ack-tracking entries are cleared.)"""
    until = tu.minutes_from_now(config.ACK_DEFAULT_MINUTES)
    acked = cc.st.setdefault("acked_sensors", {})
    for s in ALL_THRESHOLDS:
        acked[s] = until
    cc.st.setdefault("pending_alert_msgs", {}).clear()
    cc.reply(f"✅ All active alerts acknowledged — silenced for {config.ACK_DEFAULT_MINUTES} minutes.", color="good")
    log.info("All alerts acked via Slack")


def _overrides_for(st: dict, mode_key: str | None) -> dict:
    if mode_key == "cold":
        return st.setdefault("threshold_overrides_cold", {})
    if mode_key == "idle":
        return st.setdefault("threshold_overrides_idle", {})
    return st.setdefault(override_key(st.get("current_mode", "IDLE")), {})


def _thresholds_for(st: dict, mode_key: str | None) -> dict:
    if mode_key == "cold":
        return config.THRESHOLDS_COLD
    if mode_key == "idle":
        return config.THRESHOLDS_IDLE
    return active_thresholds(st)


def cmd_reset_threshold(cc: CommandContext, sensor_token: str, mode_key: str | None) -> None:
    st = cc.st
    sensor = units.resolve_sensor(sensor_token)
    if not sensor:
        cc.reply(f"Unknown sensor `{sensor_token}`. Use `list` to see all sensors.")
        return
    _overrides_for(st, mode_key).pop(sensor.mapping, None)
    mode_label = f"[{(mode_key or st.get('current_mode', 'current')).upper()}] "
    entry = _thresholds_for(st, mode_key).get(sensor.mapping) or ALL_THRESHOLDS.get(sensor.mapping, (None, None, ""))
    default_val = entry[0] if entry[0] is not None else entry[1]
    cc.reply(f"✅ {mode_label}*{sensor.mapping}* reset to default: `{default_val} {sensor.unit}`", color="good")
    log.info("%s%s threshold reset to default", mode_label, sensor.mapping)


def parse_override_duration(raw: str) -> tuple[str | None, str] | None:
    """'for 2h' / '30min' / 'ever' → (expires_at_iso | None, label) or None if invalid."""
    r = raw.strip().lower()
    if r in ("ever", "forever", "permanent", "permanently"):
        return None, "*permanently*"
    h = re.fullmatch(r"(\d+(?:\.\d+)?)\s*h(?:ours?)?", r)
    m = re.fullmatch(r"(\d+)\s*min(?:utes?)?", r)
    if h:
        mins = float(h.group(1)) * 60
        return tu.minutes_from_now(mins), f"for *{h.group(1)} hours*"
    if m:
        return tu.minutes_from_now(int(m.group(1))), f"for *{int(m.group(1))} minutes*"
    return None


def apply_override(st: dict, sensor_mapping: str, value: float, expires_at: str | None, mode_key: str | None) -> None:
    entry = ALL_THRESHOLDS.get(sensor_mapping, (None, None, ""))
    if entry[0] is not None:
        ov = {"max_val": value, "min_val": entry[1], "expires_at": expires_at}
    else:
        ov = {"max_val": entry[0], "min_val": value, "expires_at": expires_at}
    _overrides_for(st, mode_key)[sensor_mapping] = ov


def cmd_change_threshold(cc: CommandContext, sensor_token: str, value_text: str, dur_text: str,
                         mode_key: str | None) -> None:
    st = cc.st
    sensor = units.resolve_sensor(sensor_token)
    if not sensor:
        cc.reply(f"Unknown sensor `{sensor_token}`. Use `list` to see all sensors.")
        return
    try:
        new_val = float(value_text)
    except ValueError:
        cc.reply(f"Invalid value `{value_text}`.")
        return
    parsed = parse_override_duration(dur_text)
    if parsed is None:
        cc.reply(f"Unknown duration `{dur_text}`.\nUse `for 30min`, `for 2h`, `for 24h`, or `for ever`.")
        return
    expires_at, label = parsed
    apply_override(st, sensor.mapping, new_val, expires_at, mode_key)
    mode_label = f"[{(mode_key or st.get('current_mode', 'current')).upper()}] "
    cc.reply(f"✅ {mode_label}*{sensor.mapping}* threshold → `{new_val} {sensor.unit}` {label}.", color="good")
    log.info("%s%s threshold → %s %s", mode_label, sensor.mapping, new_val, label)


# ── filter ───────────────────────────────────────────────────────────────────
_filter = FilterReminder()


def cmd_filter_done(cc: CommandContext) -> None:
    _filter.confirm(cc.st, cc.enqueue, reply_ts=cc.reply_ts)


def cmd_filter_status(cc: CommandContext) -> None:
    cc.reply(_filter.status_text(cc.st))
