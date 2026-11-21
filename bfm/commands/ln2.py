"""Liquid-nitrogen scale commands (merged from ln2_monitor/ln2_query.py).

Silencing now lives in the shared state: `acked_sensors["LN2::__all__"]`,
which the AlarmEngine honours for every LN2::* key. The old implementation
wrote a separate ln2 state file with no lock (lost-update race with the ln2
cron)."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from .. import config
from ..alarms import dew_point
from ..core import timeutil as tu
from ..core import units
from . import plot
from .context import CommandContext

log = logging.getLogger("bfm.commands")

LN2_RE = re.compile(r"\bln2\b|liquid\s*nitrogen|nitrogen|\bweight\b|\bscale\b|"
                    r"液氮|氮气|氮|秤|重量|称重|磅", re.IGNORECASE)
PLOT_RE = re.compile(r"\bplot\b|\bgraph\b|\btrend\b|\bchart\b|图|曲线|趋势", re.IGNORECASE)
SILENCE_RE = re.compile(r"silence|mute|quiet|snooze|stop.*alert|静音|安静|别报|停止", re.IGNORECASE)
UNSILENCE_RE = re.compile(r"unsilence|unmute|resume|un-?silence|恢复|取消静音|解除", re.IGNORECASE)
ALL_KEY = "LN2::__all__"


def silence(cc: CommandContext, minutes: float) -> None:
    until = tu.now() + timedelta(minutes=minutes)
    cc.st.setdefault("acked_sensors", {})[ALL_KEY] = until.isoformat()
    if minutes >= 1440:
        dur = f"{minutes / 1440:g} day(s)"
    elif minutes >= 60:
        dur = f"{minutes / 60:g} hour(s)"
    else:
        dur = f"{minutes:g} min"
    cc.reply(f":no_bell: *LN2 alarms silenced for {dur}* (until {until.strftime('%m-%d %H:%M')} "
             f"{tu.tz_abbrev()}).\nSay `resume LN2 alerts` to re-enable early.", color="#2196F3")
    log.info("LN2 alarms silenced for %s", dur)


def unsilence(cc: CommandContext) -> None:
    cc.st.setdefault("acked_sensors", {}).pop(ALL_KEY, None)
    cc.reply(":bell: *LN2 alarms re-enabled.*", color="#2196F3")


def status_text(cc: CommandContext) -> str:
    if not cc.ln2_ok():
        return f":warning: Cannot read LN2 database: {cc.ln2.last_error if cc.ln2 else 'not configured'}"
    row = cc.data.ln2_latest() if cc.cs2 is not None else None
    if row is None and cc.ln2 is not None:
        r = cc.ln2.one("SELECT time, temp, humidity FROM scale_readings ORDER BY time DESC LIMIT 1")
        if r:
            w = cc.ln2.one("SELECT weight FROM scale_readings WHERE weight >= 0 ORDER BY time DESC LIMIT 1")
            row = (r[0], w[0] if w else None, r[1], r[2])
    if row is None:
        return "No LN2 scale data yet."
    ts, weight, temp, hum = row
    u = config.LN2_WEIGHT_UNIT
    w1h = cc.ln2.one("SELECT weight FROM scale_readings WHERE weight >= 0 AND "
                     "time <= now() - interval '60 minutes' ORDER BY time DESC LIMIT 1")
    age = tu.age_minutes(ts) or 0
    fresh = "" if age <= config.LN2_DATA_STALE_MINUTES else \
        f"\n:sos: _data is {age:.0f} min old — scale may be offline_"
    trend = ""
    if w1h and weight is not None:
        d = weight - w1h[0]
        arrow = ":arrow_up:" if d > 0.001 else ":arrow_down:" if d < -0.001 else ":left_right_arrow:"
        trend = f"\n  • 1 h change: {arrow} `{d:+.2f} {u}`"
    dp = dew_point(temp, hum)
    dp_line = f"\n  • Dew point: `{dp:.1f} °C`" if dp is not None else ""
    wtxt = f"{weight:g} {u}" if weight is not None else "n/a"
    return (":balance_scale: *LN2 Scale — current reading*\n"
            f"  • Weight: `{wtxt}`\n  • Temperature: `{temp:g} °C`\n  • Humidity: `{hum:g} %`"
            f"{dp_line}{trend}\n_at {str(ts)[:19]}_{fresh}")


def handle(cc: CommandContext, text: str) -> None:
    """Route any LN2-flavoured request (status / plot / silence / resume)."""
    if UNSILENCE_RE.search(text):
        unsilence(cc)
        return
    if SILENCE_RE.search(text):
        silence(cc, units.parse_duration_minutes(text, 120))
        return
    if PLOT_RE.search(text):
        col, label, u = plot.ln2_quantity(text)
        plot.plot_ln2(cc, col, label, u, units.parse_duration_minutes(text, 120))
        return
    cc.reply(status_text(cc), color="#2196F3")
