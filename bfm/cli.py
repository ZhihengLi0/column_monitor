"""Command-line entry points.

  python3 -m bfm.cli serve                 run the daemon (what systemd does)
  python3 -m bfm.cli once [--dry-run]      one check cycle, print the decision record
  python3 -m bfm.cli shadow [--hours N]    dry-run daemon on a COPY of the state (phase-5 shadow run)
  python3 -m bfm.cli summary [--post]      print (or post) the 12 h summary
  python3 -m bfm.cli init                  set every event cursor to "now" (skip history)
  python3 -m bfm.cli status                health snapshot: state, queue, DB, Slack
  python3 -m bfm.cli cmd "<text>" [--post] run one Slack command locally (replies printed)
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from . import __version__, config


def _daemon(dry_run: bool, state_path: Path | None = None):
    from .daemon import Daemon, setup_logging
    setup_logging(to_file=not dry_run)
    return Daemon(dry_run=dry_run, state_path=state_path)


def cmd_serve(args) -> int:
    _daemon(dry_run=args.dry_run).serve()
    return 0


def cmd_once(args) -> int:
    d = _daemon(dry_run=True)
    rec = d.run_cycle()
    print(json.dumps(rec, indent=2, default=str))
    print("queued:", [(m["kind"], m["key"], m["text"][:60]) for m in d.store.data["outbox"]])
    return 0


def cmd_shadow(args) -> int:
    """Run the full daemon in dry-run against a copy of the live state, writing
    one decision record per cycle to the shadow log (compared later with the
    legacy recorder's output)."""
    from .daemon import Daemon, setup_logging
    setup_logging(to_file=False)
    copy = Path(args.state_copy)
    if not copy.exists():
        shutil.copy(config.STATE_FILE, copy)
    d = Daemon(dry_run=True, state_path=copy)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    import threading, time
    stop_at = time.monotonic() + args.hours * 3600

    def dump():
        while not d.stop.is_set():
            d.stop.wait(config.CHECK_INTERVAL_S)
            if d.decisions:
                with out.open("a") as f:
                    for rec in d.decisions:
                        rec["sent"] = [s.get("text", s.get("upload", ""))[:120] for s in d.slack.sent_log]
                        f.write(json.dumps(rec, default=str) + "\n")
                d.decisions.clear()
                d.slack.sent_log.clear()
            if time.monotonic() > stop_at:
                d.stop.set()

    threading.Thread(target=dump, daemon=True).start()
    d.serve()
    return 0


def cmd_summary(args) -> int:
    from .summary import generate_summary
    d = _daemon(dry_run=not args.post)
    text = generate_summary(d.cs2, d.store.data)
    print(text)
    if args.post:
        d.slack.post_now(text, color="good")
    return 0


def cmd_init(args) -> int:
    from . import events
    d = _daemon(dry_run=True)
    with d.store.locked() as st:
        events.init_cursors(st, d.cs2)
        import time
        st["last_slack_ts"] = f"{time.time():.6f}"
    d.store.save()
    print("cursors initialised:", {k: d.store.data[k] for k in d.store.data if k.startswith("last_")})
    return 0


def cmd_status(args) -> int:
    from .core.state import StateStore
    st = StateStore(config.STATE_FILE).load().data
    d = _daemon(dry_run=True)
    print(f"bfm {__version__}")
    print("mode:", st.get("current_mode"), "(override)" if st.get("mode_override") else "(auto)",
          "| paused:", st.get("alerts_paused"), "| sentinel:", st.get("cs2_alerts_enabled"))
    print("last cycle:", st.get("last_cycle"), "| active alarms:", list(st.get("active_alarms", {})))
    print("silenced:", list(st.get("acked_sensors", {})), "| pending:", list(st.get("pending_alert_msgs", {})))
    print("outbox:", len(st.get("outbox", [])), "| slack:", st.get("slack"))
    print("cs2 db:", "ok" if d.cs2.available else d.cs2.last_error, "| ln2 db:", "ok" if d.ln2.available else d.ln2.last_error)
    print("slack api:", "ok" if d.slack.probe(force=True) else d.slack.last_error)
    return 0


def cmd_cmd(args) -> int:
    d = _daemon(dry_run=not args.post)
    with d.store.locked() as st:
        name = d.router.execute(args.text, "0.0", st=st, user_id="cli")
    print("handler:", name)
    for m in d.store.data["outbox"]:
        if m["kind"] == "reply":
            print("---\n" + m["text"])
    if args.post:
        d.drain_outbox()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="bfm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("once"); s.set_defaults(fn=cmd_once)
    s = sub.add_parser("shadow"); s.add_argument("--hours", type=float, default=24)
    s.add_argument("--state-copy", default="/mnt/harddrive/pi/logs/shadow_state.json")
    s.add_argument("--out", default="/mnt/harddrive/pi/logs/shadow_decisions.jsonl"); s.set_defaults(fn=cmd_shadow)
    s = sub.add_parser("summary"); s.add_argument("--post", action="store_true"); s.set_defaults(fn=cmd_summary)
    s = sub.add_parser("init"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("cmd"); s.add_argument("text"); s.add_argument("--post", action="store_true"); s.set_defaults(fn=cmd_cmd)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
