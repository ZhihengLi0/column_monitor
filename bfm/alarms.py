"""Condition alarms and the engine that applies the shared rules to them.

A *condition alarm* is something that is either OK or in ALARM right now
(threshold exceeded, sensor stale, device fault, ...). Every one of them used
to re-implement the same 15 lines (mark active → check ack → check cooldown
→ record time → build message) with small, accidental differences. Now each
alarm class only *evaluates* and returns Verdicts; `AlarmEngine.apply()`
does the bookkeeping uniformly:

  ALARM   → mark active; skip if silenced; skip if within cooldown (unless
            `force`); record last_alert_time; enqueue message (kind="alarm",
            collapsed by key while Slack is down)
  OK      → drop cooldown; if the alarm was active, enqueue "✅ Cleared" to the
            channel and to the alarm's own thread (unless paused / silenced)
  UNKNOWN → leave everything as is (no data / not applicable)

Per-alarm hooks that the review identified as real behaviour:
  force=True             air-pressure escalation, device fault-set change
  reset_silence_on_clear FRESHNESS drops its reply-silence when data returns
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from . import config, modes
from .core import timeutil as tu
from .core import units

log = logging.getLogger("bfm.alarms")

OK, ALARM, UNKNOWN = "OK", "ALARM", "UNKNOWN"
SILENCE_HINT = "_React ✅ or reply `ok` / `silent for 2h` in thread to silence_"


@dataclass
class Verdict:
    key: str
    status: str                       # OK / ALARM / UNKNOWN
    label: str = ""                   # shown in active_alarms and "Cleared"
    message: str = ""                 # alarm text (ALARM only)
    clear_note: str | None = None     # "Cleared — <note>" (OK only); None → default
    color: str = "danger"
    force: bool = False               # bypass cooldown (escalation / changed fault set)
    reset_silence_on_clear: bool = False
    level: str = ""                   # informational: CRITICAL / WARNING


Enqueue = Callable[..., dict]


class AlarmEngine:
    """Applies verdicts to the state dict and queues Slack messages."""

    def __init__(self, st: dict, enqueue: Enqueue):
        self.st = st
        self.enqueue = enqueue
        self.cooldown = timedelta(minutes=config.ALERT_COOLDOWN_MINUTES)
        self.fired: list[str] = []      # keys enqueued this cycle (for logging)
        self.cleared: list[str] = []

    # ── state helpers ────────────────────────────────────────────────────────
    def is_silenced(self, key: str) -> bool:
        return tu.is_future(self.st.get("acked_sensors", {}).get(key))

    def in_cooldown(self, key: str) -> bool:
        last = tu.parse_iso(self.st.get("last_alert_time", {}).get(key))
        return last is not None and tu.now() - last < self.cooldown

    def mark_active(self, key: str, label: str) -> None:
        aa = self.st.setdefault("active_alarms", {})
        if key in aa and isinstance(aa[key], dict):
            aa[key]["label"] = label
        else:
            aa[key] = {"label": label, "ts": None}

    def clear(self, key: str, note: str | None = None) -> None:
        """'✅ Cleared' to channel + thread if the alarm was active."""
        self.st.setdefault("last_alert_time", {}).pop(key, None)
        info = self.st.setdefault("active_alarms", {}).pop(key, None)
        if info is None:
            return
        self.st.setdefault("pending_alert_msgs", {}).pop(key, None)
        self.cleared.append(key)
        if self.st.get("alerts_paused") or self.is_silenced(key):
            return
        label = info.get("label", key) if isinstance(info, dict) else str(info)
        ts = info.get("ts") if isinstance(info, dict) else None
        msg = f":white_check_mark: *Cleared* — {note or f'*{label}* is back to normal.'}"
        r = self.enqueue(msg, color="good", key=key, kind="clear")
        if ts and not r.get("dropped"):
            self.enqueue(msg, color="good", thread_ts=ts, kind="reply")
        log.info("Alarm cleared: %s", key)

    # ── the shared rule ──────────────────────────────────────────────────────
    def apply(self, v: Verdict) -> bool:
        """Returns True if an alarm message was enqueued."""
        if v.status == UNKNOWN:
            return False
        if v.status == OK:
            self.clear(v.key, v.clear_note)
            if v.reset_silence_on_clear:
                self.st.setdefault("acked_sensors", {}).pop(v.key, None)
            return False
        # ALARM
        self.mark_active(v.key, v.label)
        if self.is_silenced(v.key):
            return False
        if not v.force and self.in_cooldown(v.key):
            return False
        self.st.setdefault("last_alert_time", {})[v.key] = tu.now().isoformat()
        if self.st.get("alerts_paused"):
            return False          # paused: cooldown consumed, nothing posted (legacy)
        self.enqueue(v.message, color=v.color, key=v.key, kind="alarm")
        self.fired.append(v.key)
        return True

    def apply_all(self, verdicts: list[Verdict]) -> int:
        return sum(1 for v in verdicts if self.apply(v))


# ── helpers ──────────────────────────────────────────────────────────────────

def _mode_tag(st: dict) -> str:
    mode = st.get("current_mode", "")
    return f" _[{mode} mode]_" if mode else ""


def _age_min(ts) -> float | None:
    return tu.age_minutes(ts)


# ── condition alarms ─────────────────────────────────────────────────────────

class ConditionAlarm:
    """Base: subclasses implement evaluate(ctx, st) -> list[Verdict]."""
    name = "base"

    def evaluate(self, ctx, st: dict) -> list[Verdict]:   # pragma: no cover
        raise NotImplementedError


class Thresholds(ConditionAlarm):
    """Per-mode value limits (config.THRESHOLDS_*), with Slack overrides."""
    name = "thresholds"

    def evaluate(self, ctx, st):
        thresholds = modes.active_thresholds(st)
        out = []
        for name, entry in thresholds.items():
            row = ctx.latest(name)
            if row is None:
                continue
            value, ts = float(row[0]), row[1]
            max_v, min_v = modes.get_threshold(name, st)
            is_max = max_v is not None and value > max_v
            is_min = min_v is not None and value < min_v
            if not (is_max or is_min):
                out.append(Verdict(name, OK))
                continue
            desc = (entry[3] if is_min else entry[2]) if len(entry) >= 4 else entry[2]
            unit = units.BY_MAPPING[name].unit if name in units.BY_MAPPING else ""
            msg = (f":warning: *{desc}*{_mode_tag(st)}\n"
                   f"Current: `{value:.4g} {unit}` | Time: {ts}\n"
                   f"_React ✅ or reply `ok` in thread to silence 10 min_")
            out.append(Verdict(name, ALARM, desc, msg))
        return out


class Staleness(ConditionAlarm):
    """Per-sensor 'not updating' (all modes), with the IDLE flat limit, the
    out-of-range and pump-off exemptions and the global-outage guard."""
    name = "staleness"

    def evaluate(self, ctx, st):
        specs = config.STALENESS_SENSORS
        if not specs:
            return []
        latest = {mp: ctx.cs2.max_time(mp) for mp, *_ in specs}
        ages = [a for a in (_age_min(t) for t in latest.values()) if a is not None]
        if ages and min(ages) > config.STALENESS_GLOBAL_GUARD_MIN:
            return []            # whole pipeline down → FRESHNESS alarm covers it

        out_of_range = set()
        for dev, info in ctx.devices().items():
            if not dev.startswith("plc.IO.P"):
                continue
            js = ctx.device(dev) or {}
            if js.get("bWarningUnderrange") or js.get("bWarningOverrange"):
                out_of_range.add(f"{dev.split('.')[-1]}_PRESSURE")
        pump_off = set()
        for pump, mp in (("B1A_ENABLED", "B1A_TEMPERATURE"), ("B2_ENABLED", "B2_TEMPERATURE")):
            r = ctx.latest_bool(pump)
            if r is not None and not r[0]:
                pump_off.add(mp)

        warm = config.STALENESS_WARM_MINUTES if st.get("current_mode", "IDLE") == "IDLE" else None
        out = []
        for mp, label, base_min, confidence in specs:
            key = f"STALE::{mp}"
            if mp in out_of_range:
                out.append(Verdict(key, OK, clear_note=f"*{label}* is out of range (gauge OK)."))
                continue
            if mp in pump_off:
                out.append(Verdict(key, OK, clear_note=f"*{label}* pump is off (not monitored)."))
                continue
            max_min = warm if warm is not None else base_min
            age = _age_min(latest.get(mp))
            if age is not None and age <= max_min:
                out.append(Verdict(key, OK, clear_note=f"*{label}* is updating again."))
                continue
            mins = int(age) if age is not None else None
            if confidence == "maybe":
                detail = ("has no readings at all in the database" if age is None
                          else f"hasn't changed for {mins} min")
                msg = (f":grey_question: *{label} — no update for a while*\n"
                       f"`{mp}` {detail}. This *may* just mean the value is steady, "
                       f"or the sensor could be offline — worth a look.\n{SILENCE_HINT}")
            else:
                detail = ("no readings at all in the database" if age is None
                          else f"last reading {mins} min ago")
                msg = (f":sos: *Sensor not updating — {label}* :sos:\n"
                       f"`{mp}` {detail} (expected within {max_min} min).\n{SILENCE_HINT}")
            out.append(Verdict(key, ALARM, f"{label} readings stale", msg))
        return out


class Freshness(ConditionAlarm):
    """Whole-pipeline outage (sync computer off). One alarm, silenceable,
    auto-recovers and drops its silence when data returns."""
    name = "freshness"
    KEY = "FRESHNESS"

    def evaluate(self, ctx, st):
        latest = ctx.cs2.max_time()
        age = _age_min(latest) if latest is not None else 3650 * 1440
        if age <= config.FRESHNESS_MAX_MINUTES:
            return [Verdict(self.KEY, OK, reset_silence_on_clear=True,
                            clear_note="data sync has caught up (readings are current again).")]
        age_txt = ">3650 days (database empty)" if latest is None else f"{int(age)} min old"
        msg = (f":sos: *Data sync may have stopped!* "
               f"Latest reading is {age_txt}. The sync computer may be off/offline.\n"
               f"_Reply `silence 3 days` / `silent forever` in thread to mute — "
               f"it auto-resumes when data returns._")
        return [Verdict(self.KEY, ALARM, "Data sync stopped", msg)]


class DatabaseDown(ConditionAlarm):
    """The Pi's own PostgreSQL is unreachable (HDD not mounted, service down).
    Evaluated by the daemon with ctx=None when the connection fails."""
    name = "db_down"
    KEY = "DB_DOWN"

    def evaluate(self, ctx, st, error: str | None = None):
        if error is None:
            return [Verdict(self.KEY, OK, clear_note="*database reachable again* — monitoring resumed.")]
        msg = (f":sos: *BlueFors Monitor cannot reach its database*\n`{error}`\n"
               f"_Checks are paused until it is back; I'll repeat this every "
               f"{config.ALERT_COOLDOWN_MINUTES} min and post when it recovers._")
        return [Verdict(self.KEY, ALARM, "Database unreachable", msg)]


class Transitioning(ConditionAlarm):
    """During TRANSITIONING the pulse tube must stay ON (critical)."""
    name = "transitioning"
    REQUIRED = {"cool down": [("PULSE_TUBE_ENABLED", "Pulse Tube")],
                "warm up":   [("PULSE_TUBE_ENABLED", "Pulse Tube")]}

    def evaluate(self, ctx, st):
        if st.get("current_mode") != "TRANSITIONING":
            st.pop("transition_direction", None)
            return []
        direction = modes.transition_direction(ctx)
        st["transition_direction"] = direction
        required = self.REQUIRED[direction or "cool down"]
        dir_label = f" ({direction})" if direction else ""
        dir_verb = {"cool down": "cooling down", "warm up": "warming up"}.get(
            direction, "cooling down or warming up")
        out = []
        for mapping, label in required:
            row = ctx.latest_bool(mapping)
            if row is None:
                continue                                   # no data → unknown
            if row[0]:
                out.append(Verdict(mapping, OK, clear_note=f"{label} is back ON."))
                continue
            msg = (f":rotating_light: *CRITICAL — {label} is OFF during TRANSITIONING{dir_label}* :rotating_light:\n"
                   f"The {label.lower()} must stay *ON* while the fridge is {dir_verb}.\n"
                   f"Turned OFF at: {row[1]}\n{SILENCE_HINT}")
            out.append(Verdict(mapping, ALARM, f"{label} OFF during TRANSITIONING", msg, level="CRITICAL"))
        return out


class AirPressure(ConditionAlarm):
    """GHS compressed-air pressures; WARNING/CRITICAL; escalation bypasses cooldown."""
    name = "air_pressure"

    def evaluate(self, ctx, st):
        js = ctx.device(config.AIR_PRESSURE_DEVICE)
        if not js:
            return []
        levels = st.setdefault("air_pressure_level", {})
        out = []
        for field_, cfg in config.AIR_PRESSURE_ALARMS.items():
            val = js.get(field_)
            if val is None:
                continue
            crit, warn = cfg.get("crit_below"), cfg.get("warn_below")
            if crit is not None and val < crit:
                level = "CRITICAL"
            elif warn is not None and val < warn:
                level = "WARNING"
            else:
                levels.pop(field_, None)
                out.append(Verdict(field_, OK))
                continue
            escalated = level == "CRITICAL" and levels.get(field_) != "CRITICAL"
            levels[field_] = level
            scale, unit = cfg.get("scale", 1), cfg["unit"]
            thr = (crit if level == "CRITICAL" else warn) * scale
            emoji = ":rotating_light:" if level == "CRITICAL" else ":warning:"
            tail = " :rotating_light:" if level == "CRITICAL" else ""
            msg = (f"{emoji} *{level} — {cfg['label']} is LOW*{tail}\n"
                   f"Current: `{val * scale:g} {unit}` (normal ~{cfg['normal'] * scale:g} {unit}; "
                   f"{level.lower()} below {thr:g} {unit}).\n{SILENCE_HINT}")
            out.append(Verdict(field_, ALARM, cfg["label"] + " low", msg, force=escalated, level=level))
        return out


def humanize_flag(flag: str) -> str:
    """'bErrorOilRunningHigh' → 'Oil Running High'."""
    name = re.sub(r"^b(?:Error|Warning)", "", flag)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).strip()


def device_running(js: dict) -> bool | None:
    for k in ("bCompressorRunning", "bPumpOnOff", "bRunning"):
        if k in js:
            return bool(js[k])
    return None


def device_faults(js: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) from statusInfo + bError*/bWarning* flags; the
    operational flags only count while the device is running."""
    ignore = config.DEVICE_FLAG_IGNORE
    si = js.get("statusInfo") or {}
    errors = [str(e) for e in (si.get("errors") or [])]
    warnings = [str(w) for w in (si.get("warnings") or [])]
    if device_running(js) is not False:
        for k, v in js.items():
            if v is not True or any(sub in k.lower() for sub in ignore):
                continue
            if k.startswith("bError"):
                errors.append(humanize_flag(k))
            elif k.startswith("bWarning"):
                warnings.append(humanize_flag(k))
    if si.get("errorBit") and not errors:
        errors.append("error bit set")
    if si.get("warningBit") and not warnings:
        warnings.append("warning bit set")
    return sorted(set(errors)), sorted(set(warnings))


