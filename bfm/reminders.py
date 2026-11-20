"""Recurring maintenance reminders: chilled-water filter (every 21 days,
daily nag) and the weekly eyewash-station check (Friday noon, 6-hourly nag).

Both share one skeleton (`Reminder`): a state dict, a "due" rule, a fresh
top-level post every `interval_hours` until a confirming reply appears under
the latest post, then quiet until the next period. Confirmation is checked
by the daemon's reply poller through `confirm_from_replies()`; a thread
reply is `confirm()`ed, so is an @mention command (filter: "I changed the
filter").
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from . import config
from .core import timeutil as tu
from .core.slack import unescape

log = logging.getLogger("bfm.reminders")


class Reminder:
    name = "base"
    state_key = "base"
    confirm_re: re.Pattern = re.compile(r"$^")
    interval_hours: float = 24
    color = "warning"

    # subclasses: default_state(), due(st, now), message(), on_confirm(st, now)
    def state(self, st: dict) -> dict:
        s = st.get(self.state_key)
        if not s:
            s = self.default_state()
            st[self.state_key] = s
        return s

    def default_state(self) -> dict:                  # pragma: no cover
        raise NotImplementedError

    def due(self, st: dict, now: datetime) -> bool:   # pragma: no cover
        raise NotImplementedError

    def message(self) -> str:                         # pragma: no cover
        raise NotImplementedError

    def confirm(self, st: dict, enqueue, reply_ts: str | None = None) -> str:
        """Mark done; returns the thank-you text (also enqueued in-thread)."""
        raise NotImplementedError                     # pragma: no cover

    # ── shared behaviour ─────────────────────────────────────────────────────
    def reminder_ts(self, st: dict) -> str | None:
        return self.state(st).get("reminder_ts")

    def confirm_from_replies(self, st: dict, replies: list[dict], enqueue) -> bool:
        """Given human replies under the latest reminder, confirm if one matches."""
        for reply in replies:
            if self.confirm_re.search(unescape(reply.get("text", "")).lower()):
                self.confirm(st, enqueue, reply_ts=self.reminder_ts(st))
                return True
        return False

    def tick(self, st: dict, enqueue, now: datetime | None = None) -> bool:
        """Post a reminder if due and the interval has elapsed. Returns True if posted."""
        now = now or tu.now()
        if not self.due(st, now):
            return False
        s = self.state(st)
        last = tu.parse_iso(s.get("last_reminded"))
        if last and now - last < timedelta(hours=self.interval_hours):
            return False
        s["last_reminded"] = now.isoformat()
        s["reminder_ts"] = None                 # filled by the sender on success
        enqueue(self.message(), color=self.color, key=f"REMINDER::{self.name}", kind="reminder")
        log.info("%s reminder queued", self.name)
        return True


# ── chilled-water filter ──────────────────────────────────────────────────────

FILTER_DONE_RE = re.compile(
    r"i\s*'?(?:ve|have)?\s*(?:just|already)?\s*(?:changed|replaced|swapped|renewed|done)"
    r"|(?:changed|replaced|swapped|renewed)\s*(?:it|the)?\s*(?:filter)?"
    r"|filter\s*(?:is\s*)?(?:changed|replaced|swapped|renewed|done|new)"
    r"|已经?(?:更换|换了|替换|换好)|换好了|换了新的?|更换完成|已换|换过了",
    re.IGNORECASE)
FILTER_WORD_RE = re.compile(r"filter|滤芯|滤网|滤器|过滤", re.IGNORECASE)


class FilterReminder(Reminder):
    name = "filter"
    state_key = "filter"
    confirm_re = FILTER_DONE_RE

    @property
    def interval_hours(self):
        return config.FILTER_REMINDER_INTERVAL_HOURS

    def default_state(self):
        return {"next_due": (tu.now() + timedelta(days=config.FILTER_INTERVAL_DAYS)).isoformat(),
                "active": False, "last_reminded": None, "reminder_ts": None}

    def due(self, st, now):
        s = self.state(st)
        if not s["active"]:
            nd = tu.parse_iso(s.get("next_due"))
            if nd is None or now < nd:
                return False
            s.update(active=True, last_reminded=None, reminder_ts=None)
        return True

    def message(self):
        return (":droplet: *Chilled water filter — replacement due*\n"
                f"It has been {config.FILTER_INTERVAL_DAYS} days since the last change. "
                "*Please replace the chilled water filter.*\n"
                "_Please reply `I have changed it` (or `filter replaced`) in this thread "
                "*after you have changed it*. I'll post a new reminder once a day until "
                "then — replying `ok` will *not* stop it._")

    def confirm(self, st, enqueue, reply_ts=None, announce=True):
        s = self.state(st)
        s["next_due"] = (tu.now() + timedelta(days=config.FILTER_INTERVAL_DAYS)).isoformat()
        s.update(active=False, last_reminded=None, reminder_ts=None)
        text = (":white_check_mark: *Chilled water filter marked as replaced — thanks!*\n"
                f"Next replacement reminder in *{config.FILTER_INTERVAL_DAYS} days* "
                f"(around *{s['next_due'][:10]}*).")
        if announce:
            enqueue(text, color="good", thread_ts=reply_ts, kind="reply")
        log.info("Filter timer reset — next due %s", s["next_due"])
        return text

    def status_text(self, st) -> str:
        s = self.state(st)
        nd = tu.parse_iso(s.get("next_due"))
        days = (nd - tu.now()).total_seconds() / 86400 if nd else 0
        if s.get("active"):
            return (":droplet: *Chilled water filter: replacement is DUE* "
                    f"(overdue by {abs(days):.1f} days). Reply `I have changed it` once done.")
        return (f":droplet: Chilled water filter: next replacement in *{days:.1f} days* "
                f"(around {s['next_due'][:10]}).")


# ── weekly eyewash check ──────────────────────────────────────────────────────

EYEWASH_YES_RE = re.compile(
    r"\b(yes|yep|yeah|yup|done|checked|confirmed)\b|是的|好了|完成|检查了|查了", re.IGNORECASE)


def eyewash_period_key(now: datetime) -> str:
    """Date of the most recent scheduled slot (Friday 12:00) at or before now.
    The 'week' runs slot-to-slot, not Monday-to-Sunday (the old ISO-week logic
    reminded on Thursday and again Monday)."""
    slot = (now - timedelta(days=(now.weekday() - config.EYEWASH_START_WEEKDAY) % 7)).replace(
        hour=config.EYEWASH_START_HOUR, minute=0, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=7)
    return slot.strftime("%Y-%m-%d")


class EyewashReminder(Reminder):
    name = "eyewash"
    state_key = "eyewash"
    confirm_re = EYEWASH_YES_RE

    @property
    def interval_hours(self):
        return config.EYEWASH_REMINDER_INTERVAL_HOURS

    def default_state(self):
        return {"week": None, "confirmed": False, "last_reminded": None, "reminder_ts": None}

    def due(self, st, now):
        if not config.EYEWASH_SLACK_USER_ID:
            return False
        s = self.state(st)
        key = eyewash_period_key(now)
        if s.get("week") != key:
            s.update(week=key, confirmed=False, last_reminded=None, reminder_ts=None)
        return not s.get("confirmed")

    def message(self):
        return (f":eyes: <@{config.EYEWASH_SLACK_USER_ID}> *Weekly eyewash equipment check*\n"
                "Please check the eyewash station.\n"
                f"_Reply `yes` in this thread once it's done. I'll remind every "
                f"{config.EYEWASH_REMINDER_INTERVAL_HOURS} h until then — after you confirm, "
                "no more reminders until next Friday noon._")

    def confirm(self, st, enqueue, reply_ts=None):
        s = self.state(st)
        s["confirmed"] = True
        text = (":white_check_mark: Thanks — eyewash equipment check confirmed for this week. "
                "I'll remind again next Friday at noon.")
        enqueue(text, color="good", thread_ts=reply_ts, kind="reply")
        log.info("Eyewash check confirmed for %s", s.get("week"))
        return text


ALL_REMINDERS: list[Reminder] = [FilterReminder(), EyewashReminder()]
