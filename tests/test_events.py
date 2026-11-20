from bfm import events as E
from tests.fakes import FakeCtx, FakeDB, base_state, utc


def _db_with_bools(rows):
    db = FakeDB()
    db.events["boolean"] = rows
    return db


def test_cursor_zero_initialises_without_replay():
    st = base_state()
    db = _db_with_bools([(1, "V112_ENABLED", True, utc(60)), (2, "V113_ENABLED", False, utc(30))])
    assert E.ValveChanges().evaluate(FakeCtx(db), st) == []
    assert st["last_valve_event_id"] == 2
    db.events["boolean"].append((3, "V112_ENABLED", False, utc()))
    msgs = E.ValveChanges().evaluate(FakeCtx(db), st)
    assert len(msgs) == 1 and "V112" in msgs[0] and "CLOSED" in msgs[0]
    assert st["last_valve_event_id"] == 3


def test_heater_changes_and_limit():
    st = base_state(); st["last_heater_event_id"] = 1
    rows = [(i, "MXC_HEATER_ENABLED", bool(i % 2), utc()) for i in range(2, 2 + E.LIMIT + 5)]
    db = _db_with_bools(rows)
    msgs = E.HeaterChanges().evaluate(FakeCtx(db), st)
    assert len(msgs) == E.LIMIT                       # one cycle processes LIMIT rows
    assert st["last_heater_event_id"] == 1 + E.LIMIT
    assert "MXC Heater" in msgs[0]
    rest = E.HeaterChanges().evaluate(FakeCtx(db), st)
    assert len(rest) == 5


def test_r1a_bool_and_power_transition():
    st = base_state(); st["last_r1a_event_id"] = 1; st["last_r1a_power_id"] = 1
    db = FakeDB()
    db.events["boolean"] = [(2, "R1A_ENABLED", False, utc(1)), (3, "R1A_ERROR_VALUE", True, utc(1))]
    db.events["double"] = [(2, "R1A_PUMP_POWER", 0.0, utc(1))]
    msgs = E.R1APump().evaluate(FakeCtx(db), st)
    assert any("R1A Pump enabled changed" in m and "OFF" in m for m in msgs)
    assert any("R1A Pump error changed" in m and "ERROR" in m for m in msgs)
    assert len(msgs) == 2                             # first power sight: prev None → no transition
    assert st["last_r1a_power_value"] == 0.0
    db.events["double"].append((3, "R1A_PUMP_POWER", 120.0, utc()))
    msgs = E.R1APump().evaluate(FakeCtx(db), st)
    assert len(msgs) == 1 and "power ON" in msgs[0] and "120.0 W" in msgs[0]


def test_cs2_alerts_grouping_and_disabled_still_advances():
    st = base_state(); st["last_cs2_alert_id"] = 10
    db = FakeDB()
    db.alerts = [
        {"id": 11, "code": 7, "datetime": "t1", "title": "Pump", "description": "d", "severity": 2},
        {"id": 12, "code": 7, "datetime": "t2", "title": "Pump", "description": "d", "severity": 2},
        {"id": 13, "code": 9, "datetime": "t3", "title": "Low", "description": None, "severity": 1},
    ]
    msgs = E.CS2Alerts().evaluate(FakeCtx(db), st)
    assert len(msgs) == 1 and "(×2)" in msgs[0] and "CS2 Error" in msgs[0]
    assert st["last_cs2_alert_id"] == 12              # severity-1 row filtered out
    db.alerts.append({"id": 14, "code": 7, "datetime": "t4", "title": "Pump", "description": "", "severity": 2})
    st["cs2_alerts_enabled"] = False
    assert E.CS2Alerts().evaluate(FakeCtx(db), st) == []
    assert st["last_cs2_alert_id"] == 14


def test_cs2_cursor_zero_initialises():
    st = base_state()
    db = FakeDB()
    db.alerts = [{"id": 5, "code": 1, "datetime": "t", "title": "x", "description": "", "severity": 2}]
    assert E.CS2Alerts().evaluate(FakeCtx(db), st) == []
    assert st["last_cs2_alert_id"] == 5


def test_init_cursors():
    st = base_state()
    db = FakeDB()
    db.alerts = [{"id": 3, "code": 1, "datetime": "t", "title": "x", "description": "", "severity": 2}]
    db.events["boolean"] = [(9, "V112_ENABLED", True, utc()), (4, "R1A_ENABLED", True, utc())]
    db.events["double"] = [(77, "R1A_PUMP_POWER", 55.0, utc())]
    E.init_cursors(st, db)
    assert (st["last_cs2_alert_id"], st["last_valve_event_id"], st["last_r1a_event_id"]) == (3, 9, 4)
    assert st["last_r1a_power_id"] == 77 and st["last_r1a_power_value"] == 55.0
