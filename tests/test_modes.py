import pytest

from bfm import modes as M
from tests.fakes import FakeCtx, FakeDB, Outbox, base_state, utc


@pytest.mark.parametrize("word,mode", [
    ("cold", "COLD"), ("BASE", "COLD"), ("idle", "IDLE"), ("warm", "IDLE"), ("room", "IDLE"),
    ("trans", "TRANSITIONING"), ("cooling", "TRANSITIONING"), ("auto", "AUTO"), ("bogus", "BOGUS")])
def test_normalise_mode(word, mode):
    assert M.normalise_mode(word) == mode


@pytest.mark.parametrize("text,mode", [
    ("what alarms in cold mode", "COLD"), ("warming up", "TRANSITIONING"), ("warm", "IDLE"),
    ("过渡模式", "TRANSITIONING"), ("低温", "COLD"), ("室温", "IDLE"), ("nothing", None)])
def test_mode_in_text(text, mode):
    assert M.mode_in_text(text) == mode


def _ctx(k):
    db = FakeDB()
    if k is not None:
        db.latest_rows[("double", "50K_TEMPERATURE")] = (k, utc())
    return FakeCtx(db)


@pytest.mark.parametrize("k,mode", [(295.0, "IDLE"), (150.0, "TRANSITIONING"), (40.0, "COLD"), (None, "IDLE")])
def test_detect_mode(k, mode):
    assert M.detect_mode(_ctx(k)) == mode


def test_update_mode_posts_and_wipes_cooldowns():
    st, ob = base_state("IDLE"), Outbox()
    st["last_alert_time"]["X"] = "2026-01-01T00:00:00"
    st["acked_sensors"]["X"] = "2099-01-01T00:00:00"
    assert M.update_mode(st, _ctx(40.0), ob) == "COLD"
    assert st["current_mode"] == "COLD" and st["mode_since"]
    assert ob.msgs[0]["key"] == "MODE" and "IDLE → COLD" in ob.msgs[0]["text"]
    assert st["last_alert_time"] == {} and st["acked_sensors"] == {}
    assert M.update_mode(st, _ctx(40.0), ob) is None          # unchanged → nothing


def test_update_mode_override_wins():
    st, ob = base_state("IDLE"), Outbox()
    st["mode_override"] = "TRANSITIONING"
    assert M.update_mode(st, _ctx(295.0), ob) == "TRANSITIONING"


def test_get_threshold_and_override_keys():
    st = base_state("COLD")
    assert M.get_threshold("MXC_TEMPERATURE", st) == (0.030, None)
    assert M.get_threshold("P1_PRESSURE", base_state("IDLE")) == (None, 1e-5)
    assert M.get_threshold("MXC_TEMPERATURE", base_state("TRANSITIONING")) == (None, None)
    assert M.override_key("COLD") == "threshold_overrides_cold"
    assert M.override_key("IDLE") == "threshold_overrides_idle"
    assert M.override_key(None) == "threshold_overrides_idle"


def test_transition_direction():
    db = FakeDB()
    db.latest_rows[("double", "50K_TEMPERATURE")] = (100.0, utc())
    db.latest_rows[("double", "50K_TEMPERATURE@before")] = (100.2, utc(15))
    assert M.transition_direction(FakeCtx(db)) is None                  # within deadband
    db.latest_rows[("double", "50K_TEMPERATURE@before")] = (90.0, utc(15))
    assert M.transition_direction(FakeCtx(db)) == "warm up"
    db.latest_rows[("double", "50K_TEMPERATURE@before")] = (110.0, utc(15))
    assert M.transition_direction(FakeCtx(db)) == "cool down"
    assert M.transition_direction(FakeCtx(FakeDB())) is None
