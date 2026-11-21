"""The long-running service: one process, three threads, one state.

  check thread    every CHECK_INTERVAL_S: mode → alarms → events → reminders
                  → recording → scheduled summary → persist (+ sd_notify)
  command thread  every COMMAND_POLL_S: Slack @mentions, reply pollers
                  (acks / reminder confirmations / NLP feedback)
  sender thread   drains the state outbox at SENDER_PACE_S, writes the Slack
                  `ts` back for ack tracking, posts the offline digest

Replaces: cron monitor.py (per minute), slack_responder.py (@reboot), the two
ln2 cron jobs. See the plan in .claude/plans for the why.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__, alarms, config, events, modes, recording, reminders
from .commands import Router
from .core import timeutil as tu
from .core.db import Database, DatabaseUnavailable, DataContext
from .core.slack import SlackClient
from .core.state import StateStore

log = logging.getLogger("bfm.daemon")


# ── logging ──────────────────────────────────────────────────────────────────

def setup_logging(level: int = logging.INFO, to_file: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if to_file:
        log_dir = config.LOG_DIR if config.LOG_DIR.exists() else config.ROOT / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(log_dir / "bfm.log", maxBytes=10_000_000, backupCount=5)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            root.warning("file logging disabled: %s", e)
    root.setLevel(level)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ── sd_notify (Type=notify + WatchdogSec) without a dependency ───────────────

def sd_notify(msg: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(msg.encode())
    except OSError:
        pass


# ── the daemon ───────────────────────────────────────────────────────────────

class Daemon:
    def __init__(self, *, dry_run: bool = False, state_path: Path | None = None):
        self.dry_run = dry_run
        self.store = StateStore(state_path or config.STATE_FILE).load()
        self.cs2 = Database(host=config.LOCAL_PG_HOST, port=config.LOCAL_PG_PORT, user=config.LOCAL_PG_USER,
                            password=config.LOCAL_PG_PASSWORD, dbname=config.LOCAL_PG_DB, name="cs2")
        self.ln2 = Database(host=config.LOCAL_PG_HOST, port=config.LOCAL_PG_PORT, user=config.LOCAL_PG_USER,
                            password=config.LOCAL_PG_PASSWORD, dbname=config.LN2_PG_DB, name="ln2")
        self.slack = SlackClient(config.SLACK_BOT_TOKEN or "", config.SLACK_CHANNEL,
                                 config.SLACK_BOT_USER_ID, dry_run=dry_run)
        self.router = Router(self.store, self.cs2, self.ln2, self.slack)
        self.stop = threading.Event()
        self.db_alarm = alarms.DatabaseDown()
        self.cycle_count = 0
        self.last_cycle: dict = {}
        self.decisions: list[dict] = []        # dry-run / --once: per-cycle decision records

    # ── one check cycle ──────────────────────────────────────────────────────
    def run_cycle(self) -> dict:
        """Evaluate everything once. Returns a decision record (what fired,
        what cleared, cursors, timing). Never raises."""
        t0 = time.monotonic()
        rec = {"ts": tu.now().isoformat(timespec="seconds"), "fired": [], "cleared": [],
               "events": 0, "errors": {}, "mode": None}
        with self.store.locked() as st:
            eng = alarms.AlarmEngine(st, self.store.enqueue)
            try:
                self.cs2.connect()
            except DatabaseUnavailable as e:
                rec["errors"]["db"] = str(e)
                eng.apply_all(self.db_alarm.evaluate(None, st, error=str(e)))
                self._finish_cycle(st, rec, t0)
                return rec
            eng.apply_all(self.db_alarm.evaluate(None, st))        # clears DB_DOWN if it was active
            ctx = DataContext(self.cs2, self.ln2)
            try:
                modes.update_mode(st, ctx, self.store.enqueue)
            except Exception as e:
                rec["errors"]["mode"] = self._err(e)
            rec["mode"] = st.get("current_mode")

            for alarm in alarms.ALL_ALARMS:
                try:
                    eng.apply_all(alarm.evaluate(ctx, st))
                except DatabaseUnavailable as e:
                    rec["errors"][alarm.name] = str(e)
                    break
                except Exception as e:
                    rec["errors"][alarm.name] = self._err(e)
                sd_notify("WATCHDOG=1")

            try:
                for m in alarms.Milestones().evaluate(ctx, st):
                    self.store.enqueue(m, color="good", kind="event")
                    rec["events"] += 1
            except Exception as e:
                rec["errors"]["milestones"] = self._err(e)

            for watcher in events.ALL_EVENTS:
                try:
                    if st.get("alerts_paused"):
                        msgs = watcher.evaluate(ctx, st)       # cursors still advance
                        if msgs:
                            log.info("alerts paused — suppressed %d %s event(s)", len(msgs), watcher.name)
                        continue
                    for m in watcher.evaluate(ctx, st):
                        self.store.enqueue(m, color="danger", kind="event")
                        rec["events"] += 1
                except DatabaseUnavailable as e:
                    rec["errors"][watcher.name] = str(e)
                except Exception as e:
                    rec["errors"][watcher.name] = self._err(e)
                sd_notify("WATCHDOG=1")

            for rem in reminders.ALL_REMINDERS:
                try:
                    rem.tick(st, self.store.enqueue)
                except Exception as e:
                    rec["errors"][rem.name] = self._err(e)

            try:
                recording.record_pulsetube(ctx, dry_run=self.dry_run)
                recording.prune_pulsetube(self.cs2, st, dry_run=self.dry_run)
            except Exception as e:
                rec["errors"]["recording"] = self._err(e)

            try:
                self._scheduled_summary(st)
            except Exception as e:
                rec["errors"]["summary"] = self._err(e)

            rec["fired"], rec["cleared"] = eng.fired, eng.cleared
            self._finish_cycle(st, rec, t0)
        return rec

    def _finish_cycle(self, st: dict, rec: dict, t0: float) -> None:
        rec["cursors"] = {k: st.get(k) for k in ("last_cs2_alert_id", "last_r1a_event_id",
                                                  "last_r1a_power_id", "last_heater_event_id", "last_valve_event_id")}
        rec["active"] = sorted(st.get("active_alarms", {}))
        rec["outbox"] = len(st.get("outbox", []))
        rec["secs"] = round(time.monotonic() - t0, 2)
        st["last_cycle"] = rec["ts"]
        self.last_cycle = rec
        self.cycle_count += 1
        if rec["errors"]:
            log.error("cycle %d: errors %s", self.cycle_count, rec["errors"])
        log.info("cycle %d: mode=%s fired=%s cleared=%s events=%d active=%d outbox=%d %.2fs",
                 self.cycle_count, rec["mode"], rec["fired"], rec["cleared"], rec["events"],
                 len(rec["active"]), rec["outbox"], rec["secs"])
        if self.dry_run:
            self.decisions.append(rec)

    @staticmethod
    def _err(e: Exception) -> str:
        log.error("check failed: %s", e, exc_info=True)
        return f"{type(e).__name__}: {str(e)[:200]}"

    # ── scheduled 12 h summary ───────────────────────────────────────────────
    def _scheduled_summary(self, st: dict) -> None:
        now = tu.now()
        for hour in config.SUMMARY_HOURS:
            slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if slot > now:
                continue
            key = slot.strftime("%Y-%m-%dT%H")
            if st.get("last_summary_slot") == key:
                continue
            if now - slot > timedelta(minutes=config.SUMMARY_MAX_LATE_MIN):
                st["last_summary_slot"] = key          # too late: skip, don't spam
                continue
            from .summary import generate_summary
            st["last_summary_slot"] = key
            self.store.enqueue(generate_summary(self.cs2, st), color="good", kind="reply",
                               key=f"SUMMARY::{key}")
            log.info("12 h summary queued for slot %s", key)

    # ── reply pollers (acks, reminder confirmations) ─────────────────────────
    def poll_replies(self) -> None:
        if not self.slack.ok and not self.slack.probe():
            return
        with self.store.locked() as st:
            pending = st.setdefault("pending_alert_msgs", {})
            acked = st.setdefault("acked_sensors", {})
            for key in list(pending):
                ts = pending[key].get("ts")
                if not ts:
                    continue
                silence = None
                if self.slack.reactions(ts) & config.ACK_REACTIONS:
                    silence = (tu.minutes_from_now(config.ACK_DEFAULT_MINUTES), f"{config.ACK_DEFAULT_MINUTES} minutes")
                if silence is None:
                    for reply in self.slack.replies(ts):
                        req = silence_request(reply.get("text", ""))
                        if req:
                            silence = req
                            break
                if not self.slack.ok:
                    return                                   # circuit opened mid-loop
                if silence:
                    expires, label = silence
                    acked[key] = expires
                    del pending[key]
                    when = "permanently" if label == "permanently" else f"for *{label}*"
                    self.store.enqueue(f"🔕 Okay, silencing *{key}* {when}.", color="good",
                                       thread_ts=ts, kind="reply")
                    log.info("%s silenced (%s)", key, label)
            for rem in reminders.ALL_REMINDERS:
                rts = rem.reminder_ts(st)
                s = rem.state(st)
                if rts and (rem.name != "eyewash" or not s.get("confirmed")) and \
                        (rem.name != "filter" or s.get("active")):
                    rem.confirm_from_replies(st, self.slack.replies(rts), self.store.enqueue)

    # ── sender ───────────────────────────────────────────────────────────────
    def drain_outbox(self, max_msgs: int = 50) -> int:
        """Send queued messages in order. Returns the number sent."""
        sent = 0
        while sent < max_msgs and not self.stop.is_set():
            m = self.store.outbox_peek()
            if m is None:
                break
            if not self.slack.ok and not self.slack.probe():
                break
            self._maybe_offline_digest()
            ts = self.slack.post_now(m["text"], color=m.get("color", "danger"), thread_ts=m.get("thread_ts"))
            if ts is None:
                self.store.outbox_failed(m["id"])
                if not self.slack.ok:
                    break
                time.sleep(2)                                # transient (429 / API error)
                if m.get("attempts", 0) >= 5:
                    log.error("dropping message after 5 failed attempts: %s", m["text"][:80])
                    self.store.outbox_done(m["id"], None)
                continue
            done = self.store.outbox_done(m["id"], ts)
            self._after_send(done)
            sent += 1
            time.sleep(config.SENDER_PACE_S)
        return sent

    def _after_send(self, m: dict | None) -> None:
        if not m:
            return
        key, ts = m.get("key"), m.get("ts")
        with self.store.locked() as st:
            if m.get("kind") == "alarm" and key and ts:
                st.setdefault("pending_alert_msgs", {})[key] = {"ts": ts, "channel": config.SLACK_CHANNEL}
                aa = st.setdefault("active_alarms", {})
                if key in aa and isinstance(aa[key], dict):
                    aa[key]["ts"] = ts
            elif m.get("kind") == "reminder" and key and ts:
                name = key.split("::", 1)[1]
                for rem in reminders.ALL_REMINDERS:
                    if rem.name == name:
                        rem.state(st)["reminder_ts"] = ts

    def _maybe_offline_digest(self) -> None:
        """First message after an outage longer than SLACK_OFFLINE_DIGEST_MIN."""
        with self.store.locked() as st:
            sl = st.setdefault("slack", {})
            since = tu.parse_iso(sl.get("down_since"))
            if since is None:
                return
            gap = tu.now() - since
            sl["down_since"] = None
            dropped, skipped = sl.get("dropped_events", 0), sl.get("skipped_commands", 0)
            sl["dropped_events"] = sl["skipped_commands"] = 0
            if gap < timedelta(minutes=config.SLACK_OFFLINE_DIGEST_MIN):
                return
            ob = st.get("outbox", [])
            n_alarms = sum(1 for x in ob if x.get("kind") == "alarm")
            n_events = sum(1 for x in ob if x.get("kind") == "event")
            collapsed = sum(x.get("collapsed", 0) for x in ob)
            active = [v.get("label", k) if isinstance(v, dict) else k for k, v in st.get("active_alarms", {}).items()]
            text = (f":satellite: *Monitor was offline from Slack for {gap.total_seconds() / 3600:.1f} h* "
                    f"({since.strftime('%m-%d %H:%M')} → {tu.now().strftime('%m-%d %H:%M')}). "
                    "Monitoring and data recording continued the whole time.\n"
                    f"• Queued and now being sent: {n_alarms} alarm(s), {n_events} event(s)"
                    + (f" ({collapsed} repeat(s) collapsed)" if collapsed else "") + "\n"
                    + (f"• Dropped {dropped} older event message(s) to avoid flooding\n" if dropped else "")
                    + (f"• Ignored {skipped} command(s) older than {config.COMMAND_MAX_AGE_MIN} min\n" if skipped else "")
                    + ("• Currently active alarms: " + ", ".join(active) if active else "• No alarms active right now"))
        ts = self.slack.post_now(text, color="warning")
        log.warning("offline digest posted (gap %.0f min): %s", gap.total_seconds() / 60, "ok" if ts else "FAILED")

    def _track_slack_health(self) -> None:
        with self.store.locked() as st:
            sl = st.setdefault("slack", {})
            if not self.slack.ok and not sl.get("down_since"):
                sl["down_since"] = tu.now().isoformat()
            sl["ok"] = self.slack.ok

    # ── threads ──────────────────────────────────────────────────────────────
    def _check_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.run_cycle()
                self.store.save()
            except Exception as e:
                log.critical("check loop crashed: %s\n%s", e, traceback.format_exc())
            self.stop.wait(config.CHECK_INTERVAL_S)

    def _command_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.router.poll()
                self.poll_replies()
                self._track_slack_health()
            except Exception as e:
                log.error("command loop error: %s", e, exc_info=True)
            self.stop.wait(config.COMMAND_POLL_S)

    def _sender_loop(self) -> None:
        while not self.stop.is_set():
            try:
                self.drain_outbox()
                self._track_slack_health()
            except Exception as e:
                log.error("sender loop error: %s", e, exc_info=True)
            self.stop.wait(1.0)

    def serve(self) -> None:
        log.info("bfm %s starting (dry_run=%s, state=%s)", __version__, self.dry_run, self.store.path)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        threads = [threading.Thread(target=self._check_loop, name="check", daemon=True),
                   threading.Thread(target=self._command_loop, name="command", daemon=True),
                   threading.Thread(target=self._sender_loop, name="sender", daemon=True)]
        for t in threads:
            t.start()
        sd_notify("READY=1")
        while not self.stop.is_set():
            self.stop.wait(5)
            sd_notify("WATCHDOG=1") if self.last_cycle and \
                tu.now() - tu.parse_iso(self.last_cycle["ts"]) < timedelta(minutes=4) else None
        sd_notify("STOPPING=1")
        log.info("stopping — persisting state (%d queued message(s))", self.store.outbox_len())
        for t in threads:
            t.join(timeout=15)
        self.store.save()
        self.cs2.close()
        self.ln2.close()
        log.info("stopped")


# ── silence-request parser (alert-thread replies) ───────────────────────────
import re as _re                                       # noqa: E402
from .core import units as _units                      # noqa: E402
from .core.slack import unescape as _unescape          # noqa: E402

SILENCE_RE = _re.compile(r"\b(silent|silence|mute|quiet|snooze|hush|ignore|ack|acknowledge)\b|"
                         r"静音|安静|消音|别响|闭嘴|忽略", _re.IGNORECASE)


def silence_request(text: str) -> tuple[str, str] | None:
    """Alert-thread reply → (expires_at_iso, label) or None.
    'ok' → 10 min; 'silent for 2h'; '静音3天'; 'silence forever'; bare '2h'."""
    low = _unescape(text).strip().lower()
    if not low:
        return None
    if low in ("ok", "okay", "k"):
        return tu.minutes_from_now(config.ACK_DEFAULT_MINUTES), f"{config.ACK_DEFAULT_MINUTES} minutes"
    parsed = _units.parse_duration(low)
    has_kw = bool(SILENCE_RE.search(low))
    core = _re.sub(r"\b(for|please|pls|thanks?|thx|the|alert)\b", " ", low).strip(" .,!，。")
    core = _re.sub(r"\s+", " ", core)
    pure_duration = bool(_units.FOREVER_RE.fullmatch(core)
                         or (_re.fullmatch(r"\d+(?:\.\d+)?\s*\S+", core) and parsed is not None))
    if not (has_kw or pure_duration):
        return None
    if parsed is None:
        return tu.minutes_from_now(config.ACK_DEFAULT_MINUTES), f"{config.ACK_DEFAULT_MINUTES} minutes"
    mins, label = parsed
    if mins is None:
        return tu.FOREVER_TS, "permanently"
    return tu.minutes_from_now(mins), label
