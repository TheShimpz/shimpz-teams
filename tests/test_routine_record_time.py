"""A Routine run's time: long runs, leases, active time, holds, the Team's daily steps, and continuous cadence.

ADR-0092 sections 5 and 9, amended 2026-10-05 (scale).
"""

from __future__ import annotations

import dataclasses
import unittest

import routine_fixture
from test_routine_record import HOURLY, KEY, NINE, IncidentNoticeTests, added, at, bound, claimed, routine

from protocol.http.v1 import routine as http_routine
from routine import hold as routine_hold
from routine import plan as routine_plan
from routine import record

REPLAY = {"phase": "replay", "step": 1}


class ScaleClaimTests(unittest.TestCase):
    """Long runs, their leases, active time, and the Team's daily steps (ADR-0092 amendment, 2026-10-05, scale)."""

    @staticmethod
    def long_routine(routine_id: str = "b" * 32, steps: int = 40, schedule: dict | None = None) -> record.Routine:
        plan = routine_fixture.plan_document()
        plan["steps"] = [{**plan["steps"][0], "id": f"s{index}"} for index in range(steps)]
        plan["output"] = {"mode": "none", "step": None, "when": None}
        return routine_fixture.confirmed(dataclasses.replace(routine(routine_id, schedule), plan=plan))

    def test_a_long_routine_waits_while_admin_holds_a_long_run_and_a_short_one_is_still_served(self):
        state = at(at(added(self.long_routine(), routine()), "b" * 32, NINE - 60), "a" * 32, NINE)
        self.assertTrue(routine_plan.long_run(40))
        self.assertEqual(record.claimable(state, NINE).routine_id, "b" * 32)
        self.assertEqual(record.claimable(state, NINE, long=False).routine_id, "a" * 32)
        _state, claim = record.claim(state, NINE, KEY, long=False)
        self.assertEqual((claim.run.routine_id, claim.active_seconds), ("a" * 32, routine_plan.active_seconds(1)))
        _state, claim = record.claim(state, NINE, KEY)
        self.assertEqual((claim.run.routine_id, claim.active_seconds), ("b" * 32, routine_plan.active_seconds(40)))
        self.assertEqual(claim.run.lease_expires_at, NINE + record.LEASE_SECONDS)

    def test_a_segment_extends_its_lease_over_its_active_time_once(self):
        state, claim = record.claim(at(added(self.long_routine()), "b" * 32, NINE), NINE, KEY)
        lease = record.lease_of(claim.lease_token, KEY)
        run_id = claim.run.run_id
        bound_state = record.bind_generation(state, run_id, lease, NINE + 10, "net_1")
        left = record.run(bound_state, run_id).active_seconds_left
        expected = NINE + 10 + left + routine_plan.LEASE_MARGIN_SECONDS
        self.assertEqual(record.run(bound_state, run_id).lease_expires_at, expected)
        # A retried request binding the same run again never renews it.
        again = record.bind_generation(bound_state, run_id, lease, NINE + 500, "net_1")
        self.assertEqual(record.run(again, run_id).lease_expires_at, expected)

    def test_a_freeze_names_exactly_the_step_of_the_plan_it_waits_at(self):
        state, claim, lease = claimed()
        for request in (
            ("human", "dns", "check", {"phase": "replay", "step": 2}),
            ("human", "dns", "check", {"phase": "replay", "step": 0}),
            ("human", "dns", "other", {"phase": "replay", "step": 1}),
            ("permission", "dns", "other", {"phase": "decision", "call": 0}),
        ):
            with self.subTest(request=request), self.assertRaisesRegex(record.RoutineStateError, "freeze-invalid"):
                record.freeze(state, claim.run.run_id, lease, NINE, request)
        frozen = record.freeze(state, claim.run.run_id, lease, NINE, ("human", "dns", "check", REPLAY))
        self.assertEqual(frozen.notices[-1].detail["position"], REPLAY)
        # A decision call names its own order, whatever Action the plan's steps hold.
        call = {"phase": "decision", "call": 3}
        waiting = record.freeze(state, claim.run.run_id, lease, NINE, ("permission", "dns", "delete-record", call))
        notice = waiting.notices[-1]
        self.assertEqual(
            (notice.detail["request_kind"], notice.detail["position"], notice.detail["steps"]), ("permission", call, 1)
        )
        self.assertEqual(record.run(waiting, claim.run.run_id).position, call)
        thawed, _token = record.thaw(waiting, claim.run.run_id, NINE + 1, 0)
        self.assertEqual(
            (record.run(thawed, claim.run.run_id).position, record.run(thawed, claim.run.run_id).steps), (None, 0)
        )

    def test_a_start_reserves_every_step_of_its_revision_under_the_team_daily_steps(self):
        state = at(added(self.long_routine(schedule=HOURLY)), "b" * 32, NINE)
        full = tuple(("c" * 32, NINE - 3600 + index, 250) for index in range(80))
        capped = dataclasses.replace(state, starts=full)
        self.assertIsNone(record.claimable(capped, NINE))
        # Only once enough starts leave the window does the next one fit.
        freed = NINE - 3600 + 86_400
        self.assertEqual(record.next_due(capped, NINE), freed)
        after, _claim = record.claim(at(capped, "b" * 32, freed), freed, KEY)
        self.assertEqual(after.starts[-1], ("b" * 32, freed, 40))


