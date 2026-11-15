"""Time resolution against the call time. The calls are on a Friday morning (2026-10-30)."""

from datetime import datetime

import pytest

from app.timeparse import resolve

TZ = "Europe/Istanbul"


def at(s):
    return datetime.fromisoformat(s)


def call(hhmm):
    return at(f"2026-10-30T{hhmm}:00+03:00")  # a Friday


@pytest.mark.parametrize("day, when, call_time, kind, start, end", [
    # "yarın akşam altı" -> Saturday 18:00
    ({"kind": "tomorrow"}, {"kind": "exact", "hour": 18}, call("11:10"), "exact", "2026-10-31T18:00:00+03:00", None),
    # "Cuma akşam altı", said on Friday morning -> today 18:00
    ({"kind": "weekday", "weekday": "friday"}, {"kind": "exact", "hour": 18}, call("11:42"), "exact",
     "2026-10-30T18:00:00+03:00", None),
    # same weekday, but the time has already passed -> next week
    ({"kind": "weekday", "weekday": "friday"}, {"kind": "exact", "hour": 10}, call("11:42"), "exact",
     "2026-11-06T10:00:00+03:00", None),
    # "pazartesi 12-1 civarı" -> Monday 12:00 (a range counts as its start)
    ({"kind": "weekday", "weekday": "monday"}, {"kind": "range", "hour": 12, "end_hour": 13}, call("11:36"), "exact",
     "2026-11-02T12:00:00+03:00", None),
    # "bugün saat 13" -> today 13:00
    ({"kind": "today"}, {"kind": "exact", "hour": 13}, call("11:44"), "exact", "2026-10-30T13:00:00+03:00", None),
    # "cumartesi 16:00" -> tomorrow (Saturday) 16:00
    ({"kind": "weekday", "weekday": "saturday"}, {"kind": "exact", "hour": 16}, call("09:57"), "exact",
     "2026-10-31T16:00:00+03:00", None),
    # "haftaya salı öğleden sonra" -> Tuesday of next week, 12-18
    ({"kind": "weekday", "weekday": "tuesday", "next_week": True}, {"kind": "part_of_day", "part_of_day": "afternoon"},
     call("10:00"), "window", "2026-11-03T12:00:00+03:00", "2026-11-03T18:00:00+03:00"),
    # "yarın" with no time -> all of Saturday
    ({"kind": "tomorrow"}, {"kind": "none"}, call("10:00"), "window", "2026-10-31T00:00:00+03:00",
     "2026-11-01T00:00:00+03:00"),
    # "5 Kasım saat 14:30"
    ({"kind": "date", "day_of_month": 5, "month": 11}, {"kind": "exact", "hour": 14, "minute": 30}, call("10:00"),
     "exact", "2026-11-05T14:30:00+03:00", None),
    # "saat 3'te" with no day -> today
    ({"kind": "none"}, {"kind": "exact", "hour": 15}, call("10:00"), "exact", "2026-10-30T15:00:00+03:00", None),
])
def test_resolves_against_the_call_time(day, when, call_time, kind, start, end):
    r = resolve(day, when, call_time, TZ)
    assert r.kind == kind
    assert r.start == (at(start) if start else None)
    assert r.end == (at(end) if end else None)


def test_no_day_and_no_time_is_no_constraint():
    # The call is cut off before a time was given
    assert resolve({"kind": "none"}, {"kind": "none"}, call("11:39"), TZ).kind == "none"


def test_call_time_in_utc_is_read_in_the_tenants_timezone():
    # 22:30 UTC on Friday is already Saturday 01:30 in Istanbul, so "yarın" is Sunday.
    r = resolve({"kind": "tomorrow"}, {"kind": "exact", "hour": 10}, at("2026-10-30T22:30:00+00:00"), TZ)
    assert r.start == at("2026-11-01T10:00:00+03:00")
