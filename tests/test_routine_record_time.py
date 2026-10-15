"""A Routine run's time: long runs, leases, active time, holds, the Team's daily steps, and continuous cadence.

ADR-0092 sections 5 and 9, amended 2026-10-05 (scale).
"""

import dataclasses
import unittest

import routine_fixture
from routine_fixture import EVERY_HOUR, KEY, NINE, added, at, bound, claimed, held, routine

from routine import claim as routine_claim
from routine import hold as routine_hold
from routine import plan as routine_plan
from routine import record
from routine import runs as routine_runs

REPLAY = 1


class ScaleClaimTests(unittest.TestCase):
    """Long runs, their leases, active time, and the Team's daily steps (ADR-0092 amendment, 2026-10-05, scale)."""

    @staticmethod
    def long_routine(routine_id: str = "b" * 32, steps: int = 40, schedule: dict | None = None) -> record.Routine:
        plan = routine_fixture.plan_document()
        plan["steps"] = [{**plan["steps"][0], "id": f"s{index}"} for index in range(steps)]
        plan["output"] = {"mode": "none", "step": None}
        return routine_fixture.confirmed(dataclasses.replace(routine(routine_id, schedule), plan=plan))

    def test_a_long_routine_waits_while_admin_holds_a_long_run_and_a_short_one_is_still_served(self):
        state = at(at(added(self.long_routine(), routine()), "b" * 32, NINE - 60), "a" * 32, NINE)
        self.assertTrue(routine_plan.long_run(40))
        self.assertEqual(routine_claim.claimable(state, NINE).routine_id, "b" * 32)
        self.assertEqual(routine_claim.claimable(state, NINE, long=False).routine_id, "a" * 32)
        _state, claim = routine_claim.claim(state, NINE, KEY, long=False)
        self.assertEqual((claim.run.routine_id, claim.active_seconds), ("a" * 32, routine_plan.active_seconds(1)))
        _state, claim = routine_claim.claim(state, NINE, KEY)
        self.assertEqual((claim.run.routine_id, claim.active_seconds), ("b" * 32, routine_plan.active_seconds(40)))
        self.assertEqual(claim.run.lease_expires_at, NINE + record.LEASE_SECONDS)

    def test_a_segment_extends_its_lease_over_its_active_time_once(self):
        state, claim = routine_claim.claim(at(added(self.long_routine()), "b" * 32, NINE), NINE, KEY)
        lease = record.lease_of(claim.lease_token, KEY)
        run_id = claim.run.run_id
        bound_state = routine_claim.bind_generation(state, run_id, lease, NINE + 10, "net_1")
        left = record.run(bound_state, run_id).active_seconds_left
        expected = NINE + 10 + left + routine_plan.LEASE_MARGIN_SECONDS
        self.assertEqual(record.run(bound_state, run_id).lease_expires_at, expected)
        # A retried request binding the same run again never renews it.
        again = routine_claim.bind_generation(bound_state, run_id, lease, NINE + 500, "net_1")
        self.assertEqual(record.run(again, run_id).lease_expires_at, expected)

    def test_a_freeze_names_exactly_the_step_of_the_plan_it_waits_at(self):
        state, claim, lease = claimed()
        for request in (
            ("human", "dns", "check", 2),
            ("human", "dns", "check", 0),
            ("human", "dns", "other", 1),
        ):
            with self.subTest(request=request), self.assertRaisesRegex(record.RoutineStateError, "freeze-invalid"):
                routine_runs.freeze(state, claim.run.run_id, lease, NINE, request)
        frozen = routine_runs.freeze(state, claim.run.run_id, lease, NINE, ("human", "dns", "check", REPLAY))
        self.assertEqual(frozen.notices[-1].detail, {"request_kind": "human"})
        self.assertEqual(record.run(frozen, claim.run.run_id).position, REPLAY)
        thawed, _token = routine_runs.thaw(frozen, claim.run.run_id, NINE + 1, 0)
        self.assertEqual(
            (record.run(thawed, claim.run.run_id).position, record.run(thawed, claim.run.run_id).steps), (None, 0)
        )

    def test_a_start_reserves_every_step_of_its_revision_under_the_team_daily_steps(self):
        state = at(added(self.long_routine(schedule=EVERY_HOUR)), "b" * 32, NINE)
        full = tuple(("c" * 32, NINE - 3600 + index, 250) for index in range(80))
        capped = dataclasses.replace(state, starts=full)
        self.assertIsNone(routine_claim.claimable(capped, NINE))
        # Only once enough starts leave the window does the next one fit.
        freed = NINE - 3600 + 86_400
        self.assertEqual(routine_claim.next_due(capped, NINE), freed)
        after, _claim = routine_claim.claim(at(capped, "b" * 32, freed), freed, KEY)
        self.assertEqual(after.starts[-1], ("b" * 32, freed, 40))


