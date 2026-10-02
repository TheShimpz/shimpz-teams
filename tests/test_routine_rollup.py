"""A continuous Routine's healthy runs roll up into one versioned notice per minute (ADR-0092 section 9)."""

from __future__ import annotations

import dataclasses
import unittest

import routine_fixture
import test_routine_record as base

from protocol.http.v1 import routine as http_routine
from routine import record

ROUTINE_ID = "a" * 32
# A minute boundary: 09:00:00.
MINUTE = base.NINE


def continuous(gap: int = 5) -> record.TeamRoutines:
    value = base.routine(schedule={"kind": "continuous", "gap": gap, "cap": 1000})
    return base.at(base.added(value), ROUTINE_ID, MINUTE)


def run_once(state: record.TeamRoutines, start: int, end: int, outcome: str = "done") -> record.TeamRoutines:
    """Claim the Routine at ``start`` and let its worker end it at ``end``."""
    state = base.at(state, ROUTINE_ID, start)
    state, claim = record.claim(state, start, base.KEY)
    lease = record.lease_of(claim.lease_token, base.KEY)
    detail = routine_fixture.DONE if outcome == "done" else {"code": "assistant-rpc-failed", "actions": []}
    return record.finish(state, claim.run.run_id, lease, end, outcome, detail)


class RollupTests(unittest.TestCase):
    def test_healthy_runs_in_one_minute_share_one_notice_whose_count_is_its_version(self):
        state = continuous()
        for index in range(3):
            state = run_once(state, MINUTE + 10 * index, MINUTE + 10 * index + 2)
        (notice,) = state.notices
        self.assertEqual(
            (notice.outcome, notice.run_id, notice.created_at, notice.detail, notice.version),
            ("healthy", "", MINUTE, {"runs": 3}, 3),
        )
        self.assertEqual(record.routine(state, ROUTINE_ID).failures, 0)
        # The next minute starts its own notice.
        state = run_once(state, MINUTE + 60, MINUTE + 61)
        self.assertEqual([(item.created_at, item.version) for item in state.notices], [(MINUTE, 3), (MINUTE + 60, 1)])
        self.assertNotEqual(state.notices[0].notice_id, state.notices[1].notice_id)

    def test_an_acknowledged_minute_goes_on_with_a_newer_version_of_the_same_notice(self):
        state = run_once(continuous(), MINUTE, MINUTE + 1)
        (first,) = state.notices
        state = record.acknowledge(state, frozenset({(first.notice_id, 1)}))
        self.assertEqual(state.notices, ())
        state = run_once(state, MINUTE + 10, MINUTE + 11)
        (second,) = state.notices
        self.assertEqual((second.notice_id, second.version, second.detail), (first.notice_id, 2, {"runs": 2}))

    def test_a_success_ends_the_failure_streak_and_failures_keep_their_own_notices(self):
        state = run_once(continuous(), MINUTE, MINUTE + 1, "failed")
        self.assertEqual(record.routine(state, ROUTINE_ID).failures, 1)
        state = run_once(state, MINUTE + 10, MINUTE + 11)
        self.assertEqual([item.outcome for item in state.notices], ["failed", "healthy"])
        self.assertEqual(record.routine(state, ROUTINE_ID).failures, 0)

    def test_a_run_with_its_own_earlier_notice_or_a_scheduled_routine_keeps_one_notice_per_run(self):
        state = base.at(continuous(), ROUTINE_ID, MINUTE)
        state, claim = record.claim(state, MINUTE, base.KEY)
        # A run that already published a notice, such as one a person answered, ends on that same notice.
        answered = dataclasses.replace(record.run(state, claim.run.run_id), notice_version=1)
        state = dataclasses.replace(state, runs=(answered,))
        lease = record.lease_of(claim.lease_token, base.KEY)
        state = record.finish(state, claim.run.run_id, lease, MINUTE + 1, "done", routine_fixture.DONE)
        self.assertEqual(
            [(item.outcome, item.notice_id, item.version) for item in state.notices], [("done", claim.run.run_id, 2)]
        )
        state, claim, lease = base.claimed()
        state = record.finish(state, claim.run.run_id, lease, base.NINE + 1, "done", routine_fixture.DONE)
        self.assertEqual([item.outcome for item in state.notices], ["done"])

    def test_a_clock_stepped_back_past_the_bound_falls_back_to_one_notice_per_run(self):
        state = continuous()
        full = dataclasses.replace(
            record.routine(state, ROUTINE_ID), rollup_minute=MINUTE, rollup_runs=http_routine.MAX_ROLLUP_RUNS
        )
        state = dataclasses.replace(state, routines=(full,))
        state = run_once(state, MINUTE + 30, MINUTE + 31)
        self.assertEqual([item.outcome for item in state.notices], ["done"])

    def test_an_hour_of_runs_every_five_seconds_delivers_one_notice_a_minute(self):
        state, now, delivered = continuous(), MINUTE, {}
        for _index in range(720):
            state = run_once(state, now, now)
            # Admin delivers and acknowledges as it goes; the rollup's versions replace each other.
            delivered.update({item.notice_id: item.version for item in state.notices})
            state = record.acknowledge(state, frozenset((item.notice_id, item.version) for item in state.notices))
            # Team removes what each ended run held.
            state = dataclasses.replace(state, discards=())
            now += 5
        self.assertEqual(len(delivered), 60)
        self.assertEqual(set(delivered.values()), {http_routine.MAX_ROLLUP_RUNS})


if __name__ == "__main__":
    unittest.main()
