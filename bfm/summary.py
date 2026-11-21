"""12-hour summary (08:00 / 20:00 local, or on demand via NLP 'daily summary').

Same content as the legacy generate_summary minus the dead code that read
the entire 340 MB monitor.log into an unused variable twice a day."""
from __future__ import annotations

from datetime import timedelta

import numpy as np

from .core import timeutil as tu
from .core import units
from .core.db import Database

TRACKED = [
    ("R1A_ENABLED", "R1A Pump"), ("R1A_ERROR_VALUE", "R1A Error"),
    ("HEATSWITCH_STILL_ENABLED", "Still Heat Switch"), ("HEATSWITCH_MXC_ENABLED", "MXC Heat Switch"),
    ("STILL_HEATER_ENABLED", "Still Heater"), ("MXC_HEATER_ENABLED", "MXC Heater"),
    ("PULSE_TUBE_ENABLED", "Pulse Tube"), ("P1_ENABLED", "Cold Cathode (P1)"),
    ("V112_ENABLED", "Valve V112"), ("V113_ENABLED", "Valve V113"), ("V114_ENABLED", "Valve V114"),
]
TEMPS = [("MXC_TEMPERATURE", "MXC"), ("STILL_TEMPERATURE", "Still"), ("4K_TEMPERATURE", "4K"), ("50K_TEMPERATURE", "50K")]
MODE_EMOJI = {"IDLE": ":white_circle:", "COLD": ":large_blue_circle:", "TRANSITIONING": ":large_yellow_circle:"}


def generate_summary(db: Database, st: dict, hours: int = 12) -> str:
    now_utc = tu.utcnow()
    since = now_utc - timedelta(hours=hours)
    tz = tu.tz_abbrev(now_utc)
    period = f"{tu.fmt_local(since, '%m-%d %H:%M')} – {tu.fmt_local(now_utc, '%m-%d %H:%M')} {tz}"
    lines = [f"*BlueFors {hours}-Hour Summary* | {tu.fmt_local(now_utc, '%Y-%m-%d %H:%M')} {tz}", f"_{period}_", ""]

    mode = st.get("current_mode", "unknown")
    lines.append(f"*Mode:* {MODE_EMOJI.get(mode, ':grey_question:')} {mode}")

    def latest(mapping):
        r = db.latest(mapping)
        return float(r[0]) if r else None

    lines.append("*Current readings:*")
    parts = []
    for mp, label in TEMPS:
        v = latest(mp)
        if v is not None:
            parts.append(f"{label}: {v * 1000:.2f} mK" if v < 1.0 else f"{label}: {v:.3f} K")
    if parts:
        lines.append("• Temp — " + "  |  ".join(parts))
    parts = []
    for i in range(1, 8):
        v = latest(f"P{i}_PRESSURE")
        if v is not None:
            parts.append(f"P{i}: {units.fmt_pressure(v)}")
    if parts:
        lines.append("• Pressure — " + "  |  ".join(parts))
    v = latest("FLOW_VALUE")
    if v is not None:
        lines.append(f"• Flow — {v:.3f} mmol/s")
    r = db.latest("PULSE_TUBE_ENABLED", "boolean")
    if r is not None:
        lines.append(f"• Pulse Tube — {'ON' if r[0] else 'OFF'}")
    lines.append("")

    changes = []
    for mp, label in TRACKED:
        for t, val in db.series(mp, since, now_utc, "boolean", 500):
            changes.append(f"• {label}: → *{'ON' if val else 'OFF'}* at {tu.fmt_local(t, '%H:%M')}")
    lines.append(f"*Device changes ({hours}h):*")
    lines.extend(changes or ["• No device state changes"])
    lines.append("")

    rows = db.query("SELECT code, title, severity, COUNT(*) AS cnt FROM public.alerts "
                    "WHERE datetime >= %s GROUP BY code, title, severity ORDER BY cnt DESC", (since,))
    lines.append(f"*CS2 alerts ({hours}h):*")
    if rows:
        for code, title, sev, cnt in rows:
            emoji = ":red_circle:" if sev >= 2 else ":large_yellow_circle:"
            lines.append(f"• {emoji} [{code}] {title}" + (f" ×{cnt}" if cnt > 1 else ""))
    else:
        lines.append("• No CS2 alerts")
    lines.append("")

    def trend(mapping):
        rows = db.series(mapping, since, now_utc, "double", 200_000)
        if len(rows) < 3:
            return None
        t0 = rows[0][0].timestamp()
        xs = np.array([(r[0].timestamp() - t0) / 3600 for r in rows])
        ys = np.array([float(r[1]) for r in rows])
        slope, _ = np.polyfit(xs, ys, 1)
        mean = np.mean(ys)
        if mean != 0 and abs(slope * hours) / abs(mean) < 0.01:
            return "➡️"
        return "📈" if slope > 0 else "📉"

    lines.append(f"*{hours}h linear trend:*")
    parts = [f"{label} {a}" for mp, label in TEMPS if (a := trend(mp))]
    if parts:
        lines.append("• Temp — " + "  |  ".join(parts))
    parts = [f"P{i} {a}" for i in range(1, 8) if (a := trend(f"P{i}_PRESSURE"))]
    if parts:
        lines.append("• Pressure — " + "  |  ".join(parts))
    a = trend("FLOW_VALUE")
    if a:
        lines.append(f"• Flow {a}")
    lines.append("")
    return "\n".join(lines)