class DeviceHealth(ConditionAlarm):
    """Every device in device_states: errors → CRITICAL, warnings → WARNING;
    re-alerts (bypassing cooldown) when the fault set changes."""
    name = "device_health"

    def evaluate(self, ctx, st):
        prev = dict(st.get("device_health", {}))
        current = {}
        out = []
        for dev in sorted(ctx.devices()):
            js = ctx.device(dev)
            if not isinstance(js, dict):
                continue
            name = (js.get("instrumentInfo") or {}).get("name") or js.get("identifier") or dev
            errors, warnings = device_faults(js)
            for sev, items, emoji, level in (("error", errors, ":rotating_light:", "CRITICAL"),
                                             ("warning", warnings, ":warning:", "WARNING")):
                key = f"DEVICE::{dev}::{sev}"
                if not items:
                    out.append(Verdict(key, OK, clear_note=f"{name}: {sev}(s) resolved."))
                    continue
                current[key] = items
                changed = set(items) != set(prev.get(key, []))
                body = "\n".join(f"    • {x}" for x in items)
                tail = f" {emoji}" if level == "CRITICAL" else ""
                plural = "s" if len(items) > 1 else ""
                msg = (f"{emoji} *{level} — {name}: {sev}{plural}*{tail}\n{body}\n{SILENCE_HINT}")
                out.append(Verdict(key, ALARM, f"{name}: {sev}", msg, force=changed, level=level))
        st["device_health"] = current
        return out


