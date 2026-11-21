#!/usr/bin/env python3
"""Compare the legacy decision recorder (tools/legacy_decisions.py, cron every
minute) with the shadow daemon's decision records (bfm.cli shadow).

    python3 tools/shadow_diff.py [--since 2026-11-20T18:00]

Both streams are JSON lines with a per-cycle 'ts'. Records are paired by
minute. For each pair we compare:
  * mode
  * the set of alarm keys that would fire ("fired" vs legacy "alerts" keys)
  * the set of keys cleared
  * event-message counts (legacy: unkeyed alerts)
  * event cursors
Expected/accepted differences are filtered out and listed separately:
  * legacy fires again every 30 min for the same key while the daemon's
    cooldown is aligned differently (a key present in one stream's 'fired'
    but ACTIVE in both at that minute) → "cooldown phase"
  * the first daemon cycle after start (cursor/arm differences)
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

LEGACY = Path("/mnt/harddrive/pi/logs/legacy_decisions.jsonl")
SHADOW = Path("/mnt/harddrive/pi/logs/shadow_decisions.jsonl")


def load(path: Path, since: str | None) -> dict[str, dict]:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts = r.get("ts", "")
        if since and ts < since:
            continue
        out[ts[:16]] = r                      # key by minute
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    args = ap.parse_args()
    legacy, shadow = load(LEGACY, args.since), load(SHADOW, args.since)
    minutes = sorted(set(legacy) & set(shadow))
    print(f"legacy records: {len(legacy)}  shadow records: {len(shadow)}  paired minutes: {len(minutes)}")
    if not minutes:
        return
    real, accepted = [], Counter()

    def neighbours(src: dict, m: str) -> list[dict]:
        """Records of the other stream at m-1 and m+1 (the two recorders sample
        ~40 s apart, so a change that straddles a minute boundary shows up one
        minute earlier in one of them)."""
        from datetime import datetime, timedelta
        t = datetime.strptime(m, "%Y-%m-%dT%H:%M")
        return [src[k] for k in ((t - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"),
                                 (t + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")) if k in src]

    def l_fired_of(r): return {k for k, _ in r.get("alerts", []) if k}

    for m in minutes:
        L, S = legacy[m], shadow[m]
        l_fired = l_fired_of(L)
        l_events = sum(1 for k, _ in L.get("alerts", []) if not k)
        s_fired, s_events = set(S.get("fired", [])), S.get("events", 0)
        l_active, s_active = set(L.get("active", [])), set(S.get("active", []))
        diffs = []
        if L.get("mode") != S.get("mode"):
            diffs.append(f"mode {L.get('mode')} vs {S.get('mode')}")
        only_l, only_s = l_fired - s_fired, s_fired - l_fired
        for k in list(only_l):
            if k in s_active:                    # same alarm active, different cooldown phase
                accepted["cooldown phase"] += 1
                only_l.discard(k)
            elif any(k in set(n.get("fired", [])) for n in neighbours(shadow, m)):
                accepted["minute skew"] += 1
                only_l.discard(k)
            elif any(k in set(n.get("cleared", [])) for n in [S] + neighbours(shadow, m)):
                accepted["fault vanished between the two samples"] += 1   # legacy re-fired, 40 s later it was gone
                only_l.discard(k)
        for k in list(only_s):
            if k in l_active:
                accepted["cooldown phase"] += 1
                only_s.discard(k)
            elif any(k in l_fired_of(n) for n in neighbours(legacy, m)):
                accepted["minute skew"] += 1
                only_s.discard(k)
            elif any(k in set(n.get("cleared", [])) for n in [L] + neighbours(legacy, m)):
                accepted["fault vanished between the two samples"] += 1
                only_s.discard(k)
        if only_l or only_s:
            diffs.append(f"fired legacy-only={sorted(only_l)} shadow-only={sorted(only_s)}")
        l_cl, s_cl = set(L.get("cleared", [])), set(S.get("cleared", []))
        for k in list(l_cl - s_cl):
            if any(k in set(n.get("cleared", [])) for n in neighbours(shadow, m)):
                accepted["minute skew"] += 1; l_cl.discard(k)
        for k in list(s_cl - l_cl):
            if any(k in set(n.get("cleared", [])) for n in neighbours(legacy, m)):
                accepted["minute skew"] += 1; s_cl.discard(k)
        if l_cl != s_cl:
            diffs.append(f"cleared {sorted(l_cl)} vs {sorted(s_cl)}")
        lc, sc = dict(L.get("cursors") or {}), dict(S.get("cursors") or {})
        # R1A_PUMP_POWER changes every few seconds; the two recorders sample
        # ~35 s apart inside the same minute, so that cursor always differs.
        if lc.pop("last_r1a_power_id", None) != sc.pop("last_r1a_power_id", None):
            accepted["R1A power cursor timing"] += 1
        if l_events != s_events:
            # the legacy recorder runs after the live monitor consumed the
            # cursors, so it cannot see events the live monitor already sent
            # in that minute; accept when the legacy cursors moved around m.
            moved = any(dict(n.get("cursors") or {}) != dict(L.get("cursors") or {})
                        for n in neighbours(legacy, m))
            if moved:
                accepted["events consumed by live monitor"] += 1
            else:
                diffs.append(f"events {l_events} vs {s_events}")
        if lc != sc:
            near = [dict(n.get("cursors") or {}) for n in neighbours(legacy, m) + neighbours(shadow, m)]
            for n in near:
                n.pop("last_r1a_power_id", None)
            if sc in near or lc in near:
                accepted["cursor timing"] += 1
            else:
                diffs.append(f"cursors {lc} vs {sc}")
        if L.get("errors") or S.get("errors"):
            diffs.append(f"errors legacy={L.get('errors')} shadow={S.get('errors')}")
        if diffs:
            real.append((m, diffs))
    print(f"accepted differences: {dict(accepted)}")
    print(f"UNEXPECTED differences: {len(real)} minute(s)")
    for m, diffs in real[:40]:
        print(f"  {m}: " + " | ".join(diffs))
    secs = [S.get("secs", 0) for S in shadow.values()]
    lsecs = [L.get("secs", 0) for L in legacy.values()]
    if secs and lsecs:
        print(f"cycle time: shadow avg {sum(secs)/len(secs):.2f}s max {max(secs):.2f}s | "
              f"legacy avg {sum(lsecs)/len(lsecs):.2f}s max {max(lsecs):.2f}s")


if __name__ == "__main__":
    main()
