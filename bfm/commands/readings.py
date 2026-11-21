"""Live-reading commands: pressure, temperature, pump, heater, pulse tube, valves."""
from __future__ import annotations

import logging

from .. import config
from ..alarms import device_faults
from ..core import units
from ..events import HEATER_MAPPINGS
from .context import CommandContext

log = logging.getLogger("bfm.commands")

VALVES = ["V001", "V003", "V004", "V005",
          "V101", "V102", "V104", "V105", "V106", "V107", "V108",
          "V110", "V111", "V112", "V113", "V114",
          "V201G", "V202", "V203",
          "V401", "V402", "V403",
          "V501H", "V502H", "V503H"]

TEMP_ROWS = [("MXC_TEMPERATURE", "MXC1", True), ("MXC_TEMPERATURE_FAR", "MXC2", True),
             ("STILL_TEMPERATURE", "Still", True), ("4K_TEMPERATURE", "4K", False),
             ("50K_TEMPERATURE", "50K", False), ("B1A_TEMPERATURE", "B1A", False),
             ("B2_TEMPERATURE", "B2", False)]


def cmd_pressure(cc: CommandContext) -> None:
    if not cc.db_ok("read pressures"):
        return
    lines = [":compression: *Current Pressure Readings*\n"]
    for mp in units.PRESSURE_MAPPINGS:
        label = units.BY_MAPPING[mp].short
        row = cc.data.latest(mp)
        if row:
            lines.append(f"  *{label}*: `{units.fmt_pressure(float(row[0]))}`  _(at {str(row[1])[:19]})_")
        else:
            lines.append(f"  *{label}*: _no data_")
    cc_row = cc.data.latest_bool("P1_ENABLED")
    if cc_row is not None:
        cc_str = ":large_green_circle: ON" if bool(cc_row[0]) else ":white_circle: OFF"
        lines.append(f"\n  *Cold Cathode (P1)*: {cc_str}")
    cc.reply("\n".join(lines))
    log.info("Sent pressure reading reply to Slack")


def cmd_temperature(cc: CommandContext) -> None:
    if not cc.db_ok("read temperatures"):
        return
    lines = [":thermometer: *Current Temperature Readings*\n"]
    for mp, label, prefer_mk in TEMP_ROWS:
        row = cc.data.latest(mp)
        if not row:
            lines.append(f"  *{label}*: _no data_")
            continue
        v, ts = float(row[0]), str(row[1])[:19]
        if prefer_mk and v < 1.0:
            lines.append(f"  *{label}*: `{v * 1000:.2f} mK`  _(at {ts})_")
        else:
            lines.append(f"  *{label}*: `{v:.4g} K` (`{v - 273.15:.1f} °C`)  _(at {ts})_")
    cc.reply("\n".join(lines), color="#2196F3")
    log.info("Sent temperature reading reply to Slack")


def cmd_pump_status(cc: CommandContext, highlight: str | None = None) -> None:
    if not cc.db_ok("read pump status"):
        return
    d = cc.data

    def lb(mp):
        r = d.latest_bool(mp)
        return bool(r[0]) if r else None

    def ld(mp):
        r = d.latest(mp)
        return float(r[0]) if r else None

    def li(mp):
        r = cc.cs2.latest(mp, "int")
        return int(r[0]) if r else None

    def icon(enabled, error):
        return ":warning:" if error else ":large_green_circle:" if enabled else ":white_circle:"

    hl = (highlight or "").upper().replace("TURBO", "B1A")
    lines = [f":gear: *Pump Status — {highlight}*\n" if hl else ":gear: *Pump Status*\n"]

    def fmt_turbo(name):
        enabled, error = lb(f"{name}_ENABLED"), lb(f"{name}_ERROR_VALUE")
        temp, power, speed = ld(f"{name}_TEMPERATURE"), li(f"{name}_POWER"), li(f"{name}_SPEED")
        details = []
        if speed is not None: details.append(f"speed {speed}%")
        if power is not None: details.append(f"power {power}%")
        if temp is not None: details.append(f"temp {temp:.1f} K")
        detail = "  |  " + ",  ".join(details) if details else ""
        return (f"{icon(enabled, error)} *{name}* (turbo): *{'ON' if enabled else 'OFF'}*"
                f"{'  :warning: ERROR' if error else ''}{detail}{' ◄' if hl == name else ''}")

    def fmt_scroll(name):
        enabled, error = lb(f"{name}_ENABLED"), lb(f"{name}_ERROR_VALUE")
        power = ld(f"{name}_PUMP_POWER")
        pw = f"  |  power {power:.2f} W" if power is not None else ""
        return (f"{icon(enabled, error)} *{name}* (scroll): *{'ON' if enabled else 'OFF'}*"
                f"{'  :warning: ERROR' if error else ''}{pw}{' ◄' if hl == name else ''}")

    devices = {"B1A": lambda: fmt_turbo("B1A"), "B2": lambda: fmt_turbo("B2"),
               "R1A": lambda: fmt_scroll("R1A"), "R2": lambda: fmt_scroll("R2")}
    if hl and hl in devices:
        lines.append(devices[hl]())
        lines.append("_Other pumps:_")
        lines.extend(fn() for n, fn in devices.items() if n != hl)
    else:
        lines.extend([fmt_turbo("B1A"), fmt_turbo("B2"), "", fmt_scroll("R1A"), fmt_scroll("R2")])
    lines.append("")
    enabled, error, power = lb("COM_ENABLED"), lb("COM_ERROR_VALUE"), ld("COM_PUMP_POWER")
    pw = f"  |  power {power:.2f} W" if power is not None else ""
    lines.append(f"{icon(enabled, error)} *COM* (compressor): *{'ON' if enabled else 'OFF'}*"
                 f"{'  :warning: ERROR' if error else ''}{pw}{' ◄' if hl == 'COM' else ''}")
    cc.reply("\n".join(lines))
    log.info("Sent pump status reply to Slack%s", f" [highlight={hl}]" if hl else "")


