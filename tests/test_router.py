"""Router tests: every command in the README/help list routes to the right
handler and produces the expected reply, with a fake DB and no Slack."""
from datetime import timedelta

import pytest

from bfm import config
from bfm.commands.router import Router
from bfm.core import timeutil as tu
from bfm.core.state import StateStore
from tests.fakes import FakeDB, utc


class FakeSlack:
    ok = True
    channel = "C1"
    bot_user_id = config.SLACK_BOT_USER_ID

    def __init__(self):
        self.uploads = []
        self.history_msgs = []

    def upload_image(self, path, title, thread_ts=None):
        self.uploads.append((str(path), title, thread_ts))
        return True

    def probe(self, force=False):
        return self.ok

    def get(self, endpoint, params):
        return {"ok": True, "messages": []}

    def history(self, oldest, limit=50):
        return [m for m in self.history_msgs if float(m["ts"]) > float(oldest)]


class AvailFakeDB(FakeDB):
    available = True
    last_error = None

    def series(self, mapping, start, end, table="double", limit=200000):
        return [(utc(10), 0.02), (utc(5), 0.021), (utc(1), 0.0205)] if mapping else []


@pytest.fixture
def env(tmp_path):
    store = StateStore(tmp_path / "s.json").load()
    with store.locked() as st:
        st["current_mode"] = "COLD"
    cs2, ln2 = AvailFakeDB(), AvailFakeDB()
    now = utc()
    for mp in ("P1_PRESSURE", "P2_PRESSURE", "P3_PRESSURE", "P4_PRESSURE", "P5_PRESSURE", "P6_PRESSURE", "P7_PRESSURE"):
        cs2.latest_rows[("double", mp)] = (0.0012, now)
    for mp in ("MXC_TEMPERATURE", "MXC_TEMPERATURE_FAR", "STILL_TEMPERATURE"):
        cs2.latest_rows[("double", mp)] = (0.015, now)
    for mp in ("4K_TEMPERATURE", "50K_TEMPERATURE", "B1A_TEMPERATURE", "B2_TEMPERATURE"):
        cs2.latest_rows[("double", mp)] = (4.5, now)
    cs2.latest_rows[("double", "R1A_PUMP_POWER")] = (120.0, now)
    cs2.latest_rows[("double", "STILL_HEATING_POWER")] = (0.0012, now)
    for mp, v in (("P1_ENABLED", True), ("B1A_ENABLED", True), ("B2_ENABLED", False), ("R1A_ENABLED", True),
                  ("R2_ENABLED", False), ("COM_ENABLED", True), ("V112_ENABLED", True), ("V113_ENABLED", False),
                  ("HEATSWITCH_STILL_ENABLED", True), ("MXC_HEATER_ENABLED", False)):
        cs2.latest_rows[("boolean", mp)] = (v, now)
    cs2.latest_rows[("int", "B1A_SPEED")] = (100, now)
    cs2.devices_[config.PT_DEVICE] = {"datetime": now, "values": {
        "fCoolantInTemp": 290.0, "fCoolantOutTemp": 295.0, "fOilTemp": 300.0, "fHeliumTemp": 301.0,
        "fHighPressure": 2_000_000, "fLowPressure": 500_000, "fMotorCurrent": 10.5,
        "bCompressorRunning": True, "fHoursOfOperation": 1234}}
    ln2.ln2_rows = [(now, 11.5, 21.0, 50.0)]
    slack = FakeSlack()
    router = Router(store, cs2, ln2, slack)

    def run(text, ts="1700.1"):
        with store.locked() as st:
            name = router.execute(text, ts, st=st)
        return name

    def replies():
        return [m for m in store.data["outbox"] if m["kind"] == "reply"]

    def last():
        r = replies()
        return r[-1]["text"] if r else ""

    return dict(store=store, cs2=cs2, ln2=ln2, slack=slack, router=router, run=run, replies=replies, last=last)


