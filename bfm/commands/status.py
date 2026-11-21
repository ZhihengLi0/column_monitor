"""Informational commands: help, list, status, alarm listing."""
from __future__ import annotations

from .. import config
from ..alarms import Transitioning
from ..core import timeutil as tu
from ..core import units
from ..events import DEVICE_ALERT_MAPPINGS, MONITORED_VALVES
from ..modes import MODE_DESC, MODE_EMOJI, override_key
from .context import CommandContext

HELP_TEXT = (
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
    "`plot coolant in 2h` — plot recorded pulse-tube values (coolant in/out, oil, helium, high/low pressure, motor current)\n"
    "\n*Liquid nitrogen scale* (natural language OK):\n"
    "`ln2` / `how much liquid nitrogen` / `液氮还剩多少` — current weight, temp, humidity + 1 h trend\n"
    "`plot ln2 weight 6h` / `液氮曲线 2h` — weight-vs-time chart (min/hour/day)\n"
    "`silence ln2 2h` / `resume ln2 alerts` — mute / unmute the LN2 scale alarms\n\n"
    "`plot <sensor>` — plot last 30 min of data as image (e.g. `plot P1`, `plot MXC`, `plot MXC2`)\n"
    "`plot <sensor> <N>min` — plot last N minutes (e.g. `plot P5 60min`, `plot P2 P5 2h`)\n"
    "`plot <sensor> YYMMDD_HHMM YYMMDD_HHMM` — plot a custom local-time range (e.g. `plot P1 260622_0000 260622_1200`)\n"
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
    "_<sensor> = number (see `list`), short name, or full mapping name_"
)


def cmd_help(cc: CommandContext) -> None:
    cc.reply(HELP_TEXT)


def _threshold_lines(thresholds: dict) -> list[str]:
    out = []
    for _full, entry in thresholds.items():
        max_v, min_v = entry[0], entry[1]
        if len(entry) >= 4:
            if max_v is not None and entry[2]:
                out.append(f"  • {entry[2]}")
            if min_v is not None and entry[3]:
                out.append(f"  • {entry[3]}")
        elif entry[2]:
            out.append(f"  • {entry[2]}")
    return out


def cmd_list_alarms(cc: CommandContext, only_mode: str | None = None) -> None:
    st = cc.st
    cur_mode = st.get("current_mode", "IDLE")
    header = ":rotating_light: *Configured Alarms — what triggers each one*"
    if only_mode:
        header += f"\n_{MODE_EMOJI.get(only_mode, '')} {only_mode} mode only_"
    lines = [header, ""]

    def section(mode):
        star = "   ← *current*" if mode == cur_mode else ""
        lines.append(f"*{MODE_EMOJI.get(mode, '')} {mode} mode — alarm triggers when:*{star}")
        if mode == "COLD":
            body = _threshold_lines(config.THRESHOLDS_COLD)
            body.append("  • Cold cathode (P1) is OFF (should be ON while cold)")
        elif mode == "IDLE":
            body = _threshold_lines(config.THRESHOLDS_IDLE)
            body.append("  • Cold cathode (P1) is ON (should be OFF at room temperature)")
        else:
            body = ["  _Sensor threshold alarms are suppressed while cooling/warming._",
                    "  Direction is auto-detected from the 50K plate trend."]
            for direction, devices in Transitioning.REQUIRED.items():
                body.append(f"  *{direction.title()}:* CRITICAL if OFF → {', '.join(l for _, l in devices)}")
        lines.extend(body or ["  _(none)_"])
        lines.append("")

    for md in ([only_mode] if only_mode else ["IDLE", "TRANSITIONING", "COLD"]):
        section(md)

    stale_min = min((s[2] for s in config.STALENESS_SENSORS), default=5)
    lines.append("*Always on (every mode) — alarm triggers when:*")
    lines.append(f"  • *Any device switches ON or OFF* — {', '.join(l for _, l in DEVICE_ALERT_MAPPINGS)}")
    lines.append("  • *R1A pump* — enable toggles, error flag set/cleared, or pump power crosses 0.1 W (stop/restart)")
    lines.append(f"  • *Valves* — {' / '.join(l for _, l in MONITORED_VALVES)} open or close")
    lines.append(f"  • *CS2 system alert* — new entry with severity ≥ {config.CS2_ALERT_MIN_SEVERITY} (error)")
    lines.append(f"  • *Data sync stalled* — no new sensor reading (any sensor) for > {config.FRESHNESS_MAX_MINUTES} min")
    lines.append(f"  • *Sensor not updating* — any single thermometer/gauge with no new reading for "
                 f"> {stale_min} min (temps) / 60 min (pressures); flat {config.STALENESS_WARM_MINUTES // 60} h "
                 "limit while IDLE")
    for cfg in config.AIR_PRESSURE_ALARMS.values():
        u, s = cfg["unit"], cfg.get("scale", 1)
        parts = []
        if cfg.get("warn_below") is not None:
            parts.append(f"warning < {cfg['warn_below'] * s:g}")
        if cfg.get("crit_below") is not None:
            parts.append(f"*CRITICAL* < {cfg['crit_below'] * s:g}")
        lines.append(f"  • *{cfg['label']}* low — {', '.join(parts)} {u} (normal ~{cfg['normal'] * s:g} {u})")
    lines.append("  • *Pulse-tube compressor* (checked only while running) — each of "
                 "*coolant-in, coolant-out, oil, helium temperature; high & low pressure; "
                 "motor current* has factory CRITICAL and WARNING limits (Cryomech built-in "
                 "fault flags): error → *CRITICAL*, warning → WARNING")
    if config.PT_COOLANT_IN_HIGH_C is not None or config.PT_OIL_HIGH_C is not None:
        lines.append(f"  • *Pulse-tube coolant-in > {config.PT_COOLANT_IN_HIGH_C:g} °C* or "
                     f"*oil > {config.PT_OIL_HIGH_C:g} °C* (while running)")
    lines.append("  • *Any other device fault* — every device is checked; an error → *CRITICAL*, "
                 "a warning → WARNING (helium compressor, pumps, pressure gauges, valves, GHS, …)")
    lines.append("  • *Coolant-in below dew point* → *CRITICAL* — pulse-tube coolant-in temperature "
                 "must stay above the dew point (from the LN2 humidity sensor) or the cooling line condenses")
    lines.append(f"  • *LN2 scale* — no reading for > {config.LN2_DATA_STALE_MINUTES} min; "
                 f"weight below {config.LN2_WEIGHT_LOW} {config.LN2_WEIGHT_UNIT}")
    lines.append(f"\n_Same alarm repeats at most every {config.ALERT_COOLDOWN_MINUTES} min "
                 "(cooldown). Reply `silent for 2h` under an alert, or `ack` to hush all._")
    lines.append("_`what alarms in cold mode` for one mode · `what is the alarm` for everything · "
                 "`list` for exact threshold numbers_")
    cc.reply("\n".join(lines), color="#cc0000")


