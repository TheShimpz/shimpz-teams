"""A continuous Routine's healthy runs roll up into one versioned notice per minute (ADR-0092 §9, ADR-0101).

The rollup carries the Routine's name as it was when each version was written and the minute's summed usage.
"""

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

import routine_fixture
import test_routine_record as base

from local.routine import notices as routine_notices
from local.routine import store as routine_store
from protocol.http.v1 import routine as http_routine
from routine import claim as routine_claim
from routine import record
from routine import runs as routine_runs

ROUTINE_ID = "a" * 32
# A minute boundary: 09:00:00.
MINUTE = base.NINE
NAME = "Resumo das mudanças de DNS"
# The active time each run of these tests spends.
RUN_MS = 1200


def defined(gap: int = 5) -> record.Routine:
    # A one-step Routine that shows no result keeps the compact minute rollup of its healthy runs (ADR-0101); every
    # five seconds all day it uses 17,280 of the Team's 20,000 daily steps.
    value = base.routine(
        schedule={"kind": "continuous", "gap": gap, "cap": http_routine.continuous_cap(gap)},
        plan=routine_fixture.plan_document(output={"mode": "none", "step": None}),
    )
    return dataclasses.replace(value, name=NAME)


def continuous(gap: int = 5) -> record.TeamRoutines:
    return base.at(base.added(defined(gap)), ROUTINE_ID, MINUTE)


def run_once(state: record.TeamRoutines, start: int, end: int, outcome: str = "done") -> record.TeamRoutines:
    """Claim the Routine at ``start`` and let its worker end it at ``end``."""
    state = base.at(state, ROUTINE_ID, start)
    state, claim = routine_claim.claim(state, start, base.KEY)
    lease = record.lease_of(claim.lease_token, base.KEY)
    state = routine_runs.spend(state, claim.run.run_id, lease, start, (1, RUN_MS))
    detail = (
        routine_fixture.DONE
        if outcome == "done"
        else {"code": "assistant-rpc-failed", "actions": [], "position": None, "steps": None}
    )
    return routine_runs.finish(state, claim.run.run_id, lease, end, outcome, detail)


def delivery(state: record.TeamRoutines) -> list[dict[str, object]]:
    """The healthy rollups Admin receives in one delivery, exactly as Team serves them."""
    return [routine_notices._notice("team_1", item) for item in state.notices if item.outcome == "healthy"]


def acknowledged(state: record.TeamRoutines, batch: list[dict[str, object]]) -> record.TeamRoutines:
    return record.acknowledge(state, frozenset((item["notice_id"], item["version"]) for item in batch))


def restarted(state: record.TeamRoutines) -> record.TeamRoutines:
    """The state as a restarted Team reads it back from its encrypted store."""
    with tempfile.TemporaryDirectory() as directory:
        store = routine_store.RoutineStore(Path(directory) / "state", Path(directory) / "key" / "aes256.key")
        store.update("team_1", lambda _before: (state, None))
        return store.load("team_1")


def same_minute_change() -> tuple[list[list[dict[str, object]]], record.TeamRoutines]:
    """A minute whose rollup is delivered, grows, survives a restart and a change, and is acknowledged late."""
    state = run_once(continuous(), MINUTE, MINUTE + 1)
    first = delivery(state)
    deliveries = [first]
    state = restarted(run_once(state, MINUTE + 10, MINUTE + 11))
    changed = record.scheduled(defined(gap=6), MINUTE + 20)
    state = record.update(state, changed, 1, MINUTE + 20)
    state = run_once(state, MINUTE + 30, MINUTE + 31)
    # The first delivery's acknowledgment arrives only now, after the minute's count grew twice.
    state = acknowledged(state, first)
    batch = delivery(state)
    deliveries.append(batch)
    return deliveries, acknowledged(state, batch)


