"""A claimed run spends, freezes, thaws, and ends exactly once, with its notice (ADR-0086, ADR-0092, ADR-0101)."""

import dataclasses
import unittest

import routine_fixture
from test_routine_record import HOURLY, KEY, NINE, STEP, added, at, bound, claimed, epoch, full_notices, routine

from routine import claim as routine_claim
from routine import definition as routine_definition
from routine import hold as routine_hold
from routine import plan as routine_plan
from routine import record
from routine import runs as routine_runs


class RunLifecycleTests(unittest.TestCase):
    def test_a_claim_sweeps_first_so_a_stale_firing_never_starts(self):
        now = NINE + 30 * 86_400
        state, claim = routine_claim.claim(at(added(routine()), "a" * 32, NINE), now, KEY)
        self.assertEqual(claim.run.scheduled_at, now)
        self.assertEqual([notice.detail for notice in state.notices], [{"missed": 30}])

    def test_worker_transitions_need_the_live_lease(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        expired = NINE + record.LEASE_SECONDS
        forged = record.lease_of("forged", KEY)
        attempts = (
            lambda value: routine_runs.spend(state, run_id, value, NINE + 1, (1, 1 * 1000)),
            lambda value: routine_runs.freeze(state, run_id, value, NINE + 1, ("human", "dns", "check", STEP)),
            lambda value: routine_runs.finish(state, run_id, value, NINE + 1, "done", routine_fixture.DONE),
            lambda value: routine_claim.bind_generation(state, run_id, value, NINE + 1, "net_2"),
        )
        for attempt in attempts:
            with self.subTest(attempt=attempt), self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
                attempt(forged)
        with self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
            routine_runs.finish(state, run_id, lease, expired, "done", routine_fixture.DONE)

    def test_a_run_binds_its_generation_from_the_trusted_network_once(self):
        state, claim, lease = claimed()
        run_id = claim.run.run_id
        with self.assertRaisesRegex(record.RoutineStateError, "generation-invalid"):
            routine_claim.bind_generation(state, run_id, lease, NINE, "bad id")
        state = routine_claim.bind_generation(state, run_id, lease, NINE, "net_1")
        self.assertEqual(record.run(state, run_id).generation, "net_1:routine:" + run_id)
        self.assertEqual(routine_claim.bind_generation(state, run_id, lease, NINE, "net_1"), state)
        with self.assertRaisesRegex(record.RoutineStateError, "generation-invalid"):
            routine_claim.bind_generation(state, run_id, lease, NINE, "net_2")

    def test_a_claim_with_nothing_due_still_returns_the_swept_state(self):
        # A daily 09:00 Routine checked at 22:00 after a long outage: its misses are skipped, and nothing is due.
        state = at(added(routine()), "a" * 32, NINE - 5 * 86_400)
        swept, claim = routine_claim.claim(state, epoch(2026, 10, 1, 22), KEY)
        self.assertIsNone(claim)
        self.assertEqual([notice.detail for notice in swept.notices], [{"missed": 6}])
        self.assertEqual(record.routine(swept, "a" * 32).next_run_at, epoch(2026, 10, 2, 9))

    def test_a_frozen_run_holds_its_routine_and_resumes_under_a_fresh_lease(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = routine_runs.spend(state, run_id, lease, NINE, (30, 30 * 1000))
        state = routine_runs.freeze(state, run_id, lease, NINE, ("human", "dns", "check", STEP))
        frozen = record.run(state, run_id)
        left = routine_plan.active_seconds(1) - 30
        self.assertEqual((frozen.status, frozen.lease_sha256, frozen.active_seconds_left), ("frozen", "", left))
        self.assertIsNone(routine_claim.claimable(state, epoch(2026, 10, 9, 9)))
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-running"):
            routine_runs.freeze(state, run_id, lease, NINE, ("human", "dns", "check", STEP))
        state, token = routine_runs.thaw(state, run_id, NINE + 50, 0)
        human = record.lease_of(token, record.HUMAN_LEASE)
        routine_claim.require_lease(record.run(state, run_id), record.lease_of(token, record.HUMAN_LEASE), NINE + 51)
        self.assertEqual(routine_hold.rekeyed(state, "f" * 64), ())
        state = routine_runs.spend(state, run_id, human, NINE + 51, (10, 10 * 1000))
        self.assertEqual(record.run(state, run_id).active_seconds_left, left - 10)
        # The person's answer runs at once, so its lease covers the run's active time left and a margin.
        self.assertEqual(
            record.run(state, run_id).lease_expires_at, NINE + 50 + left + routine_plan.LEASE_MARGIN_SECONDS
        )
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-frozen"):
            routine_runs.thaw(state, run_id, NINE + 60, 0)

    def test_invalid_freezes_and_durations_are_refused(self):
        state, claim, lease = claimed()
        for kind, assistant, action in (("mail", "dns", "list"), ("human", "Dns", "list"), ("human", "dns", "Bad")):
            with (
                self.subTest(kind=kind, assistant=assistant, action=action),
                self.assertRaisesRegex(record.RoutineStateError, "freeze-invalid"),
            ):
                routine_runs.freeze(state, claim.run.run_id, lease, NINE, (kind, assistant, action, STEP))
        for position in (
            {"phase": "replay", "step": 2},
            {"phase": "replay", "step": 0},
            {"phase": "decision", "call": 65},
            {"phase": "other", "step": 1},
            1,
        ):
            with self.subTest(position=position), self.assertRaisesRegex(record.RoutineStateError, "freeze-invalid"):
                routine_runs.freeze(state, claim.run.run_id, lease, NINE, ("human", "dns", "check", position))
        with self.assertRaisesRegex(record.RoutineStateError, "freeze-invalid"):
            routine_runs.freeze(state, claim.run.run_id, lease, NINE, ("human", "dns", "notify", STEP))
        for elapsed in ((-1, 0), (1.5, 0), (True, 0), (1, -1), (1, 1.5)):
            with self.subTest(elapsed=elapsed), self.assertRaisesRegex(record.RoutineStateError, "invalid-duration"):
                routine_runs.spend(state, claim.run.run_id, lease, NINE, elapsed)

    def test_a_team_freezes_at_most_eight_runs(self):
        state, claim, lease = claimed()
        frozen = tuple(record.Run(f"{index:032x}", f"{index + 1:032x}", "frozen", NINE) for index in range(8))
        state = dataclasses.replace(state, runs=(*state.runs, *frozen))
        self.assertEqual(record.run(state, frozen[-1].run_id), frozen[-1])
        with self.assertRaisesRegex(record.RoutineStateError, "frozen-limit"):
            routine_runs.freeze(state, claim.run.run_id, lease, NINE, ("human", "dns", "check", STEP))

    def test_team_ends_runs_without_their_lease_by_state(self):
        state, claim, _lease = bound()
        run_id = claim.run.run_id
        self.assertEqual(routine_runs.end(state, run_id, NINE, "stopped", {"actions": []}).runs, ())
        for outcome in ("done", "denied", "uncertain"):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
                routine_runs.end(state, run_id, NINE, outcome, {"actions": [["dns", "x"]]})
        state, claim, lease = bound()
        frozen = routine_runs.freeze(state, claim.run.run_id, lease, NINE, ("human", "dns", "check", STEP))
        denied = routine_runs.end(frozen, claim.run.run_id, NINE, "denied", {"actions": []})
        self.assertEqual(denied.notices[0].outcome, "denied")
        with self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
            routine_runs.end(frozen, claim.run.run_id, NINE, "done", routine_fixture.DONE)

    def test_worker_outcomes_are_closed(self):
        state, claim, lease = claimed()
        done = routine_runs.finish(state, claim.run.run_id, lease, NINE + 9, "done", routine_fixture.DONE)
        self.assertEqual((done.runs, done.notices[0].outcome), ((), "done"))
        for outcome in ("skipped", "scope-changed", "uncertain", "unknown"):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
                routine_runs.finish(state, claim.run.run_id, lease, NINE, outcome, {"missed": 1})
        with self.assertRaisesRegex(record.RoutineStateError, "notice-invalid"):
            routine_runs.finish(
                state, claim.run.run_id, lease, NINE, "stopped", {"actions": [["dns", "check"]], "result": {}}
            )
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-found"):
            record.run(done, claim.run.run_id)

    def test_a_stored_notice_is_a_deep_copy_and_versions_are_positive(self):
        actions = [["dns", "list-zones"]]
        state, claim, lease = claimed()
        state = routine_runs.finish(state, claim.run.run_id, lease, NINE, "stopped", {"actions": actions})
        actions[0][1] = "replace-dns-record"
        self.assertEqual(state.notices[0].detail, {"actions": [["dns", "list-zones"]]})
        with self.assertRaisesRegex(record.RoutineStateError, "notice-invalid"):
            record._notice(state, record.Notice("n" * 32, "a" * 32, "", "skipped", NINE, {"missed": 1}, 0))

    def test_in_flight_outcomes_always_fit_above_the_claim_bound(self):
        state, claim, lease = claimed()
        state = dataclasses.replace(state, notices=full_notices())
        state = routine_runs.finish(state, claim.run.run_id, lease, NINE, "done", routine_fixture.DONE)
        self.assertEqual(len(state.notices), record.MAX_UNDELIVERED_NOTICES + 1)
        over = dataclasses.replace(
            state, notices=full_notices(record.MAX_UNDELIVERED_NOTICES + record.MAX_ROUTINE_NOTICES)
        )
        with self.assertRaisesRegex(record.RoutineStateError, "notices-full"):
            record.mark_scope_changed(over, "a" * 32, NINE, ["dns"])

    def test_leases_and_active_time_expire(self):
        state, claim, lease = claimed()
        self.assertEqual(routine_hold.expired(state, NINE + 10), ())
        spent = routine_runs.spend(
            state, claim.run.run_id, lease, NINE, (record.ACTIVE_SECONDS, record.ACTIVE_SECONDS * 1000)
        )
        self.assertEqual([item.run_id for item in routine_hold.expired(spent, NINE + 10)], [claim.run.run_id])
        with self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
            routine_claim.require_lease(
                record.run(spent, claim.run.run_id), record.lease_of(claim.lease_token, KEY), NINE + 10
            )
        self.assertEqual(len(routine_hold.expired(state, NINE + record.LEASE_SECONDS)), 1)

    def test_deletion_keeps_runs_until_they_end_and_keeps_notices(self):
        state, claim, lease = claimed()
        state = routine_runs.finish(state, claim.run.run_id, lease, NINE, "stopped", {"actions": []})
        state, again = routine_claim.claim(at(state, "a" * 32, NINE + 86_400), NINE + 86_400, KEY)
        state, runs = record.begin_delete(state, "a" * 32)
        self.assertEqual([item.run_id for item in runs], [again.run.run_id])
        with self.assertRaisesRegex(record.RoutineStateError, "routine-busy"):
            record.complete_delete(state, "a" * 32, NINE)
        state = routine_runs.end(state, again.run.run_id, NINE + 86_400, "stopped", {"actions": []})
        state = record.complete_delete(state, "a" * 32, NINE + 86_400)
        self.assertEqual(
            (state.routines, [item.outcome for item in state.notices]), ((), ["stopped"] * 2 + ["deleted"])
        )
        with self.assertRaisesRegex(record.RoutineStateError, "routine-busy"):
            record.complete_delete(added(routine()), "a" * 32, NINE)

    def test_ids_and_lease_digests(self):
        self.assertRegex(record.new_id(), r"\A[0-9a-f]{32}\Z")
        self.assertEqual(len(record.lease_sha256("token")), 64)
        self.assertEqual(record.grace_seconds(routine()), 12 * 3600)
        self.assertEqual(record.grace_seconds(routine(schedule=HOURLY)), 3600)
        self.assertEqual(routine_claim.generation_for("net_1", "a" * 32), "net_1:routine:" + "a" * 32)


class FailureStreakTests(unittest.TestCase):
    def test_three_failures_in_a_row_pause_the_routine_and_a_success_resets_the_streak(self):
        state = added(routine())
        for index, outcome in enumerate(("failed", "failed", "done", "failed", "failed", "failed")):
            claimed_state, claim = routine_claim.claim(at(state, "a" * 32, NINE), NINE, KEY)
            state = (
                routine_runs.end(
                    claimed_state,
                    claim.run.run_id,
                    NINE + index,
                    outcome,
                    {"code": "x", "actions": [], "position": None, "steps": None},
                )
                if (outcome == "failed")
                else routine_runs.finish(
                    claimed_state,
                    claim.run.run_id,
                    record.lease_of(claim.lease_token, KEY),
                    NINE,
                    "done",
                    routine_fixture.DONE,
                )
            )
            state = dataclasses.replace(state, discards=(), starts=())
            current = record.routine(state, "a" * 32)
            with self.subTest(index=index):
                self.assertEqual(current.failures, (1, 2, 0, 1, 2, 3)[index])
                self.assertEqual(current.paused, index == 5)
        # A Stop or a denial is no execution failure.
        claimed_state, claim = routine_claim.claim(
            at(
                dataclasses.replace(
                    state, routines=(dataclasses.replace(record.routine(state, "a" * 32), paused=False, failures=2),)
                ),
                "a" * 32,
                NINE,
            ),
            NINE,
            KEY,
        )
        stopped = routine_runs.end(claimed_state, claim.run.run_id, NINE, "stopped", {"actions": []})
        self.assertEqual(record.routine(stopped, "a" * 32).failures, 2)


class RecoveredRunTests(unittest.TestCase):
    """The watchdog's lease-less endings touch only the exact leased run it read."""

    def test_a_recovered_run_is_held_or_done_only_under_the_lease_the_watchdog_read(self):
        state, claim, _lease = bound()
        run_id, lease_sha256 = claim.run.run_id, claim.run.lease_sha256
        held = record.run(routine_hold.hold_recovered(state, run_id, lease_sha256), run_id)
        self.assertEqual((held.status, held.lease_sha256, held.lease_expires_at), ("held", "", 0))
        done = routine_runs.complete_recovered(state, run_id, lease_sha256, NINE + 5)
        self.assertEqual(done.runs, ())
        unavailable = {"step": 1, "state": "unavailable", "value": None, "truncated": False}
        self.assertEqual(
            (done.notices[-1].outcome, done.notices[-1].detail),
            (
                "done",
                {
                    "plan": routine_definition.summary(routine_fixture.plan_document(), 1),
                    "output": unavailable,
                },
            ),
        )
        unbound, unclaimed, _lease = claimed()
        for transition, code in (
            (lambda: routine_hold.hold_recovered(state, run_id, "0" * 64), "run-changed"),
            (lambda: routine_runs.complete_recovered(state, run_id, "0" * 64, NINE), "run-changed"),
            (
                lambda: routine_hold.hold_recovered(unbound, unclaimed.run.run_id, unclaimed.run.lease_sha256),
                "generation-invalid",
            ),
            (
                lambda: routine_hold.hold_recovered(
                    routine_hold.hold_recovered(state, run_id, lease_sha256), run_id, lease_sha256
                ),
                "run-not-running",
            ),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(record.RoutineStateError, code):
                transition()


class TransitionEdgeTests(unittest.TestCase):
    def test_stale_or_misplaced_transitions_are_refused(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        with self.assertRaisesRegex(record.RoutineStateError, "run-changed"):
            routine_runs.end(state, run_id, NINE, "stopped", {"actions": []}, status="frozen")
        deleting, _runs = record.begin_delete(state, "a" * 32)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-deleting"):
            routine_runs.freeze(deleting, run_id, lease, NINE, ("human", "dns", "check", STEP))
        with self.assertRaisesRegex(record.RoutineStateError, "generation-invalid"):
            routine_claim.generation_for("net_1", run_id, "x9")
        queued = dataclasses.replace(state, discards=((run_id, "g1"), (run_id, "g2")))
        self.assertEqual(record.discarded(queued, run_id, "g1").discards, ((run_id, "g2"),))


if __name__ == "__main__":
    unittest.main()