# ── routing table ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,handler,needle", [
    ("help", "help", "BlueFors Monitor — Commands"),
    ("", "help", "Commands"),
    ("pressure reading", "pressure", "Current Pressure Readings"),
    ("temperature reading", "temperature", "Current Temperature Readings"),
    ("pump status", "pump_status", "Pump Status"),
    ("heater status", "heater_status", "Heater Status"),
    ("valve status", "valve_status", "Valve Status"),
    ("pulse tube status", "pulsetube_status", "Pulse Tube Compressor"),
    ("pt status", "pulsetube_status", "RUNNING"),
    ("pause alerts", "pause", "All alerts paused"),
    ("resume alert", "resume", "Alerts resumed"),
    ("sentinel off", "sentinel", "Sentinel alerts OFF"),
    ("sentinel on", "sentinel", "Sentinel alerts ON"),
    ("list", "list", "Sensor List"),
    ("status", "status", "Monitor Status"),
    ("mode", "status", "Mode:"),
    ("what is the alarm", "list_alarms", "Configured Alarms"),
    ("what is current alarm criteria", "list_alarms", "COLD mode only"),
    ("what alarms in idle mode", "list_alarms", "IDLE mode only"),
    ("alarms", "list_alarms", "Always on"),
    ("filter status", "filter_status", "Chilled water filter"),
    ("I have changed the filter", "filter_done", "marked as replaced"),
    ("set mode cold", "set_mode", "manually set to *COLD*"),
    ("set mode auto", "set_mode", "automatic"),
    ("set mode bogus", "set_mode", "Unknown mode"),
    ("ack", "ack", "acknowledged"),
    ("reset P5", "reset_threshold", "reset to default"),
    ("idle reset P5", "reset_threshold", "[IDLE]"),
    ("reset P9", "reset_threshold", "Unknown sensor"),
    ("change P5 to 0.001 for 2h", "change_threshold", "threshold → `0.001 bar` for *2 hours*"),
    ("cold change MXC to 0.035 for ever", "change_threshold", "*permanently*"),
    ("change P5 to 0.001 for fortnight", "change_threshold", "Unknown duration"),
    ("ln2", "ln2", "LN2 Scale — current reading"),
    ("how much liquid nitrogen", "ln2", "Weight: `11.5 kg`"),
    ("silence ln2 2h", "ln2", "LN2 alarms silenced for 2 hour(s)"),
    ("resume ln2 alerts", "ln2", "LN2 alarms re-enabled"),
])
def test_routing(env, text, handler, needle):
    assert env["run"](text) == handler
    assert needle in env["last"](), env["last"]()


def test_replies_go_to_thread(env):
    env["run"]("help", ts="42.0")
    assert env["replies"]()[-1]["thread_ts"] == "42.0"


# ── plots ────────────────────────────────────────────────────────────────────
def test_plot_single_multi_and_context(env):
    assert env["run"]("plot P2") == "plot"
    assert env["slack"].uploads[-1][1].startswith("P2 Pressure — last 30 min")
    assert env["run"]("plot P2 P5 2h") == "plot"
    assert env["slack"].uploads[-1][1] == "P2 + P5 — last 2 h"
    assert env["run"]("plot MXC2,12h") == "plot"
    assert env["slack"].uploads[-1][1].startswith("MXC2 (far) Temperature — last 12 h")
    assert env["run"]("plot 1h") == "plot"                      # context sensor
    assert env["slack"].uploads[-1][1].startswith("MXC2 (far) Temperature — last 1 h")
    assert env["run"]("longer") == "plot_longer"                # 4× the last window
    assert env["slack"].uploads[-1][1].startswith("MXC2 (far) Temperature — last 4 h")
    assert env["run"]("plot it 30min") == "plot"
    assert env["store"].data["ctx"]["sensor_key"] == "MXC2"


def test_plot_time_range_and_errors(env):
    assert env["run"]("plot P1 260622_0000 260622_1200") == "plot_range"
    assert "2026-06-22 00:00 – 2026-06-22 12:00 CDT" in env["slack"].uploads[-1][1]
    assert env["run"]("plot P1 260622_1200 260622_0000") == "plot_range"
    assert "Invalid time range" in env["last"]()
    env["store"].data["ctx"] = {}
    assert env["run"]("plot 12h") == "plot_which"
    assert "Which sensor?" in env["last"]()