def dew_point(temp_c, rh):
    """Magnus-Tetens dew point (°C); None for invalid inputs."""
    if temp_c is None or rh is None or rh <= 0:
        return None
    a, b = 17.62, 243.12
    gamma = math.log(rh / 100.0) + (a * temp_c) / (b + temp_c)
    return (b * gamma) / (a - gamma)


class CoolantDewpoint(ConditionAlarm):
    """Pulse-tube coolant-in must stay above the room dew point (from the LN2
    humidity sensor). Fail-safe: no reading → no alarm."""
    name = "dewpoint"
    KEY = "COOLANT_DEWPOINT"

    def evaluate(self, ctx, st):
        js = ctx.device(config.PT_DEVICE)
        if not js or js.get("fCoolantInTemp") is None:
            return []
        coolant_c = js["fCoolantInTemp"] - 273.15
        row = ctx.ln2_latest()
        if not row:
            return []
        _ts, _w, air_t, hum = row
        dp = dew_point(air_t, hum)
        if dp is None:
            return []
        if coolant_c >= dp:
            return [Verdict(self.KEY, OK, clear_note=(
                f"coolant-in `{coolant_c:.1f} °C` is back above the dew point `{dp:.1f} °C`."))]
        msg = (":rotating_light: *CRITICAL — coolant-in below dew point* :rotating_light:\n"
               f"Coolant-in temperature `{coolant_c:.1f} °C` is *below* the dew point "
               f"`{dp:.1f} °C` (air {air_t:.1f} °C / {hum:.0f}% RH).\n"
               "Condensation will form on the cooling-water line — raise the water "
               "temperature or lower the room humidity.\n" + SILENCE_HINT)
        return [Verdict(self.KEY, ALARM, "Coolant-in below dew point", msg, level="CRITICAL")]