def cmd_heater_status(cc: CommandContext) -> None:
    if not cc.db_ok("read heater status"):
        return
    lines = [":fire: *Heater Status*\n"]
    for mp, label in HEATER_MAPPINGS:
        r = cc.data.latest_bool(mp)
        if r is None:
            lines.append(f"  *{label}*: _no data_")
            continue
        val = bool(r[0])
        lines.append(f"{':large_green_circle:' if val else ':white_circle:'} *{label}*: "
                     f"{'*ON*' if val else '*OFF*'}  _(updated {str(r[1])[:19]})_")
    lines.append("")
    for mp, label in (("STILL_HEATING_POWER", "Still Heater power"), ("MXC_HEATING_POWER", "MXC Heater power")):
        r = cc.data.latest(mp)
        if r is not None:
            lines.append(f"  *{label}*: `{float(r[0]) * 1000:.2f} mW`")
    cc.reply("\n".join(lines), color="#cc6600")
    log.info("Sent heater status reply to Slack")


def cmd_pulsetube_status(cc: CommandContext) -> None:
    if not cc.db_ok("read pulse tube status"):
        return
    js = cc.data.device(config.PT_DEVICE)
    if not js:
        cc.reply("No pulse tube data available.")
        return
    running = bool(js.get("bCompressorRunning"))

    def C(f):
        v = js.get(f); return f"{v - 273.15:.1f} °C" if v is not None else "—"

    def psi(f):
        v = js.get(f); return f"{units.pa_to_psi(v):.1f} psi" if v is not None else "—"

    def A(f):
        v = js.get(f); return f"{v:.1f} A" if v is not None else "—"

    hours = js.get("fHoursOfOperation")
    lines = [":cyclone: *Pulse Tube Compressor*\n",
             f"{':large_green_circle:' if running else ':red_circle:'} *{'RUNNING' if running else 'OFF'}*"
             + (f"   ·   {hours:.0f} h total operation" if hours is not None else ""),
             "", "*Temperatures:*",
             f"  • Coolant In: `{C('fCoolantInTemp')}`    Coolant Out: `{C('fCoolantOutTemp')}`",
             f"  • Oil: `{C('fOilTemp')}`    Helium: `{C('fHeliumTemp')}`",
             "*Pressures:*",
             f"  • High: `{psi('fHighPressure')}`    Low: `{psi('fLowPressure')}`",
             "*Motor:*", f"  • Current: `{A('fMotorCurrent')}`"]
    errors, warnings = device_faults(js)
    if errors:
        lines.append("\n:rotating_light: *Faults (critical):* " + ", ".join(errors))
    if warnings:
        lines.append(":warning: *Warnings:* " + ", ".join(warnings))
    if not errors and not warnings:
        lines.append(f"\n:white_check_mark: No active faults "
                     f"({'within factory limits' if running else 'compressor off — limits not checked'}).")
    color = "good" if (running and not errors and not warnings) else ("danger" if errors else "#cc6600")
    cc.reply("\n".join(lines), color=color)
    log.info("Sent pulse tube status reply to Slack")


def cmd_valve_status(cc: CommandContext, highlight: str | None = None) -> None:
    if not cc.db_ok("read valve status"):
        return
    hl = (highlight or "").upper()
    lines = [f":valve: *Valve Status — {hl}*\n" if hl else ":valve: *Valve Status*\n"]
    open_v, closed_v, hl_lines = [], [], []
    for v in VALVES:
        r = cc.data.latest_bool(f"{v}_ENABLED")
        if r is None:
            continue
        is_open, ts = bool(r[0]), str(r[1])[:16]
        if v == hl:
            hl_lines.append(f"{':large_green_circle:' if is_open else ':red_circle:'} *{v}* — "
                            f"*{'OPEN' if is_open else 'CLOSED'}*  _(at {ts})_ ◄")
        elif is_open:
            open_v.append(f":large_green_circle: *{v}* — OPEN  _(at {ts})_")
        else:
            closed_v.append(f":white_circle: {v} — closed")
    if hl_lines:
        lines.extend(hl_lines + ["", "_All valves:_"])
    if open_v:
        lines.extend(["*Open:*"] + open_v + [""])
    lines.extend(["*Closed:*"] + closed_v)
    cc.reply("\n".join(lines), color="#2E86AB")
    log.info("Sent valve status reply to Slack%s", f" [highlight={hl}]" if hl else "")
