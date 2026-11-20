from datetime import datetime, timedelta

from bfm import config
from bfm import reminders as R
from bfm.core import timeutil as tu
from tests.fakes import Outbox, base_state


# ── eyewash period ───────────────────────────────────────────────────────────
def test_eyewash_period_runs_friday_noon_to_friday_noon():
    assert R.eyewash_period_key(datetime(2026, 10, 19, 9, 0)) == "2026-10-16"    # Mon → prev Fri
    assert R.eyewash_period_key(datetime(2026, 10, 23, 11, 59)) == "2026-10-16"  # Fri before noon
    assert R.eyewash_period_key(datetime(2026, 10, 23, 12, 0)) == "2026-10-23"   # Fri noon → new
    assert R.eyewash_period_key(datetime(2026, 10, 26, 9, 0)) == "2026-10-23"


def test_eyewash_tick_confirm_and_quiet_until_next_period():
    st, ob = base_state(), Outbox()
    r = R.EyewashReminder()
    fri = datetime(2026, 11, 20, 12, 0)
    assert r.tick(st, ob, now=fri) is True
    assert ob.msgs[0]["kind"] == "reminder" and config.EYEWASH_SLACK_USER_ID in ob.msgs[0]["text"]
    assert r.tick(st, ob, now=fri + timedelta(hours=5)) is False       # within 6 h
    assert r.tick(st, ob, now=fri + timedelta(hours=6, minutes=1)) is True
    st["eyewash"]["reminder_ts"] = "1.5"
    assert r.confirm_from_replies(st, [{"user": "U1", "text": "Yes, done"}], ob) is True
    assert st["eyewash"]["confirmed"] is True
    assert ob.msgs[-1]["thread_ts"] == "1.5" and "confirmed" in ob.msgs[-1]["text"]
    assert r.tick(st, ob, now=fri + timedelta(days=3)) is False          # same period: quiet
    assert r.tick(st, ob, now=fri + timedelta(days=7)) is True           # next Friday noon


def test_eyewash_ok_does_not_confirm():
    st, ob = base_state(), Outbox()
    r = R.EyewashReminder()
    r.tick(st, ob, now=datetime(2026, 11, 20, 12, 0))
    assert r.confirm_from_replies(st, [{"user": "U1", "text": "ok"}], ob) is False


def test_eyewash_disabled(monkeypatch):
    monkeypatch.setattr(config, "EYEWASH_SLACK_USER_ID", None)
    assert R.EyewashReminder().tick(base_state(), Outbox(), now=datetime(2026, 11, 20, 12, 0)) is False


# ── filter ───────────────────────────────────────────────────────────────────
def test_filter_due_daily_until_confirmed():
    st, ob = base_state(), Outbox()
    r = R.FilterReminder()
    now = tu.now()
    assert r.tick(st, ob, now=now) is False                   # fresh: 21 days away
    st["filter"]["next_due"] = (now - timedelta(minutes=1)).isoformat()
    assert r.tick(st, ob, now=now) is True
    assert st["filter"]["active"] is True and "replacement due" in ob.msgs[0]["text"]
    assert r.tick(st, ob, now=now + timedelta(hours=23)) is False
    assert r.tick(st, ob, now=now + timedelta(hours=25)) is True
    st["filter"]["reminder_ts"] = "2.2"
    assert r.confirm_from_replies(st, [{"text": "ok"}], ob) is False
    assert r.confirm_from_replies(st, [{"text": "I have changed it"}], ob) is True
    assert st["filter"]["active"] is False
    nd = tu.parse_iso(st["filter"]["next_due"])
    assert abs((nd - tu.now()).days - config.FILTER_INTERVAL_DAYS) <= 1
    assert ob.msgs[-1]["thread_ts"] == "2.2"


def test_filter_status_text():
    st = base_state()
    r = R.FilterReminder()
    assert "next replacement in" in r.status_text(st)
    st["filter"]["active"] = True
    st["filter"]["next_due"] = (tu.now() - timedelta(days=2)).isoformat()
    assert "DUE" in r.status_text(st)


def test_filter_done_regex_variants():
    for t in ["i changed it", "I've replaced the filter", "filter replaced", "换好了", "已更换", "replaced"]:
        assert R.FILTER_DONE_RE.search(t.lower()), t
    for t in ["ok", "yes", "I changed the threshold"]:
        assert not (R.FILTER_DONE_RE.search(t.lower()) and R.FILTER_WORD_RE.search(t)), t