# The active time of a run of the one-step Routine these tests claim.
ONE_STEP = routine_plan.active_seconds(1)


class HoldTimeTests(unittest.TestCase):
    def test_a_hold_keeps_the_run_balance_and_a_continuation_restores_it(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = record.spend(state, run_id, lease, NINE, (250, 250_400))
        state = routine_hold.settle_hold(routine_hold.fence(state, run_id, lease, NINE), run_id, NINE + 1, 1)
        self.assertEqual(routine_hold.incident(state, run_id).active_seconds_left, ONE_STEP - 250)
        self.assertEqual(routine_hold.incident(state, run_id).usage, {"duration_ms": 250_400, "models": []})
        reopened, _token = routine_hold.reopen_incident(
            state, run_id, NINE + 2, record.generation_for("net_1", run_id, "s1")
        )
        self.assertEqual(record.run(reopened, run_id).active_seconds_left, ONE_STEP - 250)
        self.assertEqual(record.run(reopened, run_id).usage["duration_ms"], 250_400)
        spent = dataclasses.replace(
            state, incidents=(dataclasses.replace(routine_hold.incident(state, run_id), active_seconds_left=0),)
        )
        with self.assertRaisesRegex(record.RoutineStateError, "run-time-exhausted"):
            routine_hold.reopen_incident(spent, run_id, NINE + 2, record.generation_for("net_1", run_id, "s1"))

    def test_a_runs_answered_human_requests_carry_through_a_hold_and_its_continuation(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = record._replace_run(state, dataclasses.replace(record.run(state, run_id), requests_used=15))
        state = routine_hold.settle_hold(routine_hold.fence(state, run_id, lease, NINE), run_id, NINE + 1, 1)
        self.assertEqual(routine_hold.incident(state, run_id).requests_used, 15)
        reopened, _token = routine_hold.reopen_incident(
            state, run_id, NINE + 2, record.generation_for("net_1", run_id, "s1")
        )
        self.assertEqual(record.run(reopened, run_id).requests_used, 15)
        # A thaw keeps the larger count: an answer only ever adds to it.
        frozen = record._replace_run(
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
                thawed, _token = record.thaw(frozen, run_id, NINE + 3, answered)
                self.assertEqual(record.run(thawed, run_id).requests_used, kept)

    def test_a_held_run_whose_routine_changed_or_went_gets_no_time_back(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = routine_hold.settle_hold(routine_hold.fence(state, run_id, lease, NINE), run_id, NINE + 1, 1)
        charged = routine_hold.charge_incident(state, run_id, 60)
        generation = routine_hold.incident(state, run_id).generation
        gone = dataclasses.replace(charged, routines=())
        self.assertIs(routine_hold.refund_incident(gone, run_id, generation, 10_000), gone)
        changed = record._replace_routine(charged, dataclasses.replace(charged.routines[0], revision=2))
        self.assertIs(routine_hold.refund_incident(changed, run_id, generation, 10_000), changed)

    def test_recovery_time_is_charged_and_refunded_only_against_the_same_held_run(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = routine_hold.settle_hold(routine_hold.fence(state, run_id, lease, NINE), run_id, NINE + 1, 1)
        charged = routine_hold.charge_incident(state, run_id, 60)
        self.assertEqual(routine_hold.incident(charged, run_id).active_seconds_left, ONE_STEP - 60)
        for seconds in (0, -1, True, ONE_STEP + 1):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(record.RoutineStateError, "time-invalid"):
                routine_hold.charge_incident(state, run_id, seconds)
        generation = routine_hold.incident(state, run_id).generation
        refunded = routine_hold.refund_incident(charged, run_id, generation, 1_000)
        self.assertEqual(routine_hold.incident(refunded, run_id).active_seconds_left, ONE_STEP)
        for target, seconds in ((("x", generation), 5), ((run_id, "other"), 5), ((run_id, generation), 0)):
            with self.subTest(target=target):
                self.assertIs(routine_hold.refund_incident(charged, *target, seconds), charged)


class ContinuousTests(unittest.TestCase):
    """A continuous Routine runs again its gap after each run ends, never overlapping, within its rolling cap."""

    def continuous(self, gap: int = 5) -> record.TeamRoutines:
        value = routine(schedule={"kind": "continuous", "gap": gap, "cap": http_routine.continuous_cap(gap)})
        return at(added(value), "a" * 32, NINE)

    def test_the_next_run_is_due_its_gap_after_the_previous_one_ended_and_never_overlaps(self):
        state = self.continuous()
        state, claim = record.claim(state, NINE, KEY)
        run_id = claim.run.run_id
        # The claim names how the run was scheduled; a scheduled Routine's claim says scheduled.
        self.assertEqual(claim.mode, "continuous")
        self.assertEqual(record.claim(at(added(routine()), "a" * 32, NINE), NINE, KEY)[1].mode, "scheduled")
        # While it runs nothing else of it starts, however long it takes.
        for later in (NINE + 5, NINE + 600):
            self.assertIsNone(record.claimable(state, later))
        self.assertIsNone(record.next_due(state, NINE + 600))
        lease = record.lease_of(claim.lease_token, KEY)
        ended = record.finish(state, run_id, lease, NINE + 40, "done", routine_fixture.DONE)
        self.assertEqual(record.routine(ended, "a" * 32).next_run_at, NINE + 45)
        self.assertIsNone(record.claimable(ended, NINE + 44))
        self.assertEqual(record.next_due(ended, NINE + 40), NINE + 45)
        self.assertEqual(record.claimable(ended, NINE + 45).routine_id, "a" * 32)
        # Any ending counts, a Team-decided one included.
        state, claim = record.claim(ended, NINE + 45, KEY)
        stopped = record.end(state, claim.run.run_id, NINE + 50, "stopped", {"actions": []})
        self.assertEqual(record.routine(stopped, "a" * 32).next_run_at, NINE + 55)

    def test_a_continuous_routine_never_skips_a_backlog_and_waits_out_its_cap_to_the_second(self):
        # Every twelve hours: at most two starts in any rolling 24 hours, even when a person forces earlier ones.
        state = self.continuous(gap=43_200)
        # A long outage reports no missed runs: it simply starts when it may.
        swept = record.sweep(state, NINE + 7 * 86_400)
        self.assertEqual((swept.notices, record.routine(swept, "a" * 32).missed), ((), 0))
        starts = []
        for _index in range(2):
            now = NINE + 100 * len(starts)
            state = at(state, "a" * 32, now)
            state, claim = record.claim(state, now, KEY)
            starts.append(now)
            state = record.end(state, claim.run.run_id, now + 1, "stopped", {"actions": []})
        # Its third start waits until its first leaves the rolling window, exactly.
        boundary = starts[0] + 86_400
        self.assertIsNone(record.claimable(state, boundary - 1))
        self.assertEqual(record.next_due(state, boundary - 1), boundary)
        self.assertEqual(record.claimable(state, boundary).routine_id, "a" * 32)

    def test_a_skipped_hold_ends_the_run_so_the_next_one_waits_its_gap_after_the_skip(self):
        state = self.continuous()
        state, claim = record.claim(state, NINE, KEY)
        run_id = claim.run.run_id
        lease = record.lease_of(claim.lease_token, KEY)
        state = record.bind_generation(state, run_id, lease, NINE, "net_1")
        state = routine_hold.settle_hold(routine_hold.fence(state, run_id, lease, NINE), run_id, NINE + 1)
        # Held for a day: the gap after the held run's own end has long passed, but the incident still holds it.
        skipped_at = NINE + 86_400
        self.assertIsNone(record.claimable(state, skipped_at))
        skipped = routine_hold.skip_incident(state, run_id, skipped_at, choice="run")
        self.assertEqual(record.routine(skipped, "a" * 32).next_run_at, skipped_at + 5)
        self.assertIsNone(record.claimable(skipped, skipped_at + 4))
        self.assertEqual(record.next_due(skipped, skipped_at), skipped_at + 5)
        self.assertEqual(record.claimable(skipped, skipped_at + 5).routine_id, "a" * 32)
        # Rodar on a continuous Routine asks for nothing more: it is due its gap after the run was set aside.
        generation = routine_hold.incident(state, run_id).generation
        ran = routine_hold.run_incident(state, run_id, skipped_at, routine_hold.Expected(1, generation, 1))
        self.assertEqual(record.routine(ran, "a" * 32).run_requested, 0)
        self.assertEqual(record.routine(ran, "a" * 32).next_run_at, skipped_at + 5)
        # A scheduled Routine's next firing is its own and a skip leaves it unchanged.
        scheduled, _run_id = IncidentNoticeTests().held()
        before = record.routine(scheduled, "a" * 32).next_run_at
        self.assertEqual(record.rebase_continuous(scheduled, "a" * 32, NINE + 10), scheduled)
        self.assertEqual(record.routine(scheduled, "a" * 32).next_run_at, before)


if __name__ == "__main__":
    unittest.main()


MODEL = {"provider": "openai", "model": "gpt-6-luna", "input_tokens": 10, "output_tokens": 2}


class UsageAndProtectionTests(unittest.TestCase):
    """A run's usage and lost protection go on through every hold and continuation (ADR-0101)."""

    def test_usage_sums_active_time_and_each_models_tokens_within_their_bounds(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = record.spend(state, run_id, lease, NINE, (1, 1500))
        state = record.used(state, run_id, [MODEL])
        state = record.used(state, run_id, [MODEL, {**MODEL, "provider": "anthropic", "model": "claude-sonnet-5-5"}])
        usage = record.run(state, run_id).usage
        self.assertEqual(usage["duration_ms"], 1500)
        self.assertEqual(
            [(item["provider"], item["input_tokens"]) for item in usage["models"]], [("anthropic", 10), ("openai", 20)]
        )
        with self.assertRaisesRegex(record.RoutineStateError, "usage-invalid"):
            record.used(state, run_id, [{**MODEL, "model": "Bad Model"}])
        capped = record.joined_usage(
            {"duration_ms": record.MAX_USAGE_MS, "models": [{**MODEL, "input_tokens": 999_999_999}]},
            {"duration_ms": 5, "models": [{**MODEL, "input_tokens": 9}]},
        )
        self.assertEqual(capped["duration_ms"], record.MAX_USAGE_MS)
        self.assertEqual(capped["models"][0]["input_tokens"], 1_000_000_000)
        many = [{**MODEL, "model": f"m{index:02d}"} for index in range(20)]
        self.assertEqual(
            len(record.joined_usage({"duration_ms": 0, "models": []}, {"duration_ms": 0, "models": many})["models"]), 16
        )

    def test_a_run_notice_carries_its_usage_and_a_lost_protection_for_good(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = record.lose_protection(record.used(state, run_id, [MODEL]), run_id)
        state = routine_hold.settle_hold(routine_hold.fence(state, run_id, lease, NINE), run_id, NINE + 1, 1)
        held = state.notices[-1]
        self.assertEqual(
            (held.outcome, held.protection_lost, held.usage["models"][0]["model"]), ("held", True, "gpt-6-luna")
        )
        incident = routine_hold.incident(state, run_id)
        self.assertTrue(incident.protection_lost)
        recovered = routine_hold.used(state, run_id, [MODEL])
        self.assertEqual(routine_hold.incident(recovered, run_id).usage["models"][0]["input_tokens"], 20)
        with self.assertRaisesRegex(record.RoutineStateError, "usage-invalid"):
            routine_hold.used(state, run_id, [{**MODEL, "provider": ""}])
        reopened, _token = routine_hold.reopen_incident(
            recovered, run_id, NINE + 2, record.generation_for("net_1", run_id, "s1")
        )
        run = record.run(reopened, run_id)
        self.assertTrue(run.protection_lost)
        self.assertEqual(run.usage["models"][0]["input_tokens"], 20)


if __name__ == "__main__":
    unittest.main()
