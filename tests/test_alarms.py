from datetime import timedelta

import pytest

from bfm import alarms as A
from bfm import config
from bfm.core import timeutil as tu
from tests.fakes import FakeCtx, FakeDB, Outbox, base_state, utc


# ── engine ───────────────────────────────────────────────────────────────────
def test_engine_alarm_then_cooldown_then_clear():
    st, ob = base_state(), Outbox()
    eng = A.AlarmEngine(st, ob)
    v = A.Verdict("K", A.ALARM, "label", "msg")
    assert eng.apply(v) is True
    assert st["active_alarms"]["K"]["label"] == "label"
    assert "K" in st["last_alert_time"]
    assert eng.apply(v) is False                     # within cooldown
    assert len(ob.msgs) == 1 and ob.msgs[0]["kind"] == "alarm"
    # simulate the sender storing the ts
    st["active_alarms"]["K"]["ts"] = "1.1"
    st["pending_alert_msgs"]["K"] = {"ts": "1.1", "channel": "c"}
    eng.apply(A.Verdict("K", A.OK, clear_note="back"))
    assert "K" not in st["active_alarms"] and "K" not in st["pending_alert_msgs"]
    assert "K" not in st["last_alert_time"]
    kinds = [m["kind"] for m in ob.msgs]
    assert kinds == ["alarm", "clear", "reply"]        # channel + thread
    assert ob.msgs[1]["text"] == ":white_check_mark: *Cleared* — back"
    assert ob.msgs[2]["thread_ts"] == "1.1"


def test_engine_force_bypasses_cooldown():
    st, ob = base_state(), Outbox()
    eng = A.AlarmEngine(st, ob)
    eng.apply(A.Verdict("K", A.ALARM, "l", "m1"))
    assert eng.apply(A.Verdict("K", A.ALARM, "l", "m2", force=True)) is True
    assert ob.texts() == ["m1", "m2"]


def test_engine_silenced_suppresses_alarm_and_clear():
    st, ob = base_state(), Outbox()
    st["acked_sensors"]["K"] = (tu.now() + timedelta(hours=1)).isoformat()
    eng = A.AlarmEngine(st, ob)
    assert eng.apply(A.Verdict("K", A.ALARM, "l", "m")) is False
    assert "K" in st["active_alarms"]                  # still tracked as active
    eng.apply(A.Verdict("K", A.OK))
    assert ob.msgs == []                               # quiet recovery while silenced


def test_engine_reset_silence_on_clear():
    st, ob = base_state(), Outbox()
    st["acked_sensors"]["FRESHNESS"] = tu.FOREVER_TS
    eng = A.AlarmEngine(st, ob)
    eng.apply(A.Verdict("FRESHNESS", A.OK, reset_silence_on_clear=True))
    assert "FRESHNESS" not in st["acked_sensors"]


def test_engine_paused_consumes_cooldown_but_posts_nothing():
    st, ob = base_state(), Outbox()
    st["alerts_paused"] = True
    eng = A.AlarmEngine(st, ob)
    assert eng.apply(A.Verdict("K", A.ALARM, "l", "m")) is False
    assert "K" in st["last_alert_time"] and ob.msgs == []
    eng.apply(A.Verdict("K", A.OK))
    assert ob.msgs == []                               # no "Cleared" while paused


def test_engine_unknown_leaves_state():
    st, ob = base_state(), Outbox()
    eng = A.AlarmEngine(st, ob)
    eng.apply(A.Verdict("K", A.ALARM, "l", "m"))
    eng.apply(A.Verdict("K", A.UNKNOWN))
    assert "K" in st["active_alarms"] and len(ob.msgs) == 1


# ── thresholds ───────────────────────────────────────────────────────────────
def _ctx_with(mapping, value, table="double", minutes_ago=0.0):
    db = FakeDB()
    db.latest_rows[(table, mapping)] = (value, utc(minutes_ago))
    return FakeCtx(db)


