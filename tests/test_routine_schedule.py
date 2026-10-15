"""Routine schedules fire in the user's timezone, strictly after an instant, across daylight-saving changes."""

import datetime
import unittest
from fractions import Fraction

from protocol.http.v1 import routine as http_routine
from routine import schedule

UTC = datetime.UTC
ANCHOR = datetime.datetime(2026, 1, 1, tzinfo=UTC)
NEW_YORK = "America/New_York"


def at(*parts: int) -> datetime.datetime:
    return datetime.datetime(*parts, tzinfo=UTC)


def local(value: datetime.datetime, name: str) -> datetime.datetime:
    return value.astimezone(schedule.zone(name))


class ScheduleContractTests(unittest.TestCase):
    def test_only_the_closed_schedule_shapes_are_canonical(self):
        valid = (
            {"kind": "continuous", "gap": 3600},
            {"kind": "continuous", "gap": 86400},
            {"kind": "daily", "time": "00:00"},
            {"kind": "weekly", "weekday": 6, "time": "23:59"},
            {"kind": "monthly", "day": 28, "time": "09:05"},
        )
        for value in valid:
            with self.subTest(value=value):
                self.assertEqual(http_routine.canonical_schedule(value), value)
        invalid = (
            None,
            [],
            {"kind": "yearly", "time": "09:00"},
            {"kind": "continuous", "gap": 0},
            {"kind": "daily", "time": "24:00"},
            {"kind": "daily", "time": "9:00"},
            {"kind": "daily", "time": 900},
            {"kind": "daily", "time": "09:00", "every": 1},
            {"kind": "weekly", "weekday": 7, "time": "09:00"},
            {"kind": "weekly", "time": "09:00"},
            {"kind": "monthly", "day": 29, "time": "09:00"},
            {"kind": "monthly", "day": 0, "time": "09:00"},
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(http_routine.canonical_schedule(value))

    def test_timezones_are_iana_names_that_load_never_paths(self):
        for name in ("UTC", "America/Sao_Paulo", "America/Argentina/Buenos_Aires", "Etc/GMT+3"):
            with self.subTest(name=name):
                self.assertEqual(schedule.zone(name).key, name)
        for name in ("", "/etc/localtime", "../UTC", "America//Sao_Paulo", 3, "Mars/Olympus"):
            with self.subTest(name=name), self.assertRaises(schedule.ScheduleError):
                schedule.zone(name)

    def test_daily_rates_bound_a_teams_schedule(self):
        self.assertEqual(http_routine.daily_rate({"kind": "continuous", "gap": 21600}), 4)
        self.assertEqual(http_routine.daily_rate({"kind": "daily", "time": "09:00"}), 1)
        self.assertEqual(http_routine.daily_rate({"kind": "weekly", "weekday": 0, "time": "09:00"}), Fraction(1, 7))
        self.assertEqual(http_routine.daily_rate({"kind": "monthly", "day": 1, "time": "09:00"}), Fraction(1, 28))
        continuous = {"kind": "continuous", "gap": 30}
        self.assertEqual((http_routine.daily_rate(continuous), http_routine.daily_cap(continuous)), (2880, 2880))
        self.assertEqual(http_routine.daily_cap({"kind": "weekly", "weekday": 0, "time": "09:00"}), 1)
        self.assertEqual(http_routine.daily_cap({"kind": "continuous", "gap": 18000}), 5)

    def test_a_continuous_schedule_runs_its_stated_gap_all_day(self):
        # The cap is always the gap's whole day, so no cap ever rewrites the interval the person stated (ADR-0101).
        for gap, cap in ((5, http_routine.MAX_CONTINUOUS_CAP), (7, 12_343), (30, 2880), (86_400, 1), (366 * 86_400, 1)):
            value = {"kind": "continuous", "gap": gap}
            self.assertEqual((http_routine.canonical_schedule(value), http_routine.continuous_cap(gap)), (value, cap))
        for value in (
            {"kind": "continuous", "gap": 4},
            {"kind": "continuous", "gap": 366 * 86_400 + 1},
            {"kind": "continuous", "gap": 5.0},
            {"kind": "continuous", "gap": True},
            {"kind": "continuous", "gap": "30"},
            {"kind": "continuous", "gap": 30, "every": 1},
            {"kind": "continuous"},
        ):
            with self.subTest(value=value):
                self.assertIsNone(http_routine.canonical_schedule(value))


def upcoming(value, timezone, anchor, after, count):
    """The next ``count`` firings after ``after``, one strictly after another."""
    runs = []
    for _index in range(count):
        after = schedule.next_run(value, timezone, anchor, after)
        runs.append(after)
    return runs


class NextRunTests(unittest.TestCase):
    def test_each_kind_fires_strictly_after_the_instant(self):
        weekly = {"kind": "weekly", "weekday": 0, "time": "09:00"}
        runs = upcoming(weekly, "America/Sao_Paulo", ANCHOR, at(2026, 9, 30), 3)
        self.assertEqual(runs, [at(2026, 10, 5, 12), at(2026, 10, 12, 12), at(2026, 10, 19, 12)])
        monthly = {"kind": "monthly", "day": 28, "time": "23:30"}
        self.assertEqual(
            upcoming(monthly, "UTC", ANCHOR, at(2026, 1, 28, 23, 30), 2),
            [
                at(2026, 2, 28, 23, 30),
                at(2026, 3, 28, 23, 30),
            ],
        )
        daily = {"kind": "daily", "time": "09:00"}
        self.assertEqual(schedule.next_run(daily, "UTC", ANCHOR, at(2026, 9, 30, 8, 59)), at(2026, 9, 30, 9))
        self.assertEqual(schedule.next_run(daily, "UTC", ANCHOR, at(2026, 9, 30, 9)), at(2026, 10, 1, 9))

    def test_a_continuous_run_is_due_its_gap_after_an_instant_never_before_the_anchor(self):
        continuous = {"kind": "continuous", "gap": 5}
        self.assertEqual(schedule.next_run(continuous, "UTC", ANCHOR, at(2026, 9, 30)), at(2026, 9, 30, 0, 0, 5))
        self.assertEqual(
            schedule.next_run(continuous, "UTC", at(2026, 10, 1), at(2026, 9, 30)), at(2026, 10, 1, 0, 0, 5)
        )

    def test_a_nonexistent_local_time_runs_shifted_past_the_gap_and_an_ambiguous_one_first(self):
        daily = {"kind": "daily", "time": "02:30"}
        spring = schedule.next_run(daily, NEW_YORK, ANCHOR, at(2026, 3, 8))
        self.assertEqual(local(spring, NEW_YORK).timetuple()[:5], (2026, 3, 8, 3, 30))
        self.assertEqual(local(schedule.next_run(daily, NEW_YORK, ANCHOR, spring), NEW_YORK).hour, 2)
        fall = schedule.next_run({"kind": "daily", "time": "01:30"}, NEW_YORK, ANCHOR, at(2026, 11, 1))
        self.assertEqual(fall, at(2026, 11, 1, 5, 30))

    def test_invalid_input_is_refused(self):
        with self.assertRaisesRegex(schedule.ScheduleError, "invalid schedule"):
            schedule.next_run({"kind": "daily", "time": "25:00"}, "UTC", ANCHOR, ANCHOR)
        with self.assertRaisesRegex(schedule.ScheduleError, "naive instant"):
            schedule.next_run({"kind": "daily", "time": "09:00"}, "UTC", ANCHOR, at(2026, 9, 30).replace(tzinfo=None))


if __name__ == "__main__":
    unittest.main()
