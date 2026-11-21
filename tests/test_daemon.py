"""Daemon pieces that don't need a database: sender/outbox drain, offline
digest, ack polling, silence-request parsing, summary scheduling."""
from datetime import timedelta

import pytest

from bfm import config, daemon as D
from bfm.core import timeutil as tu
from bfm.core.state import StateStore


class StubSlack:
    """Scriptable SlackClient stand-in."""
    channel = "C1"
    bot_user_id = config.SLACK_BOT_USER_ID

    def __init__(self):
        self.ok = True
        self.posts = []
        self.fail_next = 0
        self.reactions_by_ts = {}
        self.replies_by_ts = {}
        self.history_msgs = []
        self.gone_ts = set()               # posts Slack no longer knows
        self.last_error = None

    def probe(self, force=False):
        return self.ok

    def post_now(self, text, color="danger", thread_ts=None, footer=None):
        if self.fail_next:
            self.fail_next -= 1
            return None
        if not self.ok:
            return None
        self.posts.append({"text": text, "color": color, "thread_ts": thread_ts})
        return f"{1000 + len(self.posts)}.0"

    def reactions(self, ts):
        self.last_error = "message_not_found" if ts in self.gone_ts else None
        return self.reactions_by_ts.get(ts, set())

    def replies(self, ts):
        self.last_error = "thread_not_found" if ts in self.gone_ts else None
        return self.replies_by_ts.get(ts, [])

    def history(self, oldest, limit=50):
        return []

    def get(self, endpoint, params):
        return {"ok": True, "messages": []}

    def upload_image(self, *a, **k):
        return True


