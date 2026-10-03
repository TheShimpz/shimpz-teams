"""When a Routine fires: the next occurrence of its canonical schedule, strictly after an instant (ADR-0086)."""

from __future__ import annotations

import datetime
import zoneinfo

from protocol.http.v1 import routine as http_routine

UTC = datetime.UTC


class ScheduleError(ValueError):
    """A schedule or timezone is not in the closed Routine contract."""


def zone(name: object) -> zoneinfo.ZoneInfo:
    """The IANA zone a canonical name loads, or ScheduleError; never a path or the host's local zone."""
    if http_routine.canonical_timezone(name) is None:
        raise ScheduleError("invalid timezone")
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError("unknown timezone") from exc


def _utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        raise ScheduleError("naive instant")
    return value.astimezone(UTC)


def _wall_clock(day: datetime.date, time: str, tz: zoneinfo.ZoneInfo) -> datetime.datetime:
    # fold=0 runs an ambiguous local time at its first occurrence and a nonexistent one shifted past the gap.
    hour, minute = (int(part) for part in time.split(":"))
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=tz).astimezone(UTC)


def _matches(schedule: dict[str, object], day: datetime.date) -> bool:
    if schedule["kind"] == "weekly":
        return day.weekday() == schedule["weekday"]
    if schedule["kind"] == "monthly":
        return day.day == schedule["day"]
    return True


def next_run(
    schedule: object, timezone: object, anchor: datetime.datetime, after: datetime.datetime
) -> datetime.datetime:
    """The first firing strictly after ``after``; hourly counts elapsed hours from the Routine's ``anchor``."""
    canonical = http_routine.canonical_schedule(schedule)
    if canonical is None:
        raise ScheduleError("invalid schedule")
    tz = zone(timezone)
    anchor, after = _utc(anchor), _utc(after)
    # Nothing fires at or before the anchor: the first firing is strictly after it.
    after = max(after, anchor)
    if canonical["kind"] == "continuous":
        # A continuous Routine's next run is due its gap after the previous one ended, never a backlog.
        return after + datetime.timedelta(seconds=canonical["gap"])
    if canonical["kind"] == "hourly":
        period = datetime.timedelta(hours=canonical["every"])
        return anchor + period * ((after - anchor) // period + 1)
    # Start a day early: a firing on the previous local date can still lie after ``after`` in UTC.
    day = after.astimezone(tz).date() - datetime.timedelta(days=1)
    while True:
        if _matches(canonical, day) and (candidate := _wall_clock(day, canonical["time"], tz)) > after:
            return candidate
        day += datetime.timedelta(days=1)
