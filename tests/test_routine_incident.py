"""A held run's incident is settled and noticed exactly once (ADR-0092)."""

import dataclasses
import unittest

import routine_fixture
from routine_fixture import KEY, NINE, STEP, added, bound, claimed, held, routine

from routine import claim as routine_claim
from routine import hold as routine_hold
from routine import record
from routine import runs as routine_runs


class IncidentNoticeTests(unittest.TestCase):
    """A held run's one notice goes on through its incident: held, then paused or user-skipped (ADR-0092)."""

    def test_a_hold_names_its_step_and_its_notice_goes_on_through_the_incident(self):
        state, run_id = held()
        state = routine_hold.settle_hold(state, run_id, NINE + 1, ("dns", "replace-dns-record", STEP, 1))
        incident = routine_hold.incident(state, run_id)
        # The held run is its own incident: the same record, indexed when it was held.
        self.assertEqual(
            (incident.status, incident.held_at, incident.assistant_id, incident.action),
            ("unresolved", NINE + 1, "dns", "replace-dns-record"),
        )
        notice = state.notices[-1]
        self.assertEqual(
            (notice.notice_id, notice.outcome, notice.detail, notice.version),
            (
                run_id,
                "held",
                {},
                1,
            ),
        )
        self.assertEqual(incident.notice_version, 1)
        paused = routine_hold.pause_incident(state, run_id, NINE + 2, "exhausted")
        self.assertTrue(record.routine(paused, "a" * 32).paused)
        self.assertEqual(
            (paused.notices[-1].outcome, paused.notices[-1].detail, paused.notices[-1].version),
            (
                "paused",
                {"reason": "exhausted"},
                2,
            ),
        )
        with self.assertRaisesRegex(ValueError, "unknown Routine pause reason"):
            routine_hold.pause_incident(state, run_id, NINE, "bored")
        # A person setting this run aside is never the Routine's missed-schedule skip.
        skipped = routine_hold.skip_incident(paused, run_id, NINE + 3)
        self.assertEqual(routine_hold.incident(skipped, run_id).status, "skipped")
        self.assertEqual(
            (skipped.notices[-1].outcome, skipped.notices[-1].run_id, skipped.notices[-1].version),
            ("user-skipped", run_id, 3),
        )
        with self.assertRaisesRegex(record.RoutineStateError, "incident-not-unresolved"):
            routine_hold.pause_incident(skipped, run_id, NINE, "exhausted")
        self.assertEqual(skipped.notices[-1].name, routine().name)

    def test_rodar_keeps_one_fresh_run_pending_through_any_delay_and_never_moves_the_cadence(self):
        state, run_id = held()
        state = routine_hold.settle_hold(state, run_id, NINE + 1, ("dns", "replace-dns-record", STEP, 1))
        state = routine_hold.pause_incident(state, run_id, NINE + 2, "exhausted")
        cadence = record.routine(state, "a" * 32).next_run_at
        expected = routine_hold.Expected(1, routine_hold.incident(state, run_id).generation, 1)
        asked = routine_hold.run_incident(state, run_id, NINE + 10, expected)
        requested = record.routine(asked, "a" * 32)
        self.assertEqual(
            (requested.paused, requested.run_requested, requested.next_run_at), (False, NINE + 10, cadence)
        )
        notice = asked.notices[-1]
        self.assertEqual((notice.outcome, notice.detail), ("user-skipped", {}))
        self.assertEqual(routine_claim.next_due(asked, NINE + 5), NINE + 10)
        # A Team catching up its notices waits; the request outlasts the schedule's own grace and is never missed.
        late = cadence - 60
        self.assertGreater(late - (NINE + 10), record.grace_seconds(requested))
        claimed_state, claim = routine_claim.claim(asked, late, KEY)
        after = record.routine(claimed_state, "a" * 32)
        self.assertEqual((claim.run.scheduled_at, after.run_requested, after.next_run_at), (NINE + 10, 0, cadence))
        self.assertEqual(claimed_state.starts[-1], ("a" * 32, late, 1))
        # A firing due at the same time serves the request too: one run, never two.
        both, claim = routine_claim.claim(asked, cadence, KEY)
        self.assertEqual((claim.run.scheduled_at, record.routine(both, "a" * 32).run_requested), (cadence, 0))
        self.assertEqual(len(record.live_runs(both)), 1)
        # The card's state is checked in the same write.
        with self.assertRaisesRegex(record.RoutineStateError, "incident-changed"):
            routine_hold.run_incident(state, run_id, NINE + 10, dataclasses.replace(expected, current=2))

    def test_deleting_a_routine_sets_a_run_held_afterwards_aside_as_it_is_indexed(self):
        state, run_id = held()
        state, _runs = record.begin_delete(state, "a" * 32)
        state = routine_hold.settle_hold(state, run_id, NINE + 1, ("dns", "replace-dns-record", STEP, 1))
        self.assertEqual(routine_hold.incident(state, run_id).status, "skipped")
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail), ("user-skipped", {}))

    def test_a_resume_starts_a_fresh_streak_and_a_deleting_routine_never_pauses_or_resumes(self):
        state = added(routine())
        state = dataclasses.replace(state, routines=(dataclasses.replace(state.routines[0], failures=3, paused=True),))
        resumed = record.routine(record.set_paused(state, "a" * 32, False), "a" * 32)
        self.assertEqual((resumed.paused, resumed.failures), (False, 0))
        paused = record.routine(record.set_paused(state, "a" * 32, True), "a" * 32)
        self.assertEqual(paused.failures, 3)
        deleting, _runs = record.begin_delete(state, "a" * 32)
        for value in (True, False):
            with self.subTest(paused=value), self.assertRaisesRegex(record.RoutineStateError, "routine-not-found"):
                record.set_paused(deleting, "a" * 32, value)

    def test_the_wake_hint_is_the_earliest_future_firing_of_a_routine_that_may_start(self):
        ids = [f"{index:x}" * 32 for index in range(1, 7)]
        state = added(*(routine(routine_id) for routine_id in ids))
        changes = {
            ids[0]: {"next_run_at": NINE - 1},
            ids[1]: {"next_run_at": NINE + 10, "paused": True},
            ids[2]: {"next_run_at": NINE + 20, "deleting": True},
            ids[3]: {"next_run_at": NINE + 30, "needs_reconfirm": True},
            ids[4]: {"next_run_at": NINE + 40},
            ids[5]: {"next_run_at": NINE + 50},
        }
        state = dataclasses.replace(
            state, routines=tuple(dataclasses.replace(item, **changes[item.routine_id]) for item in state.routines)
        )
        self.assertEqual(routine_claim.next_due(state, NINE), NINE + 40)
        # A Routine with a live run or an unresolved incident wakes nothing; its own end or resolution does.
        busy = dataclasses.replace(state, runs=(record.Run("f" * 32, ids[4], "frozen", 0),))
        self.assertEqual(routine_claim.next_due(busy, NINE), NINE + 50)
        # A leased run holds the Team's one slot: no other Routine of the Team is claimed or hinted until it ends.
        leased = dataclasses.replace(state, runs=(record.Run("f" * 32, ids[4], "leased", 0),))
        self.assertEqual(routine_claim.claimable(busy, NINE).routine_id, ids[0])
        self.assertIsNone(routine_claim.claimable(leased, NINE))
        self.assertIsNone(routine_claim.next_due(leased, NINE))
        incident = record.Run("e" * 32, ids[5], "unresolved", 0, generation="g", held_at=NINE)
        held = dataclasses.replace(busy, runs=(*busy.runs, incident))
        self.assertIsNone(routine_claim.next_due(held, NINE))

    def test_a_hold_without_a_sealed_cursor_names_no_step(self):
        state, run_id = held()
        state = routine_hold.settle_hold(state, run_id, NINE + 1)
        self.assertEqual(routine_hold.held_step(routine_hold.incident(state, run_id)), routine_hold.UNKNOWN_STEP)

    def test_a_completed_continuation_is_recovered_and_resets_the_streak(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        self.assertEqual(routine_runs.completed(record.run(state, run_id)), "done")
        continued = record.run(state, run_id)
        continued = dataclasses.replace(continued, generation=routine_claim.generation_for("net_1", run_id, "s1"))
        self.assertEqual(routine_runs.completed(continued), "recovered")
        streak = dataclasses.replace(record.routine(state, "a" * 32), failures=2)
        state = dataclasses.replace(state, routines=(streak,))
        ended = routine_runs.finish(state, run_id, lease, NINE + 1, "recovered", routine_fixture.DONE)
        self.assertEqual((ended.notices[-1].outcome, record.routine(ended, "a" * 32).failures), ("recovered", 0))


class HoldEdgeTests(unittest.TestCase):
    def test_a_hold_settles_only_a_held_run_and_keeps_its_one_record(self):
        state, claim, _lease = bound()
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-held"):
            routine_hold.settle_hold(state, claim.run.run_id, NINE + 1)
        unbound, unbound_claim, unbound_lease = claimed()
        with self.assertRaisesRegex(record.RoutineStateError, "generation-invalid"):
            routine_hold.fence(unbound, unbound_claim.run.run_id, unbound_lease, NINE)
        fenced, run_id = held()
        settled = routine_hold.settle_hold(fenced, run_id, NINE + 1)
        # Indexing needs no room of its own: the claim reserved the run's record, which becomes its incident.
        self.assertEqual(([item.run_id for item in settled.runs], record.live_runs(settled)), ([run_id], ()))

    def test_an_incident_reopens_only_once_unresolved_resumable_and_idle(self):
        fenced, run_id = held()
        state = routine_hold.settle_hold(fenced, run_id, NINE + 1)
        generation = routine_claim.generation_for("net_1", run_id, "s1")
        with self.assertRaisesRegex(record.RoutineStateError, "incident-not-found"):
            routine_hold.incident(state, "0" * 32)
        paused = record.set_paused(state, "a" * 32, True)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-not-resumable"):
            routine_hold.reopen_incident(paused, run_id, NINE + 2, generation)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-busy"):
            routine_hold.reopen_incident(state, run_id, NINE + 2, "net_1:routine:" + "0" * 32)
        skipped = routine_hold.skip_incident(state, run_id, NINE + 2)
        for transition in (
            lambda: routine_hold.reopen_incident(skipped, run_id, NINE + 3, generation),
            lambda: routine_hold.skip_incident(skipped, run_id, NINE + 3),
            lambda: routine_hold.release_incident(state, run_id),
        ):
            with self.subTest(transition=transition), self.assertRaises(record.RoutineStateError):
                transition()
        # A released incident's record goes; releasing it again finds it already released.
        released = routine_hold.release_incident(skipped, run_id)
        self.assertEqual(released.runs, ())
        self.assertIs(routine_hold.release_incident(released, run_id), released)


if __name__ == "__main__":
    unittest.main()