class PulsetubeTemps(ConditionAlarm):
    """Coolant-in / oil temperature limits, only while the compressor runs."""
    name = "pt_temps"
    CHECKS = (("fCoolantInTemp", "PT_COOLANT_IN_HIGH_C", "PT_COOLANT_IN_HIGH",
               "Pulse-tube coolant-in temperature high"),
              ("fOilTemp", "PT_OIL_HIGH_C", "PT_OIL_HIGH", "Pulse-tube oil temperature high"))

    def evaluate(self, ctx, st):
        js = ctx.device(config.PT_DEVICE)
        if not js:
            return []
        running = bool(js.get("bCompressorRunning"))
        out = []
        for field_, cfg_name, key, label in self.CHECKS:
            limit = getattr(config, cfg_name, None)
            if limit is None:
                continue
            raw = js.get(field_)
            if not running or raw is None:
                note = f"*{label.rsplit(' ', 1)[0]}* — compressor off, not checked." if not running else None
                out.append(Verdict(key, OK, clear_note=note))
                continue
            temp_c = raw - 273.15
            if temp_c <= limit:
                out.append(Verdict(key, OK, clear_note=(
                    f"{label.lower()} back to normal (`{temp_c:.1f} °C` ≤ `{limit:g} °C`).")))
                continue
            msg = (f":rotating_light: *{label}* :rotating_light:\n"
                   f"Currently `{temp_c:.1f} °C` — above the `{limit:g} °C` limit.\n{SILENCE_HINT}")
            out.append(Verdict(key, ALARM, label, msg))
        return out


