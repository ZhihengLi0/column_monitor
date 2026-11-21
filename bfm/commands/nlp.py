"""Natural-language fallback (nlp_intent.py classifier) and the self-learning
feedback loop ("yes" / "wrong" replies under uncertain answers)."""
from __future__ import annotations

import logging
import re
import time

from .. import config
from ..core import timeutil as tu
from ..core import units
from ..core.slack import unescape
from . import control, plot, readings, status
from .context import CommandContext

log = logging.getLogger("bfm.nlp")

CONFIRM_RE = re.compile(
    r"\b(yes|yep|yeah|yup|correct|right|sure|ok|okay|confirmed|"
    r"you'?re right|you are right|that'?s right|that is right|"
    r"对|是的|没错|正确|好的|可以)\b|^(对|是|好)$", re.IGNORECASE)
DENY_RE = re.compile(
    r"\b(no|nope|wrong|incorrect|not right|that'?s wrong|that is wrong|不对|错了|不是)\b", re.IGNORECASE)
DENY_STANDALONE_RE = re.compile(
    r"^(?:wrong|incorrect|not right|that(?:'s| was| is) wrong|不对|错了|不是这个意思)$")

NLP_LOW, NLP_UNCERTAIN = 0.12, 0.45


def _nlp():
    import nlp_intent          # repo-root module (bfm.config puts ROOT on sys.path)
    return nlp_intent


def _didnt_understand(cc: CommandContext, text: str, tail: str = "Try natural language or type `help` to see all commands.") -> None:
    cc.reply(f"I didn't understand: `{text}`\n{tail}")


def _sensors_from_mappings(mappings: list[str]) -> list[units.Sensor]:
    return [units.BY_MAPPING[m] for m in mappings if m in units.BY_MAPPING]


def dispatch(cc: CommandContext, text: str, ctx_sensor: units.Sensor | None, ctx_minutes: float | None,
             update_ctx) -> None:
    """Classify `text` and run the matching handler. Mirrors the legacy NLP
    branch of _execute_command, including the uncertainty hint + tracking."""
    try:
        nlp = _nlp()
        intent, ent, conf = nlp.classify_command(text)
    except Exception as e:
        log.error("NLP classify failed: %s", e)
        _didnt_understand(cc, text, "Type `help` to see all commands.")
        return
    log.info("NLP: intent=%s conf=%.2f entities=%s", intent, conf, ent)
    if conf < NLP_LOW:
        _didnt_understand(cc, text)
        return
    st = cc.st
    try:
        if intent == "plot":
            sensors = _sensors_from_mappings(ent.get("sensors", []))
            if not sensors and ctx_sensor:
                sensors = [ctx_sensor]
            if not sensors:
                cc.reply("Which sensor would you like to plot? (e.g. P2, MXC, FLOW)")
                return
            minutes = ent.get("minutes") or ctx_minutes or 30
            plot.plot_sensors(cc, sensors, minutes=minutes)
            update_ctx(sensor=sensors[-1], minutes=minutes)
        elif intent == "temperature_reading":
            readings.cmd_temperature(cc)
        elif intent == "pause_alerts":
            control.cmd_pause(cc, via_nlp=True)
        elif intent == "resume_alerts":
            control.cmd_resume(cc, via_nlp=True)
        elif intent == "pressure_reading":
            readings.cmd_pressure(cc)
        elif intent == "pump_status":
            readings.cmd_pump_status(cc, highlight=ent.get("device"))
        elif intent == "heater_status":
            readings.cmd_heater_status(cc)
        elif intent == "valve_status":
            readings.cmd_valve_status(cc, highlight=ent.get("valve"))
        elif intent == "status":
            pump = nlp._extract_pump_device(text)
            valve = nlp._extract_valve_name(text)
            if pump:
                readings.cmd_pump_status(cc, highlight=pump)
            elif valve:
                readings.cmd_valve_status(cc, highlight=valve)
            else:
                status.cmd_status(cc)
        elif intent == "daily_summary":
            from ..summary import generate_summary
            if cc.db_ok("build the summary"):
                cc.reply(generate_summary(cc.cs2, st), color="good")
        elif intent == "help":
            status.cmd_help(cc)
        elif intent == "ack":
            control.cmd_ack(cc)
        elif intent == "sentinel":
            on_off = ent.get("on_off")
            control.cmd_sentinel(cc, True if on_off == "on" else False if on_off == "off" else None)
        elif intent == "set_mode":
            word = ent.get("mode") or ""
            if not word:
                cc.reply("Which mode? Say `cold`, `idle`, `transitioning`, or `auto`.")
            else:
                control.cmd_set_mode(cc, word)
        elif intent == "change_threshold":
            sensor_map, value, mk, minutes = ent.get("sensor"), ent.get("value"), ent.get("mode_prefix"), ent.get("minutes")
            if not sensor_map:
                cc.reply("Which sensor? (e.g. MXC, P2, STILL)")
                return
            if value is None:
                cc.reply(f"What value should the threshold be for *{sensor_map}*?")
                return
            expires = None if minutes is None else tu.minutes_from_now(minutes)
            control.apply_override(st, sensor_map, value, expires, mk)
            mode_label = f"[{(mk or st.get('current_mode', 'current')).upper()}] "
            cc.reply(f"✅ {mode_label}*{sensor_map}* → `{value}` "
                     f"{'permanently' if minutes is None else f'for {minutes} min'}", color="good")
        elif intent == "reset_threshold":
            sensor_map, mk = ent.get("sensor"), ent.get("mode_prefix")
            if not sensor_map:
                cc.reply("Which sensor to reset?")
                return
            control._overrides_for(st, mk).pop(sensor_map, None)
            cc.reply(f"✅ *{sensor_map}* reset to default.", color="good")
        else:
            _didnt_understand(cc, text)
            return
    except Exception as e:
        log.error("NLP dispatch error: %s", e, exc_info=True)
        _didnt_understand(cc, text, "Type `help` to see all commands.")
        return

    st["last_nlp_command"] = {"input_text": text, "intent": intent, "conf": conf, "ts": time.time()}
    if conf < NLP_UNCERTAIN:
        label = nlp.INTENT_LABELS.get(intent, intent)
        cc.reply(f"_(Auto-detected as: *{label}*. Reply \"yes\" to confirm or \"wrong\" if "
                 f"incorrect — I'll learn from it.)_", color="#888888")
        st.setdefault("nlp_pending", []).append({
            "input_text": text, "intent": intent, "conf": conf, "user_msg_ts": cc.reply_ts,
            "bot_msg_ts": cc.reply_ts, "created": time.time(), "asked_correction": False})
        st["nlp_pending"] = st["nlp_pending"][-20:]