def test_thresholds_cold_max_and_clear():
    st = base_state("COLD")
    ctx = _ctx_with("MXC_TEMPERATURE", 0.045)
    vs = {v.key: v for v in A.Thresholds().evaluate(ctx, st)}
    assert vs["MXC_TEMPERATURE"].status == A.ALARM
    assert "MXC temperature > 30 mK" in vs["MXC_TEMPERATURE"].message
    assert "[COLD mode]" in vs["MXC_TEMPERATURE"].message
    ctx = _ctx_with("MXC_TEMPERATURE", 0.010)
    vs = {v.key: v for v in A.Thresholds().evaluate(ctx, st)}
    assert vs["MXC_TEMPERATURE"].status == A.OK


def test_thresholds_idle_min_uses_min_description():
    st = base_state("IDLE")
    ctx = _ctx_with("P1_PRESSURE", 1e-6)
    vs = {v.key: v for v in A.Thresholds().evaluate(ctx, st)}
    assert vs["P1_PRESSURE"].status == A.ALARM
    assert "leak check" in vs["P1_PRESSURE"].message


def test_thresholds_transitioning_checks_nothing():
    st = base_state("TRANSITIONING")
    assert A.Thresholds().evaluate(_ctx_with("MXC_TEMPERATURE", 5), st) == []


def test_thresholds_override_and_expiry():
    st = base_state("COLD")
    st["threshold_overrides_cold"]["MXC_TEMPERATURE"] = {
        "max_val": 0.1, "min_val": None, "expires_at": (tu.now() + timedelta(hours=1)).isoformat()}
    ctx = _ctx_with("MXC_TEMPERATURE", 0.045)
    assert {v.key: v.status for v in A.Thresholds().evaluate(ctx, st)}["MXC_TEMPERATURE"] == A.OK
    st["threshold_overrides_cold"]["MXC_TEMPERATURE"]["expires_at"] = (tu.now() - timedelta(minutes=1)).isoformat()
    assert {v.key: v.status for v in A.Thresholds().evaluate(ctx, st)}["MXC_TEMPERATURE"] == A.ALARM
    assert "MXC_TEMPERATURE" not in st["threshold_overrides_cold"]


# ── staleness ────────────────────────────────────────────────────────────────
def _stale_ctx(ages: dict, devices=None, bools=None):
    db = FakeDB()
    for mp, label, _m, _c in config.STALENESS_SENSORS:
        db.max_time_[mp] = utc(ages.get(mp, 0.5))
    db.devices_ = devices or {}
    for mp, val in (bools or {}).items():
        db.latest_rows[("boolean", mp)] = (val, utc())
    return FakeCtx(db)


def test_staleness_certain_vs_maybe_wording_in_cold():
    st = base_state("COLD")
    ctx = _stale_ctx({"STILL_TEMPERATURE": 12, "P2_PRESSURE": 90})
    vs = {v.key: v for v in A.Staleness().evaluate(ctx, st)}
    assert vs["STALE::STILL_TEMPERATURE"].status == A.ALARM
    assert "Sensor not updating — Still" in vs["STALE::STILL_TEMPERATURE"].message
    assert "expected within 5 min" in vs["STALE::STILL_TEMPERATURE"].message
    assert vs["STALE::P2_PRESSURE"].status == A.ALARM
    assert "no update for a while" in vs["STALE::P2_PRESSURE"].message
    assert vs["STALE::4K_TEMPERATURE"].status == A.OK


def test_staleness_idle_flat_24h_limit():
    st = base_state("IDLE")
    ctx = _stale_ctx({"STILL_TEMPERATURE": 600})           # 10 h old: fine when IDLE
    vs = {v.key: v.status for v in A.Staleness().evaluate(ctx, st)}
    assert vs["STALE::STILL_TEMPERATURE"] == A.OK
    ctx = _stale_ctx({"STILL_TEMPERATURE": 1500})          # 25 h → alarm
    vs = {v.key: v.status for v in A.Staleness().evaluate(ctx, st)}
    assert vs["STALE::STILL_TEMPERATURE"] == A.ALARM