class ColdCathode(ConditionAlarm):
    """P1 cold-cathode gauge must be OFF in IDLE and ON in COLD."""
    name = "cold_cathode"
    KEY = "COLD_CATHODE"

    def evaluate(self, ctx, st):
        mode = st.get("current_mode")
        if mode not in ("IDLE", "COLD"):
            return []
        row = ctx.latest_bool("P1_ENABLED")
        if row is None:
            return []
        cc_on = bool(row[0])
        problem = (mode == "IDLE" and cc_on) or (mode == "COLD" and not cc_on)
        if not problem:
            return [Verdict(self.KEY, OK, clear_note="cold cathode (P1) state is back to normal.")]
        if mode == "IDLE":
            msg = (":warning: *Cold Cathode (P1) is ON at room temperature!*\n"
                   "System is in IDLE mode — cold cathode gauge should be OFF.")
        else:
            msg = (":warning: *Cold Cathode (P1) is OFF while fridge is COLD!*\n"
                   "System is in COLD mode — cold cathode gauge should be ON.")
        return [Verdict(self.KEY, ALARM, "Cold cathode (P1) wrong state", msg)]


class Milestones:
    """Informational cool-down milestones: one message on the downward crossing,
    re-armed after warming back above threshold + hysteresis. Not an alarm —
    returns plain event texts."""
    name = "milestones"

    def evaluate(self, ctx, st) -> list[str]:
        flags = st.setdefault("cooldown_milestones", {})
        out = []
        for mapping, below_k, label in config.COOLDOWN_MILESTONES:
            v = ctx.latest_value(mapping)
            if v is None:
                continue
            key = f"{mapping}<{below_k:g}"
            if key not in flags:
                flags[key] = v < below_k            # first sight → arm only
                continue
            if v < below_k and not flags[key]:
                flags[key] = True
                out.append(f":snowflake: *{label} is now below {below_k:g} K* — currently "
                           f"`{v:.2f} K`.\n_Cool-down milestone._")
            elif v >= below_k + config.COOLDOWN_MILESTONE_HYSTERESIS_K and flags[key]:
                flags[key] = False
        return out