def standalone_wrong(cc: CommandContext) -> None:
    """A bare 'wrong' as a new @mention → reopen the last NLP command."""
    st = cc.st
    last = st.get("last_nlp_command")
    if last and time.time() - last.get("ts", 0) < 600:
        cc.reply(f"Got it, *\"{last['input_text']}\"* was wrong. What did you mean?\n"
                 "Reply in your own words — I'll try to understand.")
        st.setdefault("nlp_pending", []).append({
            "input_text": last["input_text"], "intent": last["intent"], "conf": last["conf"],
            "user_msg_ts": cc.reply_ts, "bot_msg_ts": cc.reply_ts, "created": time.time(),
            "asked_correction": True, "last_seen_ts": cc.reply_ts})
        st["nlp_pending"] = st["nlp_pending"][-20:]
    else:
        cc.reply("No recent command to correct — what would you like to do?")


def check_feedback(st: dict, slack, make_cc, execute) -> None:
    """Poll replies under pending NLP answers and learn from them.

    make_cc(thread_ts) -> CommandContext ; execute(text, thread_ts) re-runs a
    corrected command through the router."""
    pending = st.get("nlp_pending", [])
    if not pending:
        return
    try:
        nlp = _nlp()
    except Exception:
        return
    now = time.time()
    still = []

    def propose(correction_text, entry, cc):
        intent, _, c2 = nlp.classify_command(correction_text)
        if c2 > 0.25:
            label = nlp.INTENT_LABELS.get(intent, intent)
            cc.reply(f"Did you mean *{label}*?\nReply `yes` to confirm or `wrong` to try again.")
            entry["orig_intent"] = entry.get("orig_intent") or entry["intent"]
            entry.update(intent=intent, correction_text=correction_text, awaiting_confirm=True, retry_count=0)
            log.info("NLP proposed: '%s' → %s (conf=%.2f)", entry["input_text"], intent, c2)
            return True
        return False

    for entry in pending:
        if now - entry.get("created", 0) > 600:
            continue
        if not slack.ok:
            still.append(entry)
            continue
        data = slack.get("conversations.replies", {"channel": slack.channel, "ts": entry["user_msg_ts"]})
        if not data.get("ok"):
            still.append(entry)
            continue
        since_ts = entry.get("last_seen_ts") or entry.get("bot_msg_ts") or entry["user_msg_ts"]
        user_replies = [m for m in data.get("messages", [])[1:]
                        if m.get("ts", "0") > since_ts and m.get("user") != slack.bot_user_id]
        if not user_replies:
            still.append(entry)
            continue
        nxt = user_replies[0]
        reply = re.sub(r"<@[A-Z0-9]+>", "", unescape(nxt.get("text", ""))).strip().lower()
        entry["last_seen_ts"] = nxt["ts"]
        cc = make_cc(entry["user_msg_ts"])

        if CONFIRM_RE.search(reply) and not DENY_RE.search(reply):
            src = "corrected" if entry.get("orig_intent") else "confirmed"
            nlp.add_example(entry["input_text"], entry["intent"], source=src, from_intent=entry.get("orig_intent"))
            label = nlp.INTENT_LABELS.get(entry["intent"], entry["intent"])
            cc.reply(f"✅ Got it — running *{label}* now and I'll remember this next time.", color="good")
            execute(entry.get("correction_text") or entry["input_text"], entry["user_msg_ts"])
        elif DENY_RE.search(reply):
            entry["awaiting_confirm"] = False
            entry["correction_text"] = None
            stripped = re.sub(r"^(?:wrong|no|nope|not right|incorrect|不对|错了)[,\s]*(?:i meant?|应该是|是)?\s*",
                              "", reply).strip()
            if not (stripped and propose(stripped, entry, cc)):
                if not entry.get("asked_correction"):
                    cc.reply("Got it, that was wrong. What did you mean?\nReply in your own words — I'll try to understand.")
                entry["asked_correction"] = True
            still.append(entry)
        elif entry.get("asked_correction") and not entry.get("awaiting_confirm"):
            if propose(reply, entry, cc):
                still.append(entry)
            else:
                entry["retry_count"] = entry.get("retry_count", 0) + 1
                if entry["retry_count"] < 2:
                    cc.reply(f"Still not sure what you meant by *\"{entry['input_text']}\"*.\n"
                             "Try the exact command, e.g.:\n"
                             "`pump status` · `temperature reading` · `plot MXC` · `pause alerts`")
                    still.append(entry)
    st["nlp_pending"] = still