def test_staleness_global_outage_guard():
    st = base_state("COLD")
    ctx = _stale_ctx({mp: 30 for mp, *_ in config.STALENESS_SENSORS})
    assert A.Staleness().evaluate(ctx, st) == []


def test_staleness_out_of_range_and_pump_off_exemptions():
    st = base_state("COLD")
    ctx = _stale_ctx({"P3_PRESSURE": 500, "B2_TEMPERATURE": 500},
                     devices={"plc.IO.P3": {"datetime": None, "values": {"bWarningUnderrange": True}}},
                     bools={"B2_ENABLED": False})
    vs = {v.key: v for v in A.Staleness().evaluate(ctx, st)}
    assert vs["STALE::P3_PRESSURE"].status == A.OK and "out of range" in vs["STALE::P3_PRESSURE"].clear_note
    assert vs["STALE::B2_TEMPERATURE"].status == A.OK and "pump is off" in vs["STALE::B2_TEMPERATURE"].clear_note


def test_staleness_no_data_at_all():
    st = base_state("COLD")
    ctx = _stale_ctx({})
    ctx.cs2.max_time_["4K_TEMPERATURE"] = None
    vs = {v.key: v for v in A.Staleness().evaluate(ctx, st)}
    assert "no readings at all" in vs["STALE::4K_TEMPERATURE"].message


# ── freshness ────────────────────────────────────────────────────────────────
def test_freshness():
    st = base_state()
    db = FakeDB(); db.global_max_time = utc(2)
    assert A.Freshness().evaluate(FakeCtx(db), st)[0].status == A.OK
    db.global_max_time = utc(9)
    v = A.Freshness().evaluate(FakeCtx(db), st)[0]
    assert v.status == A.ALARM and "9 min old" in v.message
    db.global_max_time = None
    assert "database empty" in A.Freshness().evaluate(FakeCtx(db), st)[0].message


# ── transitioning ────────────────────────────────────────────────────────────
def test_transitioning_pulse_tube_off_with_direction():
    st = base_state("TRANSITIONING")
    db = FakeDB()
    db.latest_rows[("double", "50K_TEMPERATURE")] = (150.0, utc())
    db.latest_rows[("double", "50K_TEMPERATURE@before")] = (160.0, utc(15))
    db.latest_rows[("boolean", "PULSE_TUBE_ENABLED")] = (False, utc(3))
    vs = A.Transitioning().evaluate(FakeCtx(db), st)
    assert st["transition_direction"] == "cool down"
    assert vs[0].status == A.ALARM and "(cool down)" in vs[0].message and "cooling down" in vs[0].message
    db.latest_rows[("boolean", "PULSE_TUBE_ENABLED")] = (True, utc())
    assert A.Transitioning().evaluate(FakeCtx(db), st)[0].status == A.OK


def test_transitioning_not_in_mode():
    st = base_state("COLD"); st["transition_direction"] = "warm up"
    assert A.Transitioning().evaluate(FakeCtx(), st) == []
    assert "transition_direction" not in st


# ── air pressure ─────────────────────────────────────────────────────────────
def _air_ctx(inp, reg=492_000):
    db = FakeDB()
    db.devices_[config.AIR_PRESSURE_DEVICE] = {"datetime": None, "values": {
        "fInputAirPressure": inp, "fRegulatorAirPressure": reg}}
    return FakeCtx(db)


def test_air_pressure_warning_then_escalation_forces():
    st = base_state()
    vs = {v.key: v for v in A.AirPressure().evaluate(_air_ctx(600_000), st)}
    assert vs["fInputAirPressure"].status == A.ALARM and vs["fInputAirPressure"].level == "WARNING"
    assert vs["fInputAirPressure"].force is False
    assert "600 kPa" in vs["fInputAirPressure"].message
    vs = {v.key: v for v in A.AirPressure().evaluate(_air_ctx(500_000), st)}
    assert vs["fInputAirPressure"].level == "CRITICAL" and vs["fInputAirPressure"].force is True
    vs = {v.key: v for v in A.AirPressure().evaluate(_air_ctx(500_000), st)}
    assert vs["fInputAirPressure"].force is False     # already critical
    vs = {v.key: v for v in A.AirPressure().evaluate(_air_ctx(700_000), st)}
    assert vs["fInputAirPressure"].status == A.OK and "fInputAirPressure" not in st["air_pressure_level"]
    assert vs["fRegulatorAirPressure"].status == A.OK


