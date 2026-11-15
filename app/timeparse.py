"""Turn what the caller said about time into a concrete request, relative to when the call took place.

The extractor (LLM or rules) only *reads* the call: "tomorrow", "Saturday", "18:00", "around 12-1". This
module does the arithmetic, always against the call's own time in the tenant's timezone, never the wall clock:
a call from Friday that says "tomorrow" means Saturday, whenever the system happens to process it.

Result kinds:
- exact:  a specific start time ("yarın 12'de"). A range ("12-1 civarı") counts as its start.
- window: a day, or a part of a day, without an exact time ("yarın öğleden sonra").
- none:   no time constraint at all.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Parts of the day, as [start hour, end hour) within business hours (the agent says 9:00-18:00).
PARTS_OF_DAY = {"morning": (9, 12), "noon": (12, 14), "afternoon": (12, 18), "evening": (16, 18)}


@dataclass(frozen=True)
class Requested:
    kind: Literal["exact", "window", "none"]
    start: datetime | None = None   # exact: the start; window: start of the window
    end: datetime | None = None     # window only, exclusive


def _resolve_day(day: dict, call: datetime, hour: int | None) -> date | None:
    kind = day.get("kind", "none")
    today = call.date()
    if kind == "none":
        return None
    if kind == "today":
        return today
    if kind == "tomorrow":
        return today + timedelta(days=1)
    if kind == "day_after_tomorrow":
        return today + timedelta(days=2)
    if kind == "weekday":
        target = WEEKDAYS.index(day["weekday"])
        ahead = (target - today.weekday()) % 7
        if day.get("next_week"):
            # "haftaya salı": that weekday in the next calendar week
            return today + timedelta(days=(7 - today.weekday()) + target)
        if ahead == 0:
            # Same weekday as the call: today if that time is still ahead, otherwise a week later.
            still_ahead = hour is None or time(hour) > call.time()
            return today if still_ahead else today + timedelta(days=7)
        return today + timedelta(days=ahead)
    if kind == "date":
        candidate = date(today.year, day["month"], day["day_of_month"])
        return candidate if candidate >= today else date(today.year + 1, day["month"], day["day_of_month"])
    raise ValueError(f"unknown day kind {kind!r}")


def resolve(day: dict, when: dict, call_time: datetime, timezone: str) -> Requested:
    """`day` and `when` are the extractor's reading (see app/extraction.py)."""
    tz = ZoneInfo(timezone)
    call = call_time.astimezone(tz)
    kind = when.get("kind", "none")
    hour = when.get("hour") if kind in ("exact", "range") else None
    target = _resolve_day(day, call, hour)

    if kind in ("exact", "range"):
        target = target or call.date()  # "saat 3'te" with no day: today
        start = datetime.combine(target, time(when["hour"], when.get("minute") or 0), tz)
        return Requested("exact", start)

    if kind == "part_of_day":
        target = target or call.date()
        first, last = PARTS_OF_DAY[when["part_of_day"]]
        return Requested("window", datetime.combine(target, time(first), tz), datetime.combine(target, time(last), tz))

    if target is not None:  # a day without a time: the whole day
        start = datetime.combine(target, time(0), tz)
        return Requested("window", start, start + timedelta(days=1))

    return Requested("none")
