"""One plot renderer for every chart the bot produces.

Legacy had three copies of the matplotlib boilerplate (single sensor, multi
sensor, pulse tube) plus a fourth in ln2_query. All used a fixed UTC-5 axis
(one hour wrong in winter) and pyplot's global state. This uses Figure +
FigureCanvasAgg (safe in a long-lived process) and the real local timezone.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates                       # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure                   # noqa: E402
from matplotlib.ticker import ScalarFormatter          # noqa: E402

from .. import config
from ..core import timeutil as tu
from ..core import units
from ..alarms import dew_point
from .context import CommandContext

log = logging.getLogger("bfm.plot")

COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]
MAX_POINTS = 200_000          # hard cap per series (a year of 1 Hz data would be 31M)


@dataclass
class Series:
    label: str
    times: list
    values: list
    unit: str


def _out_dir() -> Path:
    for d in (config.RUNTIME_DIR, Path("/tmp/bluefors")):
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    return Path("/tmp")


def _range(minutes: float | None, start: datetime | None, end: datetime | None):
    """(t_from, t_to, range_label) — both aware UTC."""
    if start and end:
        tz = tu.tz_abbrev(end)
        label = f"{tu.fmt_local(start, '%Y-%m-%d %H:%M')} – {tu.fmt_local(end, '%Y-%m-%d %H:%M')} {tz}"
        return start, end, label
    t_to = tu.utcnow()
    return t_to - timedelta(minutes=minutes or 30), t_to, f"last {units.fmt_minutes(minutes or 30)}"


def render(series: list[Series], title: str, t_from: datetime, t_to: datetime,
           filename: str, sci_y: bool = False) -> Path:
    """Draw one or more series (dual y-axis when two units) and save a PNG."""
    duration_h = max((t_to - t_from).total_seconds() / 3600, 0.01)
    fig = Figure(figsize=(max(10, min(16, int(duration_h * 1.5))), 4))
    FigureCanvasAgg(fig)
    ax1 = fig.add_subplot(111)
    units_order = []
    for s in series:
        if s.unit not in units_order:
            units_order.append(s.unit)
    ax2 = ax1.twinx() if len(units_order) > 1 else None
    lines = []
    for i, s in enumerate(series):
        ax = ax1 if s.unit == units_order[0] else ax2
        line, = ax.plot(s.times, s.values, linewidth=1.0, color=COLORS[i % len(COLORS)], label=s.label)
        lines.append(line)
    ax1.set_ylabel(units_order[0])
    if ax2 is not None:
        ax2.set_ylabel(units_order[1])
    if len(series) > 1:
        ax1.legend(lines, [l.get_label() for l in lines], loc="upper left", fontsize=9)
    ax1.set_title(title, fontsize=12)
    ax1.set_xlabel(f"Time ({tu.tz_abbrev(t_to)})")
    fmt = "%H:%M" if duration_h <= 2 else "%m-%d %H:%M" if duration_h <= 48 else "%m-%d"
    ax1.xaxis.set_major_formatter(mdates.DateFormatter(fmt, tz=tu.LOCAL_TZ))
    ax1.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=30)
    ax1.grid(True, linestyle="--", alpha=0.5)
    if sci_y:
        ax1.yaxis.set_major_formatter(ScalarFormatter(useMathText=True))
        ax1.ticklabel_format(axis="y", style="sci", scilimits=(0, 0))
    fig.tight_layout()
    out = _out_dir() / filename
    fig.savefig(out, dpi=120, bbox_inches="tight")
    return out


def _send(cc: CommandContext, path: Path, title: str, ok_log: str) -> None:
    ok = cc.upload(path, title, thread_ts=cc.reply_ts)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    if ok:
        log.info(ok_log)
    else:
        cc.reply("Could not upload plot — Slack upload failed (network or missing "
                 "`files:write` scope).")


# ── fridge sensors ───────────────────────────────────────────────────────────

def _fetch(cc: CommandContext, sensor: units.Sensor, t_from, t_to) -> Series | None:
    rows = cc.cs2.series(sensor.mapping, t_from, t_to, "double", MAX_POINTS)
    if not rows:
        return None
    vals = [float(r[1]) for r in rows]
    unit = sensor.unit
    if sensor.kind == "pressure":
        vals, unit = [v * 1000 for v in vals], "mbar"
    elif unit == "K" and max(vals) < 1.0:
        vals, unit = [v * 1000 for v in vals], "mK"
    return Series(sensor.label, [r[0] for r in rows], vals, unit)


def plot_sensors(cc: CommandContext, sensors: list[units.Sensor], *, minutes: float | None = 30,
                 start: datetime | None = None, end: datetime | None = None) -> None:
    if not cc.db_ok("plot"):
        return
    t_from, t_to, range_label = _range(minutes, start, end)
    series = [s for s in (_fetch(cc, sn, t_from, t_to) for sn in sensors) if s]
    names = " + ".join(sn.short for sn in sensors)
    if not series:
        if len(sensors) == 1:
            cc.reply(f"No data for *{sensors[0].label}* ({range_label}).")
        else:
            cc.reply(f"No data for {names} ({range_label}).")
        return
    pts = sum(len(s.times) for s in series)
    if len(series) == 1:
        title = f"{series[0].label}  —  {range_label}  ({pts} points)"
        sci = sensors[0].kind == "pressure" and max(series[0].values) < 0.1
        slack_title = f"{series[0].label} — {range_label}"
    else:
        title = f"{names}  —  {range_label}  ({pts} pts)"
        sci = False
        slack_title = f"{names} — {range_label}"
    path = render(series, title, t_from, t_to, f"plot_{cc.reply_ts.replace('.', '_')}.png", sci)
    _send(cc, path, slack_title, f"Sent plot for {names} ({pts} points) [{range_label}]")


# ── pulse-tube recorded quantities ───────────────────────────────────────────
PT_QUANTITIES = [
    (r"coolant\s*[- ]?in",      "coolant_in",    "Coolant In",    "temp"),
    (r"coolant\s*[- ]?out",     "coolant_out",   "Coolant Out",   "temp"),
    (r"\boil\b",                "oil",           "Oil",           "temp"),
    (r"helium|\bhe\b",          "helium",        "Helium",        "temp"),
    (r"high\s*press",           "high_pressure", "High Pressure", "psi"),
    (r"low\s*press",            "low_pressure",  "Low Pressure",  "psi"),
    (r"motor\s*current|current", "motor_current", "Motor Current", "amp"),
]


def match_pulsetube_quantity(text: str):
    t = text.lower()
    for pat, col, label, kind in PT_QUANTITIES:
        if re.search(pat, t):
            return col, label, kind
    return None


def plot_pulsetube(cc: CommandContext, column: str, label: str, kind: str, minutes: float = 120) -> None:
    if not cc.db_ok("plot"):
        return
    rows = cc.cs2.query(
        f"SELECT time, {column} FROM {config.PULSETUBE_TABLE} "
        f"WHERE time >= now() - (%s || ' minutes')::interval AND {column} IS NOT NULL "
        f"ORDER BY time LIMIT %s", (minutes, MAX_POINTS))
    if not rows:
        cc.reply(f"No recorded pulse-tube data yet for *{label}* — recording just "
                 "started; try again in a few minutes.")
        return
    if kind == "temp":
        vals, unit = [r[1] - 273.15 for r in rows], "°C"
    elif kind == "psi":
        vals, unit = [units.pa_to_psi(r[1]) for r in rows], "psi"
    else:
        vals, unit = [r[1] for r in rows], "A"
    rng = units.fmt_minutes(minutes)
    t_to = tu.utcnow()
    path = render([Series(f"{label} ({unit})", [r[0] for r in rows], vals, unit)],
                  f"Pulse Tube {label} — last {rng} ({tu.tz_abbrev()})  ({len(rows)} points)",
                  t_to - timedelta(minutes=minutes), t_to, f"pt_{column}_{cc.reply_ts.replace('.', '_')}.png")
    _send(cc, path, f"Pulse Tube {label} — last {rng}", f"Sent pulse-tube plot: {column} ({len(rows)} pts)")


# ── LN2 scale ────────────────────────────────────────────────────────────────

def ln2_quantity(text: str):
    t = text.lower()
    if re.search(r"dew|露点", t):
        return "dewpoint", "Dew Point", "°C"
    if re.search(r"temp|温度", t):
        return "temp", "Temperature", "°C"
    if re.search(r"humid|湿度|潮", t):
        return "humidity", "Humidity", "%"
    return "weight", "Weight", config.LN2_WEIGHT_UNIT


def plot_ln2(cc: CommandContext, column: str, label: str, unit: str, minutes: float = 120) -> None:
    if not cc.ln2_ok():
        cc.reply(":warning: Cannot read LN2 database.")
        return
    if column == "dewpoint":
        rows = cc.ln2.query("SELECT time, temp, humidity FROM scale_readings "
                            "WHERE time >= now() - (%s || ' minutes')::interval ORDER BY time LIMIT %s",
                            (minutes, MAX_POINTS))
        rows = [(t, dew_point(tp, h)) for t, tp, h in rows]
        rows = [r for r in rows if r[1] is not None]
    else:
        extra = " AND weight >= 0" if column == "weight" else ""
        rows = cc.ln2.query(f"SELECT time, {column} FROM scale_readings "
                            f"WHERE time >= now() - (%s || ' minutes')::interval "
                            f"AND {column} IS NOT NULL{extra} ORDER BY time LIMIT %s", (minutes, MAX_POINTS))
    if not rows:
        cc.reply("No LN2 data in that time range.")
        return
    rng = units.fmt_minutes(minutes)
    t_to = tu.utcnow()
    path = render([Series(f"{label} ({unit})", [r[0] for r in rows], [r[1] for r in rows], unit)],
                  f"LN2 Scale {label} — last {rng} ({tu.tz_abbrev()})  ({len(rows)} points)",
                  t_to - timedelta(minutes=minutes), t_to, f"ln2_{column}_{cc.reply_ts.replace('.', '_')}.png")
    _send(cc, path, f"LN2 {label.lower()} — last {rng}", f"Sent LN2 plot: {column} ({len(rows)} pts)")