# The active time of a run of the one-step Routine these tests claim.
ONE_STEP = routine_plan.active_seconds(1)


class HoldTimeTests(unittest.TestCase):
    def test_a_hold_keeps_the_run_balance_and_a_continuation_restores_it(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = routine_runs.spend(state, run_id, lease, NINE, (250, 250_400))
        state = routine_hold.settle_hold(routine_hold.fence(state, run_id, lease, NINE), run_id, NINE + 1)
        self.assertEqual(routine_hold.incident(state, run_id).active_seconds_left, ONE_STEP - 250)
        self.assertEqual(routine_hold.incident(state, run_id).usage, {"duration_ms": 250_400})
        reopened, _token = routine_hold.reopen_incident(
            state, run_id, NINE + 2, routine_claim.generation_for("net_1", run_id, "s1")
        )
        self.assertEqual(record.run(reopened, run_id).active_seconds_left, ONE_STEP - 250)
        self.assertEqual(record.run(reopened, run_id).usage["duration_ms"], 250_400)
        spent = record.replace_run(
            state, dataclasses.replace(routine_hold.incident(state, run_id), active_seconds_left=0)
        )
        with self.assertRaisesRegex(record.RoutineStateError, "run-time-exhausted"):
            routine_hold.reopen_incident(spent, run_id, NINE + 2, routine_claim.generation_for("net_1", run_id, "s1"))

    def test_a_runs_answered_human_requests_carry_through_a_hold_and_its_continuation(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = record.replace_run(state, dataclasses.replace(record.run(state, run_id), requests_used=15))
        state = routine_hold.settle_hold(routine_hold.fence(state, run_id, lease, NINE), run_id, NINE + 1)
        self.assertEqual(routine_hold.incident(state, run_id).requests_used, 15)
        reopened, _token = routine_hold.reopen_incident(
            state, run_id, NINE + 2, routine_claim.generation_for("net_1", run_id, "s1")
        )
        self.assertEqual(record.run(reopened, run_id).requests_used, 15)
        # A thaw keeps the larger count: an answer only ever adds to it.
        frozen = record.replace_run(
            reopened,
            dataclasses.replace(
                record.run(reopened, run_id),
                status="frozen",
                lease_sha256="",
                lease_key="",
                lease_expires_at=0,
                request_kind="human",
                assistant_id="shimpz-cloudflare",
                action="list-zones",
            ),
        )
        for answered, kept in ((16, 16), (3, 15)):
            with self.subTest(answered=answered):
                thawed, _token = routine_runs.thaw(frozen, run_id, NINE + 3, answered)
                self.assertEqual(record.run(thawed, run_id).requests_used, kept)


class ContinuousTests(unittest.TestCase):
    """A continuous Routine runs again its gap after each run ends, never overlapping, within its rolling cap."""

    def continuous(self, gap: int = 5) -> record.TeamRoutines:
        value = routine(schedule={"kind": "continuous", "gap": gap})
        return at(added(value), "a" * 32, NINE)

    def test_the_next_run_is_due_its_gap_after_the_previous_one_started_and_never_overlaps(self):
        state = self.continuous()
        state, claim = routine_claim.claim(state, NINE, KEY)
        run_id = claim.run.run_id
        # While it runs nothing else of it starts, however long it takes.
        for later in (NINE + 5, NINE + 600):
            self.assertIsNone(routine_claim.claimable(state, later))
        self.assertIsNone(routine_claim.next_due(state, NINE + 600))
        # A run that outlasted its gap makes the next one due at its end: no overlap, no backlog.
        lease = record.lease_of(claim.lease_token, KEY)
        ended = routine_runs.finish(state, run_id, lease, NINE + 40, "done", routine_fixture.DONE)
        self.assertEqual(record.routine(ended, "a" * 32).next_run_at, NINE + 40)
        self.assertEqual(routine_claim.claimable(ended, NINE + 40).routine_id, "a" * 32)
        # A shorter run keeps the interval from its own start; any ending counts, a Team-decided one included.
        state, claim = routine_claim.claim(ended, NINE + 40, KEY)
        stopped = routine_runs.end(state, claim.run.run_id, NINE + 42, "stopped", {})
        self.assertEqual(record.routine(stopped, "a" * 32).next_run_at, NINE + 45)
        self.assertIsNone(routine_claim.claimable(stopped, NINE + 44))
        self.assertEqual(routine_claim.next_due(stopped, NINE + 42), NINE + 45)

    def test_a_continuous_routine_never_skips_a_backlog_and_waits_out_its_cap_to_the_second(self):
        # Every twelve hours: at most two starts in any rolling 24 hours, even when a person forces earlier ones.
        state = self.continuous(gap=43_200)
        # A long outage reports no missed runs: it simply starts when it may.
        swept = routine_claim.sweep(state, NINE + 7 * 86_400)
        self.assertEqual((swept.notices, record.routine(swept, "a" * 32).missed), ((), 0))
        starts = []
        for _index in range(2):
            now = NINE + 100 * len(starts)
            state = at(state, "a" * 32, now)
            state, claim = routine_claim.claim(state, now, KEY)
            starts.append(now)
            state = routine_runs.end(state, claim.run.run_id, now + 1, "stopped", {})
        # Its third start waits until its first leaves the rolling window, exactly.
        boundary = starts[0] + 86_400
        self.assertIsNone(routine_claim.claimable(state, boundary - 1))
        self.assertEqual(routine_claim.next_due(state, boundary - 1), boundary)
        self.assertEqual(routine_claim.claimable(state, boundary).routine_id, "a" * 32)

    def test_rodar_on_a_set_aside_run_starts_the_routine_now_whatever_its_schedule(self):
        state = self.continuous()
        state, claim, lease = routine_fixture.claim_bound(state, NINE, network="net_1")
        run_id = claim.run.run_id
        state = routine_hold.settle_hold(routine_hold.fence(state, run_id, lease, NINE), run_id, NINE + 1)
        # Held for a day: the incident still holds the Routine, whatever its gap.
        later = NINE + 86_400
        self.assertIsNone(routine_claim.claimable(state, later))
        generation = routine_hold.incident(state, run_id).generation
        ran = routine_hold.run_incident(state, run_id, later, routine_hold.Expected(1, generation, 1))
        self.assertEqual(record.routine(ran, "a" * 32).run_requested, later)
        self.assertEqual(routine_claim.claimable(ran, later).routine_id, "a" * 32)
        # A scheduled Routine's next firing is its own: ending a run leaves it unchanged.
        scheduled, _run_id = held()
        before = record.routine(scheduled, "a" * 32).next_run_at
        self.assertEqual(record.rebase_continuous(scheduled, "a" * 32, NINE + 10), scheduled)
        self.assertEqual(record.routine(scheduled, "a" * 32).next_run_at, before)


if __name__ == "__main__":
    unittest.main()


class UsageAndProtectionTests(unittest.TestCase):
    """A run's usage and lost protection go on through every hold and continuation (ADR-0101)."""

    def test_usage_sums_active_time_within_its_bound(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = routine_runs.spend(state, run_id, lease, NINE, (1, 1500))
        self.assertEqual(record.run(state, run_id).usage, {"duration_ms": 1500})
        self.assertEqual(routine_runs.joined_usage({"duration_ms": 1}, {"duration_ms": 2}), {"duration_ms": 3})
        capped = routine_runs.joined_usage({"duration_ms": routine_runs.MAX_USAGE_MS}, {"duration_ms": 5})
        self.assertEqual(capped, {"duration_ms": routine_runs.MAX_USAGE_MS})

    def test_a_run_notice_carries_its_usage_and_a_lost_protection_for_good(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = routine_runs.spend(state, run_id, lease, NINE, (1, 1500))
        state = routine_runs.lose_protection(state, run_id)
        state = routine_hold.settle_hold(routine_hold.fence(state, run_id, lease, NINE), run_id, NINE + 1)
        held = state.notices[-1]
        self.assertEqual((held.outcome, held.protection_lost, held.usage["duration_ms"]), ("held", True, 1500))
        incident = routine_hold.incident(state, run_id)
        self.assertTrue(incident.protection_lost)
        reopened, _token = routine_hold.reopen_incident(
            state, run_id, NINE + 2, routine_claim.generation_for("net_1", run_id, "s1")
        )
        run = record.run(reopened, run_id)
        self.assertTrue(run.protection_lost)
        self.assertEqual(run.usage["duration_ms"], 1500)


if __name__ == "__main__":
    unittest.main()