def test_plot_pulsetube_quantity(env):
    env["cs2"].query = lambda sql, params=None: [(utc(5), 290.0), (utc(1), 291.0)]
    assert env["run"]("plot coolant in 2h") == "plot_pulsetube"
    assert env["slack"].uploads[-1][1] == "Pulse Tube Coolant In — last 2 h"


def test_plot_ln2(env):
    env["ln2"].query = lambda sql, params=None: [(utc(5), 11.4), (utc(1), 11.5)]
    assert env["run"]("plot ln2 weight 6h") == "ln2"
    assert env["slack"].uploads[-1][1] == "LN2 weight — last 6 h"


# ── state effects ────────────────────────────────────────────────────────────
def test_set_mode_wipes_and_sets_override(env):
    st = env["store"].data
    st["acked_sensors"]["X"] = tu.FOREVER_TS
    env["run"]("set mode transitioning")
    assert st["mode_override"] == "TRANSITIONING" and st["current_mode"] == "TRANSITIONING"
    assert st["acked_sensors"] == {}
    env["run"]("set mode auto")
    assert st["mode_override"] is None


def test_ack_silences_threshold_keys_only_but_clears_all_pending(env):
    st = env["store"].data
    st["pending_alert_msgs"] = {"STALE::P2": {"ts": "1", "channel": "c"}}
    env["run"]("ack")
    assert tu.is_future(st["acked_sensors"]["MXC_TEMPERATURE"])
    assert "STALE::P2" not in st["acked_sensors"]
    assert st["pending_alert_msgs"] == {}


def test_change_threshold_sets_override_for_mode(env):
    st = env["store"].data
    env["run"]("idle change P2 to 0.02 for 30min")
    ov = st["threshold_overrides_idle"]["P2_PRESSURE"]
    assert ov["max_val"] == 0.02 and tu.is_future(ov["expires_at"])
    env["run"]("change MXC to 0.04 for ever")
    assert st["threshold_overrides_cold"]["MXC_TEMPERATURE"]["expires_at"] is None
    env["run"]("list")
    assert "(overridden)" in env["last"]()
    env["run"]("status")
    assert "Active threshold overrides (COLD)" in env["last"]()


def test_ln2_silence_sets_namespace_key(env):
    st = env["store"].data
    env["run"]("silence ln2 for 1 day")
    assert tu.is_future(st["acked_sensors"]["LN2::__all__"])
    env["run"]("resume ln2 alerts")
    assert "LN2::__all__" not in st["acked_sensors"]


def test_db_down_commands_still_answer(env):
    env["cs2"].available = False
    env["run"]("pressure reading")
    assert "no database connection" in env["last"]()
    env["run"]("help")
    assert "Commands" in env["last"]()


def test_nlp_fallback_and_feedback(env):
    st = env["store"].data
    assert env["run"]("temps") == "nlp"
    assert "Current Temperature Readings" in "\n".join(m["text"] for m in env["replies"]())
    n = len(env["replies"]())
    assert env["run"]("asdkjh qwe zxc") == "nlp"
    assert len(env["replies"]()) > n                     # always answers something
    assert st["last_nlp_command"]["input_text"] == "temps"
    assert env["run"]("wrong") == "nlp_wrong"
    assert "was wrong. What did you mean?" in env["last"]()
    assert st["nlp_pending"][-1]["asked_correction"] is True


def test_poll_skips_old_mentions_and_runs_new(env):
    import time
    sl, store = env["slack"], env["store"]
    tag = f"<@{config.SLACK_BOT_USER_ID}>"
    old = f"{time.time() - 3600:.6f}"
    new = f"{time.time() - 5:.6f}"
    sl.history_msgs = [{"ts": old, "text": f"{tag} pause alerts", "user": "U1"},
                       {"ts": new, "text": f"{tag} help", "user": "U1"},
                       {"ts": f"{time.time() - 4:.6f}", "text": "not for the bot", "user": "U2"}]
    assert env["router"].poll() == 1
    assert store.data["alerts_paused"] is False            # old command skipped
    assert store.data["slack"]["skipped_commands"] == 1
    assert float(store.data["last_slack_ts"]) >= float(new)
    assert "Commands" in env["last"]()