def cmd_list(cc: CommandContext) -> None:
    st = cc.st
    mode = st.get("current_mode", "IDLE")
    lines = [f"*Sensor List — current mode: {MODE_EMOJI.get(mode, '')} {mode}*\n", "*COLD mode thresholds:*"]
    ov_cold = st.get(override_key("COLD"), {})
    ov_idle = st.get(override_key("IDLE"), {})
    for i, s in enumerate(units.LIST_COLD, 1):
        entry = config.THRESHOLDS_COLD.get(s.mapping, (None, None, ""))
        thr = f"> `{entry[0]} {s.unit}`" if entry[0] is not None else f"< `{entry[1]} {s.unit}`"
        ov = " *(overridden)*" if s.mapping in ov_cold else ""
        lines.append(f"  `{i:2d}` `{s.short:<7}` {s.desc} — alert if {thr}{ov}")
    lines.append("\n*IDLE mode thresholds (room temperature):*")
    for s in units.LIST_IDLE:
        entry = config.THRESHOLDS_IDLE.get(s.mapping, (None, None, ""))
        thr = f"> `{entry[0]} {s.unit}`" if entry[0] is not None else f"< `{entry[1]} {s.unit}`"
        ov = " *(overridden)*" if s.mapping in ov_idle else ""
        lines.append(f"  `{s.short:<7}` {s.desc} — alert if {thr}{ov}")
    lines.append("\n_Example: `@BlueFors-Alert change 1 to 0.05 for 5min`_\n"
                 "_Example: `@BlueFors-Alert set mode cold`_")
    cc.reply("\n".join(lines))


def cmd_status(cc: CommandContext) -> None:
    st = cc.st
    mode = st.get("current_mode", "unknown")
    since = (st.get("mode_since") or "")[:16]
    mode_str = f"{MODE_EMOJI.get(mode, '')} `{mode}`"
    if mode == "TRANSITIONING" and st.get("transition_direction"):
        mode_str += f" — *{st['transition_direction']}*"
    lines = ["*Monitor Status*\n",
             f"*Mode:* {mode_str}" + (" *(manually set)*" if st.get("mode_override") else " *(auto-detected)*"),
             f"*Since:* {since}",
             f"\n_{MODE_DESC.get(mode, '')}_"]
    if st.get("alerts_paused"):
        lines.append("\n:no_bell: *All alerts are PAUSED* (`resume alerts` to re-enable).")
    if not st.get("cs2_alerts_enabled", True):
        lines.append(":white_circle: Sentinel (CS2) alert forwarding is OFF.")
    overrides = st.get(override_key(mode), {})
    if overrides:
        lines.append(f"\n*Active threshold overrides ({mode}):*")
        for sensor, ov in overrides.items():
            val = ov.get("max_val") if ov.get("max_val") is not None else ov.get("min_val")
            exp = ov.get("expires_at")
            unit = units.BY_MAPPING[sensor].unit if sensor in units.BY_MAPPING else ""
            lines.append(f"  • `{sensor}`: `{val} {unit}` ({'permanent' if exp is None else 'until ' + exp[:16]})")
    active = {s: t for s, t in st.get("acked_sensors", {}).items() if tu.is_future(t)}
    if active:
        lines.append("\n*Silenced sensors:*")
        for s, until in active.items():
            lines.append(f"  • `{s}` " + ("permanently" if until.startswith("9999") else f"until `{until[:16]}`"))
    alarms = st.get("active_alarms", {})
    if alarms:
        lines.append("\n*Active alarms:*")
        for k, info in alarms.items():
            lines.append(f"  • `{k}` — {info.get('label', '') if isinstance(info, dict) else info}")
    sl = st.get("slack", {})
    if sl.get("down_since"):
        lines.append(f"\n:warning: Slack has been unreachable since {sl['down_since'][:16]}; "
                     f"{len(st.get('outbox', []))} message(s) queued.")
    cc.reply("\n".join(lines))