# ── device health ────────────────────────────────────────────────────────────
def test_device_faults_running_gate_and_ignore():
    js = {"bCompressorRunning": False, "bErrorOilRunningHigh": True,
          "statusInfo": {"errors": ["E1"], "warnings": []}}
    assert A.device_faults(js) == (["E1"], [])
    js["bCompressorRunning"] = True
    assert A.device_faults(js) == (["E1", "Oil Running High"], [])
    assert A.device_faults({"bWarningUnderrange": True, "bWarningOther": True}) == ([], ["Other"])
    assert A.device_faults({"statusInfo": {"errorBit": True}}) == (["error bit set"], [])


def test_device_health_change_forces_and_clears():
    st = base_state()
    db = FakeDB()
    db.devices_["plc.X"] = {"datetime": None, "values": {
        "instrumentInfo": {"name": "Pump X"}, "bErrorA": True}}
    ctx = FakeCtx(db)
    vs = {v.key: v for v in A.DeviceHealth().evaluate(ctx, st)}
    v = vs["DEVICE::plc.X::error"]
    assert v.status == A.ALARM and v.force is True and "Pump X: error" in v.message and "• A" in v.message
    assert vs["DEVICE::plc.X::warning"].status == A.OK
    vs = {v.key: v for v in A.DeviceHealth().evaluate(ctx, st)}
    assert vs["DEVICE::plc.X::error"].force is False              # same fault set
    db.devices_["plc.X"]["values"]["bErrorB"] = True
    vs = {v.key: v for v in A.DeviceHealth().evaluate(ctx, st)}
    assert vs["DEVICE::plc.X::error"].force is True and "errors*" in vs["DEVICE::plc.X::error"].message
    db.devices_["plc.X"]["values"] = {"instrumentInfo": {"name": "Pump X"}}
    vs = {v.key: v for v in A.DeviceHealth().evaluate(ctx, st)}
    assert vs["DEVICE::plc.X::error"].status == A.OK and "resolved" in vs["DEVICE::plc.X::error"].clear_note
    assert st["device_health"] == {}


# ── dew point / pulse tube ───────────────────────────────────────────────────
def _pt_ctx(coolant_k=290.0, oil_k=300.0, running=True, ln2=None):
    db = FakeDB()
    db.devices_[config.PT_DEVICE] = {"datetime": None, "values": {
        "fCoolantInTemp": coolant_k, "fOilTemp": oil_k, "bCompressorRunning": running}}
    l = FakeDB()
    if ln2:
        l.ln2_rows = [ln2]
    return FakeCtx(db, l)


def test_dew_point_formula():
    assert round(A.dew_point(20.0, 50.0), 1) == 9.3
    assert A.dew_point(None, 50) is None and A.dew_point(20, 0) is None


def test_coolant_dewpoint_alarm_and_failsafe():
    st = base_state()
    ctx = _pt_ctx(coolant_k=273.15 + 5, ln2=(utc(), 10.0, 25.0, 80.0))   # dp ≈ 21.3 °C
    v = A.CoolantDewpoint().evaluate(ctx, st)[0]
    assert v.status == A.ALARM and "coolant-in below dew point" in v.message and "`5.0 °C`" in v.message
    ctx = _pt_ctx(coolant_k=273.15 + 25, ln2=(utc(), 10.0, 25.0, 80.0))
    assert A.CoolantDewpoint().evaluate(ctx, st)[0].status == A.OK
    assert A.CoolantDewpoint().evaluate(_pt_ctx(ln2=None), st) == []     # no LN2 → fail-safe