@pytest.fixture
def d(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SENDER_PACE_S", 0)
    dm = D.Daemon.__new__(D.Daemon)
    dm.dry_run = False
    dm.store = StateStore(tmp_path / "s.json").load()
    dm.slack = StubSlack()
    dm.stop = __import__("threading").Event()
    dm.cs2 = dm.ln2 = None
    dm.last_cycle = {}
    dm.cycle_count = 0
    dm.decisions = []
    dm._last_reply_poll = -1e9
    return dm


# ── sender ───────────────────────────────────────────────────────────────────
def test_drain_sends_in_order_and_records_ts(d):
    d.store.enqueue("alarm A", key="A", kind="alarm")
    d.store.enqueue("event 1", kind="event")
    d.store.enqueue("reply", kind="reply", thread_ts="5.5")
    with d.store.locked() as st:
        st["active_alarms"]["A"] = {"label": "A", "ts": None}
    assert d.drain_outbox() == 3
    assert [p["text"] for p in d.slack.posts] == ["alarm A", "event 1", "reply"]
    assert d.slack.posts[2]["thread_ts"] == "5.5"
    st = d.store.data
    assert st["pending_alert_msgs"]["A"]["ts"] == "1001.0"
    assert st["active_alarms"]["A"]["ts"] == "1001.0"
    assert st["outbox"] == []


def test_drain_stops_when_slack_down_and_keeps_queue(d):
    d.store.enqueue("x", key="X", kind="alarm")
    d.slack.ok = False
    assert d.drain_outbox() == 0
    assert d.store.outbox_len() == 1
    d.slack.ok = True
    assert d.drain_outbox() == 1


def test_drain_retries_transient_then_drops_after_5(d):
    d.store.enqueue("x", kind="event")
    d.slack.fail_next = 2
    assert d.drain_outbox() == 1                      # two failures, then success
    d.store.enqueue("y", kind="event")
    d.slack.fail_next = 6
    assert d.drain_outbox() == 0
    assert d.store.outbox_len() == 0                  # dropped after 5 attempts


def test_reminder_ts_written_back(d):
    from bfm.reminders import EyewashReminder
    d.store.enqueue("eyewash", key="REMINDER::eyewash", kind="reminder")
    d.drain_outbox()
    assert EyewashReminder().state(d.store.data)["reminder_ts"] == "1001.0"


# ── offline digest ───────────────────────────────────────────────────────────
def test_offline_digest_posted_first_after_long_outage(d):
    with d.store.locked() as st:
        st["slack"]["down_since"] = (tu.now() - timedelta(hours=3)).isoformat()
        st["slack"]["dropped_events"] = 4
        st["slack"]["skipped_commands"] = 2
        st["active_alarms"]["K"] = {"label": "Pump X: error", "ts": None}
    d.store.enqueue("a1", key="K", kind="alarm")
    d.store.enqueue("a1 again", key="K", kind="alarm")      # collapsed
    d.store.enqueue("e", kind="event")
    d.drain_outbox()
    texts = [p["text"] for p in d.slack.posts]
    assert texts[0].startswith(":satellite: *Monitor was offline from Slack for 3.0 h*")
    assert "1 alarm(s), 1 event(s) (1 repeat(s) collapsed)" in texts[0]
    assert "Dropped 4 older event" in texts[0] and "Ignored 2 command(s)" in texts[0]
    assert "Pump X: error" in texts[0]
    assert texts[1:] == ["a1 again", "e"]
    assert d.store.data["slack"]["down_since"] is None


def test_short_outage_has_no_digest(d):
    with d.store.locked() as st:
        st["slack"]["down_since"] = (tu.now() - timedelta(minutes=3)).isoformat()
    d.store.enqueue("e", kind="event")
    d.drain_outbox()
    assert [p["text"] for p in d.slack.posts] == ["e"]


# ── ack polling ──────────────────────────────────────────────────────────────
def test_poll_replies_reaction_and_thread_silence(d):
    with d.store.locked() as st:
        st["pending_alert_msgs"] = {"A": {"ts": "1.0", "channel": "c"}, "B": {"ts": "2.0", "channel": "c"},
                                    "C": {"ts": "3.0", "channel": "c"}}
    d.slack.reactions_by_ts["1.0"] = {"white_check_mark"}
    d.slack.replies_by_ts["2.0"] = [{"user": "U1", "text": "silent for 2h"}]
    d.slack.replies_by_ts["3.0"] = [{"user": "U1", "text": "thanks, looking"}]
    d.poll_replies()
    st = d.store.data
    assert tu.is_future(st["acked_sensors"]["A"]) and "A" not in st["pending_alert_msgs"]
    assert "B" not in st["pending_alert_msgs"]
    assert "C" in st["pending_alert_msgs"]                 # no silence word → untouched
    replies = [m for m in st["outbox"] if m["kind"] == "reply"]
    assert any("silencing *A* for *10 minutes*" in m["text"] and m["thread_ts"] == "1.0" for m in replies)
    assert any("silencing *B* for *2 hours*" in m["text"] for m in replies)


def test_poll_replies_confirms_reminders(d):
    from bfm.reminders import EyewashReminder, FilterReminder
    with d.store.locked() as st:
        EyewashReminder().state(st).update(week="2026-11-20", reminder_ts="7.0", confirmed=False)
        FilterReminder().state(st).update(active=True, reminder_ts="8.0")
    d.slack.replies_by_ts["7.0"] = [{"user": "U1", "text": "yes"}]
    d.slack.replies_by_ts["8.0"] = [{"user": "U1", "text": "I have changed it"}]
    d.poll_replies()
    assert d.store.data["eyewash"]["confirmed"] is True
    assert d.store.data["filter"]["active"] is False


def test_poll_replies_forgets_deleted_posts(d):
    from bfm.reminders import EyewashReminder
    with d.store.locked() as st:
        st["pending_alert_msgs"] = {"A": {"ts": "1.0", "channel": "c"}, "B": {"ts": "2.0", "channel": "c"}}
        EyewashReminder().state(st).update(week="2026-11-20", reminder_ts="7.0", confirmed=False)
    d.slack.gone_ts = {"1.0", "7.0"}
    d.poll_replies()
    st = d.store.data
    assert "A" not in st["pending_alert_msgs"] and "B" in st["pending_alert_msgs"]
    assert "A" not in st["acked_sensors"]
    assert st["eyewash"]["reminder_ts"] is None


def test_poll_replies_is_throttled(d, monkeypatch):
    monkeypatch.setattr(config, "REPLY_POLL_S", 20)
    with d.store.locked() as st:
        st["pending_alert_msgs"] = {"A": {"ts": "1.0", "channel": "c"}}
    calls = []
    d.slack.reactions = lambda ts: calls.append(ts) or set()
    d.poll_replies(); d.poll_replies(); d.poll_replies()
    assert len(calls) == 1                                  # 2nd/3rd within 20 s → skipped
    d._last_reply_poll -= 21
    d.poll_replies()
    assert len(calls) == 2
    d.poll_replies(force=True)
    assert len(calls) == 3


# ── silence request parsing ──────────────────────────────────────────────────
@pytest.mark.parametrize("text,label", [
    ("ok", "10 minutes"), ("OK", "10 minutes"), ("silent for 2h", "2 hours"), ("mute 30 min", "30 minutes"),
    ("silence 3 days", "3 days"), ("静音3天", "3 days"), ("2h", "2 hours"), ("snooze", "10 minutes"),
    ("silent forever", "permanently"), ("forever", "permanently"), ("ack please", "10 minutes")])
def test_silence_request(text, label):
    r = D.silence_request(text)
    assert r is not None and r[1] == label
    if label == "permanently":
        assert r[0] == tu.FOREVER_TS


@pytest.mark.parametrize("text", ["thanks", "looking into it", "", "what happened?", "12"])
def test_silence_request_none(text):
    assert D.silence_request(text) is None


# ── summary scheduling ───────────────────────────────────────────────────────
def test_scheduled_summary_once_per_slot_and_skips_when_late(d, monkeypatch):
    monkeypatch.setattr("bfm.summary.generate_summary", lambda db, st: "SUMMARY")
    now = tu.now().replace(hour=8, minute=5, second=0, microsecond=0)
    monkeypatch.setattr(tu, "now", lambda: now)
    with d.store.locked() as st:
        d._scheduled_summary(st)
        assert st["last_summary_slot"] == now.strftime("%Y-%m-%dT08")
        d._scheduled_summary(st)
    assert sum(1 for m in d.store.data["outbox"] if m["text"] == "SUMMARY") == 1
    late = now.replace(hour=21, minute=30)
    monkeypatch.setattr(tu, "now", lambda: late)
    with d.store.locked() as st:
        d._scheduled_summary(st)                       # 20:00 slot is 90 min late → skipped
        assert st["last_summary_slot"] == late.strftime("%Y-%m-%dT20")
    assert sum(1 for m in d.store.data["outbox"] if m["text"] == "SUMMARY") == 1


def test_scheduled_summary_not_rearmed_by_earlier_skipped_slot(d, monkeypatch):
    """Shadow run 2026-11-20: daemon started 18:05, 08:00 slot already too late;
    at 20:00 the summary was queued every minute because the 08:00 'late'
    marker overwrote last_summary_slot each cycle."""
    monkeypatch.setattr("bfm.summary.generate_summary", lambda db, st: "SUMMARY")
    base = tu.now().replace(hour=18, minute=5, second=0, microsecond=0)
    monkeypatch.setattr(tu, "now", lambda: base)
    with d.store.locked() as st:
        d._scheduled_summary(st)                       # 08:00 is 10 h late → skipped
        assert st["last_summary_slot"] == base.strftime("%Y-%m-%dT08")
    for minute in range(0, 61):
        t = base.replace(hour=20, minute=0) + timedelta(minutes=minute)
        monkeypatch.setattr(tu, "now", lambda t=t: t)
        with d.store.locked() as st:
            d._scheduled_summary(st)
    assert sum(1 for m in d.store.data["outbox"] if m["text"] == "SUMMARY") == 1
    assert d.store.data["last_summary_slot"] == base.strftime("%Y-%m-%dT20")
    nxt = (base + timedelta(days=1)).replace(hour=8, minute=0)   # next morning fires once
    monkeypatch.setattr(tu, "now", lambda: nxt)
    with d.store.locked() as st:
        d._scheduled_summary(st); d._scheduled_summary(st)
    assert sum(1 for m in d.store.data["outbox"] if m["text"] == "SUMMARY") == 2
    assert d.store.data["last_summary_slot"] == nxt.strftime("%Y-%m-%dT08")


def test_scheduled_summary_fresh_state_just_after_slot(d, monkeypatch):
    monkeypatch.setattr("bfm.summary.generate_summary", lambda db, st: "SUMMARY")
    t = tu.now().replace(hour=20, minute=0, second=30, microsecond=0)
    monkeypatch.setattr(tu, "now", lambda: t)
    with d.store.locked() as st:
        d._scheduled_summary(st)
    assert sum(1 for m in d.store.data["outbox"] if m["text"] == "SUMMARY") == 1
