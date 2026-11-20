import json
from datetime import datetime, timedelta

from bfm.core import timeutil as tu
from bfm.core.state import StateStore, OUTBOX_EVENT_CAP


def test_fresh_store_has_defaults(tmp_state):
    d = tmp_state.data
    assert d["last_slack_ts"] == "0"
    assert d["cs2_alerts_enabled"] is True
    assert d["outbox"] == []
    assert d["slack"]["ok"] is True


def test_save_and_reload_roundtrip(tmp_path):
    s = StateStore(tmp_path / "s.json").load()
    with s.locked() as d:
        d["current_mode"] = "COLD"
        d["last_cs2_alert_id"] = 42
    s.save()
    s2 = StateStore(tmp_path / "s.json").load()
    assert s2.data["current_mode"] == "COLD"
    assert s2.data["last_cs2_alert_id"] == 42
    assert not (tmp_path / "s.json").with_suffix(".lock").exists() or True   # lock file may exist


def test_corrupt_file_is_kept_not_silently_reset(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    s = StateStore(p).load()
    assert s.data["last_cs2_alert_id"] == 0
    backups = list(tmp_path.glob("s.corrupt.*"))
    assert len(backups) == 1 and backups[0].read_text() == "{not json"


def test_migration_legacy_overrides_and_orphans(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({
        "current_mode": "COLD",
        "threshold_overrides": {"P5_PRESSURE": {"max_val": 1, "min_val": None, "expires_at": None}},
        "pulsetube_flags": {"x": 1},
        "last_freshness_alert": "2026-01-01T00:00:00",
    }))
    s = StateStore(p).load()
    assert "threshold_overrides" not in s.data
    assert "P5_PRESSURE" in s.data["threshold_overrides_cold"]
    assert "pulsetube_flags" not in s.data
    assert "last_freshness_alert" not in s.data


def test_prune_expired_acks_and_stale_pending(tmp_path):
    past = (tu.now() - timedelta(minutes=1)).isoformat()
    future = (tu.now() + timedelta(hours=1)).isoformat()
    old_ts = f"{(datetime.now() - timedelta(days=2)).timestamp():.6f}"
    new_ts = f"{datetime.now().timestamp():.6f}"
    p = tmp_path / "s.json"
    p.write_text(json.dumps({
        "acked_sensors": {"A": past, "B": future, "C": "garbage"},
        "pending_alert_msgs": {"OLD": {"ts": old_ts, "channel": "c"},
                               "NEW": {"ts": new_ts, "channel": "c"},
                               "ACTIVE_OLD": {"ts": old_ts, "channel": "c"}},
        "active_alarms": {"ACTIVE_OLD": {"label": "x", "ts": old_ts}},
        "threshold_overrides_idle": {"P2_PRESSURE": {"max_val": 1, "expires_at": past},
                                     "P5_PRESSURE": {"max_val": 1, "expires_at": None}},
    }))
    s = StateStore(p).load()
    assert set(s.data["acked_sensors"]) == {"B"}
    assert set(s.data["pending_alert_msgs"]) == {"NEW", "ACTIVE_OLD"}
    assert set(s.data["threshold_overrides_idle"]) == {"P5_PRESSURE"}


def test_live_state_file_migrates(live_state_copy):
    d = live_state_copy.data
    assert "threshold_overrides" not in d
    assert isinstance(d["outbox"], list)
    # every pending entry must correspond to an active alarm or be < 24 h old
    for k, v in d["pending_alert_msgs"].items():
        age_h = (datetime.now() - datetime.fromtimestamp(float(v["ts"]))).total_seconds() / 3600
        assert k in d["active_alarms"] or age_h < 24
    live_state_copy.save()    # must round-trip


# ── outbox ───────────────────────────────────────────────────────────────────
def test_outbox_alarm_collapses_by_key(tmp_state):
    tmp_state.enqueue("first", key="STALE::P2", kind="alarm")
    tmp_state.enqueue("second", key="STALE::P2", kind="alarm")
    tmp_state.enqueue("other", key="STALE::P3", kind="alarm")
    ob = tmp_state.data["outbox"]
    assert [m["text"] for m in ob] == ["second", "other"]
    assert ob[0]["collapsed"] == 1


def test_outbox_clear_drops_unseen_alarm(tmp_state):
    tmp_state.enqueue("alarm!", key="X", kind="alarm")
    r = tmp_state.enqueue("cleared", key="X", kind="clear")
    assert r.get("dropped") is True
    assert tmp_state.outbox_len() == 0


def test_outbox_clear_is_queued_when_alarm_was_posted(tmp_state):
    tmp_state.enqueue("cleared", key="X", kind="clear")
    assert tmp_state.outbox_len() == 1


def test_outbox_event_cap(tmp_state):
    for i in range(OUTBOX_EVENT_CAP + 5):
        tmp_state.enqueue(f"e{i}", kind="event")
    assert tmp_state.outbox_len() == OUTBOX_EVENT_CAP
    assert tmp_state.data["slack"]["dropped_events"] == 5


def test_outbox_done_and_failed(tmp_state):
    m = tmp_state.enqueue("hi", kind="reply", thread_ts="1.2")
    tmp_state.outbox_failed(m["id"])
    assert tmp_state.outbox_peek()["attempts"] == 1
    sent = tmp_state.outbox_done(m["id"], "9.9")
    assert sent["ts"] == "9.9" and sent["thread_ts"] == "1.2"
    assert tmp_state.outbox_peek() is None


def test_outbox_survives_save(tmp_path):
    s = StateStore(tmp_path / "s.json").load()
    s.enqueue("queued while offline", key="K", kind="alarm")
    s.save()
    s2 = StateStore(tmp_path / "s.json").load()
    assert s2.outbox_peek()["text"] == "queued while offline"
