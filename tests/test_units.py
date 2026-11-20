import pytest

from bfm.core import units as u


# ── sensor registry ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("token,mapping", [
    ("MXC", "MXC_TEMPERATURE"), ("mc", "MXC_TEMPERATURE"), ("MXC1", "MXC_TEMPERATURE"),
    ("MXC2", "MXC_TEMPERATURE_FAR"), ("mcfar", "MXC_TEMPERATURE_FAR"), ("MC2", "MXC_TEMPERATURE_FAR"),
    ("p5", "P5_PRESSURE"), ("P7", "P7_PRESSURE"), ("4k", "4K_TEMPERATURE"),
    ("flow", "FLOW_VALUE"), ("FLOW_VALUE", "FLOW_VALUE"),
    ("1", "MXC_TEMPERATURE"), ("11", "FLOW_VALUE"),   # legacy list numbering
    ("STILL_TEMPERATURE", "STILL_TEMPERATURE"),
])
def test_resolve_sensor(token, mapping):
    assert u.resolve_sensor(token).mapping == mapping


@pytest.mark.parametrize("token", ["", "12", "0", "XYZ", "P8"])
def test_resolve_sensor_unknown(token):
    assert u.resolve_sensor(token) is None


def test_registry_consistency():
    assert len(u.LIST_COLD) == 11
    assert [s.short for s in u.LIST_IDLE] == ["P2", "P5"]
    assert u.PRESSURE_MAPPINGS == [f"P{i}_PRESSURE" for i in range(1, 8)]
    assert "MXC_TEMPERATURE_FAR" in u.TEMP_MAPPINGS


# ── formatting ───────────────────────────────────────────────────────────────
def test_fmt_pressure_matches_legacy():
    assert u.fmt_pressure(0.0123) == "12.3 mbar"
    assert u.fmt_pressure(1e-3) == "1 mbar"
    assert u.fmt_pressure(5e-5) == "50 μbar"      # < 0.1 mbar → μbar
    assert u.fmt_pressure(0.1) == "100 mbar"


def test_fmt_temperature():
    assert u.fmt_temperature(0.0123, prefer_mk=True) == "12.30 mK"
    assert u.fmt_temperature(4.2, prefer_mk=True) == "4.2 K (-268.9 °C)"
    assert u.fmt_temperature(0.5, prefer_mk=False).startswith("0.5 K")


def test_pa_to_psi():
    assert round(u.pa_to_psi(101325), 2) == 14.70


# ── durations ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,minutes,label", [
    ("silent for 2h", 120, "2 hours"),
    ("mute 30 min", 30, "30 minutes"),
    ("silence 3 days", 4320, "3 days"),
    ("1 day", 1440, "1 day"),
    ("8m", 8, "8 minutes"),
    ("0.5h", 30, "0.5 hours"),
    ("7.5min", 7.5, "7.5 minutes"),
    ("2 weeks", 20160, "2 weeks"),
    ("1w", 10080, "1 week"),
    ("6 months", 259200, "6 months"),
    ("1mo", 43200, "1 month"),
    ("1 year", 525600, "1 year"),
    ("2y", 1051200, "2 years"),
    ("静音3天", 4320, "3 days"),
    ("2小时", 120, "2 hours"),
    ("15分钟", 15, "15 minutes"),
])
def test_parse_duration(text, minutes, label):
    assert u.parse_duration(text) == (minutes, label)


@pytest.mark.parametrize("text", ["silence forever", "for ever", "permanently", "永久静音", "always"])
def test_parse_duration_forever(text):
    assert u.parse_duration(text) == (None, "permanently")


@pytest.mark.parametrize("text", ["ok", "plot MXC", "", "hello 12"])
def test_parse_duration_none(text):
    # "hello 12" has no unit → not a duration
    assert u.parse_duration(text) is None


def test_month_beats_minute():
    assert u.parse_duration("plot P2 1mo")[0] == 43200
    assert u.parse_duration("plot P2 1m")[0] == 1


def test_duration_does_not_match_inside_numbers():
    # "260622_0000" must not be read as a duration (legacy lookbehind rule)
    assert u.parse_duration("plot P1 260622_0000 260622_1200") is None


def test_parse_duration_minutes_default():
    assert u.parse_duration_minutes("plot P2", 30) == 30
    assert u.parse_duration_minutes("plot P2 12h", 30) == 720
    assert u.parse_duration_minutes("silence forever", 30) == 30


def test_fmt_minutes():
    assert u.fmt_minutes(30) == "30 min"
    assert u.fmt_minutes(90) == "1.5 h"
    assert u.fmt_minutes(1440) == "1 day"
    assert u.fmt_minutes(4320) == "3 days"


def test_strip_duration():
    assert u.strip_duration("P2 P5 2h").split() == ["P2", "P5"]
