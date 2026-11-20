"""Units, durations and the sensor registry — ONE copy of each.

The legacy monitor.py had: bar→mbar formatting in 3 places, K→mK in 4,
K→°C in 5, Pa→psi in 2, four different duration parsers (silence, plot,
"change ... for", ln2) and six overlapping sensor lists. Everything below is
the merged, behaviour-preserving version; all of it is pure (no I/O).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── Sensor registry ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Sensor:
    mapping: str          # DB mapping name, e.g. "MXC_TEMPERATURE"
    short: str            # canonical short name used in commands/plots
    label: str            # human label for plots / readings
    unit: str             # DB-native unit: K / bar / mmol/s
    kind: str             # "temp" | "pressure" | "flow"
    mk: bool = False      # temperature usually shown in mK when < 1 K
    desc: str = ""        # one-line description for `list`


SENSORS: list[Sensor] = [
    Sensor("MXC_TEMPERATURE",     "MXC",   "MXC Temperature",        "K",   "temp", True,  "Mixing chamber temperature"),
    Sensor("MXC_TEMPERATURE_FAR", "MXC2",  "MXC2 (far) Temperature", "K",   "temp", True,  "Mixing chamber far-end temperature"),
    Sensor("STILL_TEMPERATURE",   "STILL", "Still Temperature",      "K",   "temp", True,  "Still temperature"),
    Sensor("4K_TEMPERATURE",      "4K",    "4K Plate Temperature",   "K",   "temp", False, "4K plate temperature"),
    Sensor("50K_TEMPERATURE",     "50K",   "50K Plate Temperature",  "K",   "temp", False, "50K plate temperature"),
    Sensor("B1A_TEMPERATURE",     "B1A",   "B1A Temperature",        "K",   "temp", False, "B1A stage temperature"),
    Sensor("B2_TEMPERATURE",      "B2",    "B2 Temperature",         "K",   "temp", False, "B2 stage temperature"),
    Sensor("P1_PRESSURE",         "P1",    "P1 Pressure",            "bar", "pressure", False, "P1 return pressure"),
    Sensor("P2_PRESSURE",         "P2",    "P2 Pressure",            "bar", "pressure", False, "P2 still pressure"),
    Sensor("P3_PRESSURE",         "P3",    "P3 Pressure",            "bar", "pressure"),
    Sensor("P4_PRESSURE",         "P4",    "P4 Pressure",            "bar", "pressure"),
    Sensor("P5_PRESSURE",         "P5",    "P5 Pressure",            "bar", "pressure", False, "P5 MXC pressure"),
    Sensor("P6_PRESSURE",         "P6",    "P6 Pressure",            "bar", "pressure"),
    Sensor("P7_PRESSURE",         "P7",    "P7 Pressure",            "bar", "pressure"),
    Sensor("FLOW_VALUE",          "FLOW",  "He Flow",                "mmol/s", "flow", False, "Helium flow rate"),
]
BY_MAPPING: dict[str, Sensor] = {s.mapping: s for s in SENSORS}
BY_SHORT: dict[str, Sensor] = {s.short: s for s in SENSORS}

# User-typed aliases → canonical short name (case-insensitive lookup).
_ALIASES = {
    "MC": "MXC", "MIXING": "MXC", "MXC1": "MXC", "MC1": "MXC",
    "MXCFAR": "MXC2", "MCFAR": "MXC2", "MC2": "MXC2",
    "FLOW_VALUE": "FLOW",
}

# Legacy `list` numbering (1..11 over the COLD list) and the `change`/`reset`
# command resolution used SENSOR_LOOKUP; preserved here.
_LEGACY_NUMBERED = ["MXC_TEMPERATURE", "MXC_TEMPERATURE_FAR", "STILL_TEMPERATURE",
                    "4K_TEMPERATURE", "50K_TEMPERATURE", "B1A_TEMPERATURE",
                    "B2_TEMPERATURE", "P1_PRESSURE", "P2_PRESSURE", "P5_PRESSURE",
                    "FLOW_VALUE"]
LIST_COLD = [BY_MAPPING[m] for m in _LEGACY_NUMBERED]
LIST_IDLE = [BY_MAPPING["P2_PRESSURE"], BY_MAPPING["P5_PRESSURE"]]

PRESSURE_MAPPINGS = [s.mapping for s in SENSORS if s.kind == "pressure"]
TEMP_MAPPINGS = [s.mapping for s in SENSORS if s.kind == "temp"]


def resolve_sensor(token: str) -> Sensor | None:
    """Short name, alias, mapping name, or legacy list number → Sensor."""
    t = (token or "").strip().upper()
    if not t:
        return None
    if t.isdigit():
        i = int(t)
        return LIST_COLD[i - 1] if 1 <= i <= len(LIST_COLD) else None
    t = _ALIASES.get(t, t)
    if t in BY_SHORT:
        return BY_SHORT[t]
    if t in BY_MAPPING:
        return BY_MAPPING[t]
    return None


# ── Unit conversions / formatting ─────────────────────────────────────────────

def fmt_pressure(val_bar: float) -> str:
    """bar → '12.3 mbar' / '4.5 μbar' (DB stores bar)."""
    mbar = val_bar * 1000
    if mbar >= 0.1:
        return f"{mbar:.4g} mbar"
    return f"{mbar * 1000:.3g} μbar"


def fmt_temperature(k: float, prefer_mk: bool = False) -> str:
    """K → '12.34 mK' or '4.123 K (-268.9 °C)'."""
    if prefer_mk and k < 1.0:
        return f"{k * 1000:.2f} mK"
    return f"{k:.4g} K ({k - 273.15:.1f} °C)"


def fmt_value(sensor: Sensor, value: float) -> str:
    if sensor.kind == "pressure":
        return fmt_pressure(value)
    if sensor.kind == "temp":
        return fmt_temperature(value, sensor.mk)
    return f"{value:.4g} {sensor.unit}"


def k_to_c(k: float) -> float:
    return k - 273.15


def pa_to_psi(pa: float) -> float:
    return pa / 6894.757


def fmt_threshold(sensor_mapping: str, value: float) -> str:
    """Threshold values in DB units → human text for alarm listings."""
    s = BY_MAPPING.get(sensor_mapping)
    if s is None:
        return f"{value:g}"
    return fmt_value(s, value)


# ── Durations ─────────────────────────────────────────────────────────────────
# One grammar for every command: number + unit, minute → year, English or
# Chinese. Longer unit words come first so "month"/"mo" beats bare "m".
DURATION_RE = re.compile(
    r"(?<![.\d])(\d+(?:\.\d+)?)\s*"
    r"(years?|yrs?|y|months?|mos?|mo|weeks?|wks?|w|days?|d|"
    r"hours?|hrs?|hr|h|minutes?|mins?|min|m|"
    r"年|个月|月|周|星期|天|日|小时|钟头|时|分钟|分)(?![A-Za-z])",
    re.IGNORECASE,
)
FOREVER_RE = re.compile(
    r"(ever|forever|permanent(?:ly)?|indefinit\w*|always|永久|永远|一直|长期)",
    re.IGNORECASE,
)

_UNIT_MINUTES = (
    (("y", "年"), 525_600, "year"),
    (("mo", "个月", "月"), 43_200, "month"),
    (("w", "周", "星期"), 10_080, "week"),
    (("d", "天", "日"), 1_440, "day"),
    (("h", "小时", "钟头", "时"), 60, "hour"),
)


def unit_to_minutes(unit: str) -> tuple[float, str]:
    """('h' → (60, 'hour')); anything unrecognised is minutes."""
    u = unit.lower()
    for prefixes, mins, word in _UNIT_MINUTES:
        for p in prefixes:
            if u.startswith(p):
                return mins, word
    return 1, "minute"


def parse_duration(text: str) -> tuple[float | None, str] | None:
    """Parse a duration phrase anywhere in `text`.

    Returns (minutes, label) e.g. (120.0, "2 hours");
            (None, "permanently") for forever-words;
            None when no duration is present.
    """
    if FOREVER_RE.search(text):
        return (None, "permanently")
    m = DURATION_RE.search(text)
    if not m:
        return None
    val = float(m.group(1))
    per, word = unit_to_minutes(m.group(2))
    return (val * per, f"{val:g} {word}{'' if val == 1 else 's'}")


def parse_duration_minutes(text: str, default: float) -> float:
    """Minutes from a phrase, or `default` if none (plot-style parsing)."""
    r = parse_duration(text)
    if r is None or r[0] is None:
        return default
    return r[0]


def fmt_minutes(m: float) -> str:
    """Range label: minutes → hours → days (used in plot titles/captions)."""
    if m < 60:
        return f"{m:g} min"
    if m < 1440:
        return f"{m / 60:g} h"
    d = m / 1440
    return f"{d:g} day" + ("" if d == 1 else "s")


def strip_duration(text: str) -> str:
    """Remove the duration token (used when the rest of the text is a sensor list)."""
    return DURATION_RE.sub(" ", text)