def backward_clock() -> tuple[list[list[dict[str, object]]], record.TeamRoutines]:
    """Two delivered minutes, a completion whose clock fell back into the first, then one back in the second."""
    deliveries: list[list[dict[str, object]]] = []
    state = continuous()
    for start, end in ((MINUTE, MINUTE + 1), (MINUTE + 10, MINUTE + 11), (MINUTE + 60, MINUTE + 61)):
        state = run_once(state, start, end)
        if end != MINUTE + 1:
            deliveries.append(delivery(state))
            state = acknowledged(state, deliveries[-1])
    # A run whose clock fell back into a delivered minute shows nothing, as a run of none does.
    state = run_once(state, MINUTE + 30, MINUTE + 31)
    if state.notices:
        raise AssertionError(state.notices)
    deliveries.append(delivery(state))
    state = run_once(state, MINUTE + 70, MINUTE + 71)
    deliveries.append(delivery(state))
    return deliveries, acknowledged(state, deliveries[-1])


# The exact rollup deliveries Team makes in these cases; Admin replays them through its real transcript.
DELIVERY = json.loads((Path(http_routine.__file__).parent / "vectors.json").read_text()).get(
    "routine_rollup_delivery", {}
)


class DeliveryTests(unittest.TestCase):
    def test_a_change_restart_and_late_acknowledgment_in_one_minute_keep_counting_one_notice(self):
        deliveries, state = same_minute_change()
        self.assertEqual(deliveries, DELIVERY["same_minute_change"]["deliveries"])
        (first,), (last,) = deliveries
        self.assertEqual((last["notice_id"], last["version"], last["detail"]), (first["notice_id"], 3, {"runs": 3}))
        # The minute's usage sums every run of it, across the change and the restart.
        self.assertEqual(last["usage"], {"duration_ms": 3 * RUN_MS, "models": []})
        self.assertEqual(record.routine(state, ROUTINE_ID).revision, 2)
        self.assertEqual([item.outcome for item in state.notices], ["changed"])

    def test_a_clock_stepped_back_into_a_delivered_minute_never_republishes_it(self):
        deliveries, state = backward_clock()
        self.assertEqual(deliveries, DELIVERY["backward_clock"]["deliveries"])
        # The earlier minute is never published again; the later one goes on counting from where it was.
        self.assertEqual(
            [[(item["created_at"], item["version"]) for item in batch] for batch in deliveries],
            [
                [("2026-10-01T09:00:00Z", 2)],
                [("2026-10-01T09:01:00Z", 1)],
                [],
                [("2026-10-01T09:01:00Z", 2)],
            ],
        )
        current = record.routine(state, ROUTINE_ID)
        self.assertEqual((current.rollup_minute, current.rollup_runs, state.notices), (MINUTE + 60, 2, ()))


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
        self.assertEqual((notice.name, notice.usage["duration_ms"]), (NAME, 3 * RUN_MS))
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
        state, claim = routine_claim.claim(state, MINUTE, base.KEY)
        # A run that already published a notice, such as one a person answered, ends on that same notice.
        answered = dataclasses.replace(record.run(state, claim.run.run_id), notice_version=1)
        state = dataclasses.replace(state, runs=(answered,))
        lease = record.lease_of(claim.lease_token, base.KEY)
        state = routine_runs.finish(state, claim.run.run_id, lease, MINUTE + 1, "done", routine_fixture.DONE)
        self.assertEqual(
            [(item.outcome, item.notice_id, item.version) for item in state.notices], [("done", claim.run.run_id, 2)]
        )
        state, claim, lease = base.claimed()
        state = routine_runs.finish(state, claim.run.run_id, lease, base.NINE + 1, "done", routine_fixture.DONE)
        self.assertEqual([item.outcome for item in state.notices], ["done"])

    def test_a_minute_past_its_bound_rolls_up_nothing_more_and_shows_nothing(self):
        state = continuous()
        full = dataclasses.replace(
            record.routine(state, ROUTINE_ID), rollup_minute=MINUTE, rollup_runs=http_routine.MAX_ROLLUP_RUNS
        )
        state = dataclasses.replace(state, routines=(full,))
        state = run_once(state, MINUTE + 30, MINUTE + 31)
        self.assertEqual(state.notices, ())
        self.assertEqual(record.routine(state, ROUTINE_ID).rollup_runs, http_routine.MAX_ROLLUP_RUNS)

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
        # Every minute's one notice counts its twelve runs.
        self.assertEqual(set(delivered.values()), {12})


if __name__ == "__main__":
    unittest.main()
