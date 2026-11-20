from datetime import datetime, timedelta, timezone

from bfm.core import timeutil as tu


def test_parse_iso_naive_and_aware():
    naive = tu.parse_iso("2026-11-20T12:00:00")
    assert naive == datetime(2026, 11, 20, 12, 0) and naive.tzinfo is None
    aware = tu.parse_iso("2026-11-20T18:00:00+00:00")       # 18 UTC = 12 CST
    assert aware == datetime(2026, 11, 20, 12, 0) and aware.tzinfo is None
    assert tu.parse_iso(None) is None
    assert tu.parse_iso("garbage") is None


def test_is_future_and_forever():
    assert tu.is_future(tu.FOREVER_TS)
    assert tu.is_future((tu.now() + timedelta(minutes=1)).isoformat())
    assert not tu.is_future((tu.now() - timedelta(minutes=1)).isoformat())
    assert not tu.is_future(None)


def test_age_minutes_aware_and_naive():
    assert abs(tu.age_minutes(tu.utcnow() - timedelta(minutes=5)) - 5) < 0.1
    assert abs(tu.age_minutes(tu.now() - timedelta(minutes=5)) - 5) < 0.1
    assert tu.age_minutes(None) is None


def test_compact_local_roundtrip_dst_aware():
    # 2026-11-20 is CST (UTC-6); 2026-06-22 is CDT (UTC-5)
    nov = tu.parse_compact_local("261120_1200")
    assert nov == datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
    jun = tu.parse_compact_local("260622_0130")
    assert jun == datetime(2026, 6, 22, 6, 30, tzinfo=timezone.utc)
    assert tu.fmt_compact_local(nov) == "261120_1200"
    assert tu.parse_compact_local("not_a_time") is None
    assert tu.parse_compact_local("261399_0000") is None


def test_tz_abbrev():
    assert tu.tz_abbrev(datetime(2026, 11, 20, tzinfo=timezone.utc)) == "CST"
    assert tu.tz_abbrev(datetime(2026, 7, 1, tzinfo=timezone.utc)) == "CDT"


def test_fmt_local():
    assert tu.fmt_local(datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)) == "2026-11-20 12:00:00"
    assert tu.fmt_local(None) == "n/a"
