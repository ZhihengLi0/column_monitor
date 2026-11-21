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
    for m in minutes:
        L, S = legacy[m], shadow[m]
        l_fired = {k for k, _ in L.get("alerts", []) if k}
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
        for k in list(only_s):
            if k in l_active:
                accepted["cooldown phase"] += 1
                only_s.discard(k)
        if only_l or only_s:
            diffs.append(f"fired legacy-only={sorted(only_l)} shadow-only={sorted(only_s)}")
        if set(L.get("cleared", [])) != set(S.get("cleared", [])):
            diffs.append(f"cleared {sorted(L.get('cleared', []))} vs {sorted(S.get('cleared', []))}")
        if l_events != s_events:
            diffs.append(f"events {l_events} vs {s_events}")
        if L.get("cursors") != S.get("cursors"):
            diffs.append(f"cursors {L.get('cursors')} vs {S.get('cursors')}")
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