def test_pulsetube_temps_gated_on_running():
    st = base_state()
    vs = {v.key: v for v in A.PulsetubeTemps().evaluate(_pt_ctx(coolant_k=273.15 + 20, oil_k=273.15 + 45), st)}
    assert vs["PT_COOLANT_IN_HIGH"].status == A.ALARM and "20.0 °C" in vs["PT_COOLANT_IN_HIGH"].message
    assert vs["PT_OIL_HIGH"].status == A.ALARM
    vs = {v.key: v for v in A.PulsetubeTemps().evaluate(_pt_ctx(coolant_k=273.15 + 20, running=False), st)}
    assert vs["PT_COOLANT_IN_HIGH"].status == A.OK and "compressor off" in vs["PT_COOLANT_IN_HIGH"].clear_note


# ── cold cathode ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode,on,expect", [
    ("IDLE", True, A.ALARM), ("IDLE", False, A.OK), ("COLD", False, A.ALARM), ("COLD", True, A.OK)])
def test_cold_cathode(mode, on, expect):
    st = base_state(mode)
    ctx = _ctx_with("P1_ENABLED", on, table="boolean")
    assert A.ColdCathode().evaluate(ctx, st)[0].status == expect


def test_cold_cathode_transitioning_skips():
    assert A.ColdCathode().evaluate(_ctx_with("P1_ENABLED", True, "boolean"), base_state("TRANSITIONING")) == []


# ── milestones ───────────────────────────────────────────────────────────────
def test_milestones_arm_fire_rearm():
    st = base_state()
    m = A.Milestones()
    assert m.evaluate(_ctx_with("4K_TEMPERATURE", 50.0), st) == []        # first sight arms
    assert m.evaluate(_ctx_with("4K_TEMPERATURE", 9.0), st)[0].startswith(":snowflake:")
    assert m.evaluate(_ctx_with("4K_TEMPERATURE", 9.0), st) == []         # once only
    m.evaluate(_ctx_with("4K_TEMPERATURE", 12.0), st)                     # re-arm (>10+1)
    assert len(m.evaluate(_ctx_with("4K_TEMPERATURE", 9.5), st)) == 1


# ── LN2 ──────────────────────────────────────────────────────────────────────
def _ln2_ctx(row):
    l = FakeDB(); l.ln2_rows = [row]
    return FakeCtx(FakeDB(), l)


def test_ln2_stale_short_circuits():
    vs = A.LN2().evaluate(_ln2_ctx((utc(30), 11.0, 21.0, 50.0)), base_state())
    assert [v.key for v in vs] == ["LN2::stale"] and vs[0].status == A.ALARM
    assert "30 min" in vs[0].message


def test_ln2_weight_low(monkeypatch):
    monkeypatch.setattr(config, "LN2_WEIGHT_LOW", 5.0)
    vs = {v.key: v for v in A.LN2().evaluate(_ln2_ctx((utc(1), 3.0, 21.0, 50.0)), base_state())}
    assert vs["LN2::stale"].status == A.OK
    assert vs["LN2::weight_low"].status == A.ALARM and "3 kg" in vs["LN2::weight_low"].message
    assert vs["LN2::weight_crit"].status == A.OK and vs["LN2::dew_high"].status == A.OK


def test_ln2_critical_beats_low(monkeypatch):
    monkeypatch.setattr(config, "LN2_WEIGHT_LOW", 5.0)
    monkeypatch.setattr(config, "LN2_WEIGHT_CRITICAL", 2.0)
    vs = {v.key: v for v in A.LN2().evaluate(_ln2_ctx((utc(1), 1.0, 21.0, 50.0)), base_state())}
    assert vs["LN2::weight_crit"].status == A.ALARM and vs["LN2::weight_low"].status == A.OK


def test_ln2_no_data():
    assert A.LN2().evaluate(FakeCtx(FakeDB(), FakeDB()), base_state()) == []
