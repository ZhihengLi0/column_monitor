"""Command router: @mention text → handler, in the same precedence the legacy
_execute_command used (one rule per entry instead of 574 lines)."""
from __future__ import annotations

import logging
import re
import time

from .. import config
from ..core import timeutil as tu
from ..core import units
from ..core.slack import unescape
from ..modes import mode_in_text
from ..reminders import FILTER_DONE_RE, FILTER_WORD_RE
from . import control, ln2, nlp, plot, readings, status
from .context import CommandContext

log = logging.getLogger("bfm.router")

PRONOUNS_RE = re.compile(r"\b(那|它|it\b|same|that|前面|刚才|之前)\b")
LONGER_WORDS = ("longer", "extend", "更长", "时间长", "久一点")
TIME_TOKEN_RE = re.compile(r"^(\d{6}_\d{4})$")
CTX_WINDOW_S = 600


class Router:
    def __init__(self, store, cs2, ln2db, slack):
        self.store = store          # StateStore
        self.cs2 = cs2
        self.ln2 = ln2db
        self.slack = slack

    # ── context ──────────────────────────────────────────────────────────────
    def _cc(self, st: dict, reply_ts: str, user_id: str = "default", raw: str = "") -> CommandContext:
        return CommandContext(st=st, cs2=self.cs2, ln2=self.ln2, enqueue=self.store.enqueue,
                              upload=self.slack.upload_image, reply_ts=reply_ts, user_id=user_id,
                              raw_text=raw)

    # ── polling ──────────────────────────────────────────────────────────────
    def poll(self) -> int:
        """Fetch new @mentions since last_slack_ts and execute them. Returns the
        number of commands run. Commands older than COMMAND_MAX_AGE_MIN are
        skipped (after a 10-day outage the legacy code replayed them all)."""
        if not self.slack.ok and not self.slack.probe():
            return 0
        with self.store.locked() as st:
            nlp.check_feedback(st, self.slack, lambda ts: self._cc(st, ts),
                               lambda text, ts: self.execute(text, ts, st=st))
            last_ts = st.get("last_slack_ts", "0")
        msgs = self.slack.history(oldest=last_ts, limit=50)
        if not msgs:
            return 0
        bot_tag = f"<@{config.SLACK_BOT_USER_ID}>"
        new_ts, ran, skipped = last_ts, 0, 0
        max_age = config.COMMAND_MAX_AGE_MIN * 60
        for msg in sorted(msgs, key=lambda m: float(m.get("ts", "0"))):
            ts = msg.get("ts", "0")
            if float(ts) <= float(last_ts):
                continue
            new_ts = max(new_ts, ts, key=float)
            text = msg.get("text", "")
            if bot_tag not in text:
                continue
            if time.time() - float(ts) > max_age:
                skipped += 1
                continue
            clean = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
            with self.store.locked() as st:
                try:
                    self.execute(clean, ts, st=st, user_id=msg.get("user", "default"))
                except Exception as e:
                    log.error("command %r failed: %s", clean, e, exc_info=True)
                    self.store.enqueue(f":x: Sorry, that command failed: `{str(e)[:120]}`",
                                       color="danger", thread_ts=ts, kind="reply")
            ran += 1
        with self.store.locked() as st:
            st["last_slack_ts"] = new_ts
            if skipped:
                st.setdefault("slack", {})["skipped_commands"] = \
                    st.get("slack", {}).get("skipped_commands", 0) + skipped
                log.warning("skipped %d @mention(s) older than %d min", skipped, config.COMMAND_MAX_AGE_MIN)
        return ran

    # ── dispatch ─────────────────────────────────────────────────────────────
    def execute(self, text: str, reply_ts: str, st: dict, user_id: str = "default") -> str:
        """Run one command. Returns the handler name (for tests/dry-run)."""
        cc = self._cc(st, reply_ts, user_id, text)
        lower = unescape(text).lower().strip()
        log.info("command from %s: %r", user_id, text)

        if nlp.DENY_STANDALONE_RE.match(lower):
            nlp.standalone_wrong(cc)
            return "nlp_wrong"

        # context memory (10-minute window)
        ctx = st.get("ctx", {})
        ctx_valid = time.time() - ctx.get("ts", 0) < CTX_WINDOW_S
        ctx_sensor = units.resolve_sensor(ctx.get("sensor_key", "")) if ctx_valid else None
        ctx_minutes = ctx.get("minutes", 30) if ctx_valid else None

        def update_ctx(sensor: units.Sensor, minutes: float):
            st["ctx"] = {"sensor_key": sensor.short, "intent": "plot", "minutes": minutes, "ts": time.time()}

        if ctx_sensor and PRONOUNS_RE.search(lower):
            lower = PRONOUNS_RE.sub(ctx_sensor.short.lower(), lower, count=1)
        if ctx_sensor and any(w in lower for w in LONGER_WORDS):
            new_min = (ctx_minutes or 30) * 4
            plot.plot_sensors(cc, [ctx_sensor], minutes=new_min)
            update_ctx(ctx_sensor, new_min)
            return "plot_longer"

        if lower in ("", "help"):
            status.cmd_help(cc)
            return "help"

        if ln2.LN2_RE.search(lower):
            ln2.handle(cc, lower)
            return "ln2"

        for pattern, fn, name in (
            (r"pressure\s+reading", readings.cmd_pressure, "pressure"),
            (r"temperature\s+reading", readings.cmd_temperature, "temperature"),
            (r"pump\s+status", readings.cmd_pump_status, "pump_status"),
            (r"heater\s+status", readings.cmd_heater_status, "heater_status"),
            (r"valve\s+status", readings.cmd_valve_status, "valve_status"),
        ):
            if re.fullmatch(pattern, lower):
                fn(cc)
                return name

        if not lower.startswith("plot") and (re.search(r"pulse\s*tube|pulsetube", lower)
                                             or re.fullmatch(r"pt\s+status", lower)):
            readings.cmd_pulsetube_status(cc)
            return "pulsetube_status"

        if lower.startswith("plot"):
            return self._plot(cc, lower, ctx_sensor, update_ctx)

        if re.fullmatch(r"pause\s+alerts?", lower):
            control.cmd_pause(cc)
            return "pause"
        if re.fullmatch(r"resume\s+alerts?", lower):
            control.cmd_resume(cc)
            return "resume"
        m = re.fullmatch(r"sentinel\s+(on|off)", lower)
        if m:
            control.cmd_sentinel(cc, m.group(1) == "on")
            return "sentinel"
        if lower == "list":
            status.cmd_list(cc)
            return "list"
        if lower in ("status", "mode"):
            status.cmd_status(cc)
            return "status"

        # alarm listing: an alarm word + (question word | mode | criteria | current | bare)
        alarm_word = re.search(r"alarms?|alerts?|报警|警报", lower)
        query_word = re.search(r"\b(what|which|list|show|display|tell)\b|有什么|哪些|什么|列出|有哪些|告诉", lower)
        crit_word = re.search(r"criteri|trigger|condition|条件|标准|触发", lower)
        cur_word = re.search(r"\bcurrent(?:ly)?\b|\bnow\b|当前|现在|目前", lower)
        alarm_mode = mode_in_text(lower)
        bare = lower in ("alarms", "alarm", "alarm list", "alarm listing", "alarm criteria", "alarm criterion")
        if alarm_word and (query_word or alarm_mode or bare or crit_word or cur_word):
            only = alarm_mode or (st.get("current_mode") if cur_word else None)
            status.cmd_list_alarms(cc, only_mode=only)
            return "list_alarms"

        if FILTER_WORD_RE.search(lower):
            if FILTER_DONE_RE.search(lower):
                control.cmd_filter_done(cc)
                return "filter_done"
            if re.fullmatch(r"(chilled\s+water\s+)?filter(\s+status)?|滤芯状态|过滤器状态", lower):
                control.cmd_filter_status(cc)
                return "filter_status"

        m = re.fullmatch(r"set\s+mode\s+(\S+)", text.strip(), re.IGNORECASE)
        if m:
            control.cmd_set_mode(cc, m.group(1))
            return "set_mode"
        if lower == "ack":
            control.cmd_ack(cc)
            return "ack"
        m = re.fullmatch(r"(cold\s+|idle\s+)?reset\s+(\S+)", text.strip(), re.IGNORECASE)
        if m:
            control.cmd_reset_threshold(cc, m.group(2), m.group(1).strip().lower() if m.group(1) else None)
            return "reset_threshold"
        m = re.fullmatch(r"(cold\s+|idle\s+)?change\s+(\S+)\s+to\s+([\d.e+\-]+)\s+for\s+(.+)",
                         text.strip(), re.IGNORECASE)
        if m:
            control.cmd_change_threshold(cc, m.group(2), m.group(3), m.group(4),
                                         m.group(1).strip().lower() if m.group(1) else None)
            return "change_threshold"

        nlp.dispatch(cc, text, ctx_sensor, ctx_minutes, update_ctx)
        return "nlp"

    # ── plot parsing ─────────────────────────────────────────────────────────
    def _plot(self, cc: CommandContext, lower: str, ctx_sensor, update_ctx) -> str:
        pt = plot.match_pulsetube_quantity(lower)
        if pt:
            col, label, kind = pt
            plot.plot_pulsetube(cc, col, label, kind, units.parse_duration_minutes(lower, 120))
            return "plot_pulsetube"
        tokens = lower.split()[1:]
        sensors, time_tokens = [], []
        for tok in tokens:
            if TIME_TOKEN_RE.fullmatch(tok):
                time_tokens.append(tok)
            else:
                s = units.resolve_sensor(tok) if not tok.isdigit() else None   # digits are durations here
                if s:
                    sensors.append(s)
        remaining = " ".join(t for t in tokens if not TIME_TOKEN_RE.fullmatch(t)
                             and not (not t.isdigit() and units.resolve_sensor(t)))
        minutes = units.parse_duration_minutes(remaining, 30)
        log.info("Plot parser: tokens=%s sensors=%s minutes=%s times=%s",
                 tokens, [s.short for s in sensors], minutes, time_tokens)
        if not sensors and ctx_sensor:
            sensors = [ctx_sensor]
        if not sensors:
            cc.reply("Which sensor? e.g. `plot P2 12h`, `plot P2 P5 2h`")
            return "plot_which"
        if len(time_tokens) >= 2:
            t0, t1 = tu.parse_compact_local(time_tokens[0]), tu.parse_compact_local(time_tokens[1])
            if t0 and t1 and t0 < t1:
                plot.plot_sensors(cc, sensors, start=t0, end=t1)
                update_ctx(sensors[-1], minutes)
            else:
                cc.reply(f"Invalid time range. Format: `plot P1 260622_0000 260622_0130` ({tu.tz_abbrev()})")
            return "plot_range"
        plot.plot_sensors(cc, sensors, minutes=minutes)
        update_ctx(sensors[-1], minutes)
        return "plot"
