"""Side-effect writers that run inside the check cycle: pulse-tube analog
recording (for `plot coolant in`) and its retention cleanup."""
from __future__ import annotations

import logging
from datetime import timedelta

from . import config
from .core import timeutil as tu

log = logging.getLogger("bfm.recording")


def record_pulsetube(ctx, dry_run: bool = False) -> bool:
    """Append the compressor's analog values to pulsetube_readings. Returns
    True if a row was written (or would have been, in dry-run)."""
    js = ctx.device(config.PT_DEVICE)
    if not js:
        return False
    vals = (js.get("fCoolantInTemp"), js.get("fCoolantOutTemp"), js.get("fOilTemp"),
            js.get("fHeliumTemp"), js.get("fHighPressure"), js.get("fLowPressure"),
            js.get("fMotorCurrent"), bool(js.get("bCompressorRunning")))
    if all(v is None for v in vals[:7]):
        return False
    if dry_run:
        return True
    try:
        ctx.cs2.execute(
            f"INSERT INTO {config.PULSETUBE_TABLE} (coolant_in, coolant_out, oil, helium, "
            "high_pressure, low_pressure, motor_current, running) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", vals)
        return True
    except Exception as e:
        log.error("pulsetube record failed: %s", e)
        return False


def prune_pulsetube(db, st: dict, dry_run: bool = False) -> None:
    """Once a day, delete pulsetube_readings older than PULSETUBE_RETENTION_DAYS."""
    last = tu.parse_iso(st.get("last_pt_prune"))
    if last and tu.now() - last < timedelta(hours=24):
        return
    st["last_pt_prune"] = tu.now().isoformat()
    if dry_run:
        return
    try:
        db.execute(f"DELETE FROM {config.PULSETUBE_TABLE} WHERE time < now() - interval '%s days'",
                   (config.PULSETUBE_RETENTION_DAYS,))
        log.info("pruned pulsetube_readings older than %d days", config.PULSETUBE_RETENTION_DAYS)
    except Exception as e:
        log.error("pulsetube prune failed: %s", e)