# ── LN2 scale (merged from ln2_monitor/monitor.py) ───────────────────────────

class LN2(ConditionAlarm):
    """Liquid-nitrogen scale: stale data, weight low/critical, temperature,
    dew point. Keys are namespaced LN2::*; silence/resume commands set
    LN2::__all__ in acked_sensors."""
    name = "ln2"

    def evaluate(self, ctx, st):
        row = ctx.ln2_latest()
        if row is None:
            return []
        ts, weight, temp, hum = row
        u = config.LN2_WEIGHT_UNIT
        out = []
        age = _age_min(ts)
        if age is not None and age > config.LN2_DATA_STALE_MINUTES:
            out.append(Verdict("LN2::stale", ALARM, "LN2 scale data stale",
                               f":sos: *LN2 scale data stale* — no new reading for "
                               f"{age:.0f} min. Arduino/scale may be disconnected."))
            return out                      # don't trust thresholds on stale data
        out.append(Verdict("LN2::stale", OK, clear_note="LN2 scale is reporting again."))

        crit, low = config.LN2_WEIGHT_CRITICAL, config.LN2_WEIGHT_LOW
        if weight is not None and crit is not None and weight < crit:
            out.append(Verdict("LN2::weight_crit", ALARM, "LN2 weight very low",
                               f":rotating_light: *CRITICAL — LN2 weight very low* :rotating_light:\n"
                               f"Weight `{weight:g} {u}` (critical below {crit:g} {u}). "
                               "Refill liquid nitrogen now.", level="CRITICAL"))
        else:
            out.append(Verdict("LN2::weight_crit", OK))
        if weight is not None and low is not None and weight < low and not (crit is not None and weight < crit):
            out.append(Verdict("LN2::weight_low", ALARM, "LN2 weight low",
                               f":warning: *LN2 weight low* — `{weight:g} {u}` "
                               f"(below {low:g} {u}). Consider refilling.", color="warning"))
        else:
            out.append(Verdict("LN2::weight_low", OK))

        for key, cond, label, msg in (
            ("LN2::temp_high", temp is not None and config.LN2_TEMP_HIGH is not None and temp > config.LN2_TEMP_HIGH,
             "LN2 room temperature high",
             f":warning: *Temperature high* — `{temp:g} °C` (above {config.LN2_TEMP_HIGH or 0:g} °C)."),
            ("LN2::temp_low", temp is not None and config.LN2_TEMP_LOW is not None and temp < config.LN2_TEMP_LOW,
             "LN2 room temperature low",
             f":warning: *Temperature low* — `{temp:g} °C` (below {config.LN2_TEMP_LOW or 0:g} °C)."),
        ):
            out.append(Verdict(key, ALARM, label, msg, color="warning") if cond else Verdict(key, OK))

        dp = dew_point(temp, hum)
        if dp is not None and config.LN2_DEW_POINT_HIGH is not None and dp > config.LN2_DEW_POINT_HIGH:
            out.append(Verdict("LN2::dew_high", ALARM, "Dew point high",
                               f":warning: *Dew point high* — `{dp:.1f} °C` "
                               f"(above {config.LN2_DEW_POINT_HIGH:g} °C — condensation risk).",
                               color="warning"))
        else:
            out.append(Verdict("LN2::dew_high", OK))
        return out


# Evaluation order mirrors the legacy run(): freshness first (its guard
# suppresses the per-sensor flood), then the rest.
ALL_ALARMS: list[ConditionAlarm] = [
    Freshness(), Staleness(), Thresholds(), Transitioning(), AirPressure(),
    DeviceHealth(), CoolantDewpoint(), PulsetubeTemps(), ColdCathode(), LN2(),
]
