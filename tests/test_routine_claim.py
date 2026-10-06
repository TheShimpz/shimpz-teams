"""Due Routines are claimed fairly and once, and missed firings are swept and reported (ADR-0086, ADR-0092)."""

from __future__ import annotations

import dataclasses
import unittest

from test_routine_record import HOURLY, KEY, NINE, added, at, epoch, full_notices, routine

from routine import claim as routine_claim
from routine import hold as routine_hold
from routine import record


class ClaimTests(unittest.TestCase):
    def test_a_due_routine_is_claimed_once_under_a_lease_bound_to_the_routine_key(self):
        state = at(added(routine()), "a" * 32, NINE)
        self.assertIsNone(routine_claim.claimable(state, NINE - 1))
        self.assertEqual(routine_claim.claim(state, NINE - 1, KEY), (state, None))
        with self.assertRaisesRegex(record.RoutineStateError, "routine-key-invalid"):
            routine_claim.claim(state, NINE, "short")
        state, claim = routine_claim.claim(state, NINE, KEY)
        self.assertEqual((claim.run.status, claim.run.scheduled_at, claim.run.lease_key), ("leased", NINE, KEY))
        self.assertEqual(record.routine(state, "a" * 32).next_run_at, epoch(2026, 10, 2, 9))
        # A second claim on the same state finds nothing: the Routine never overlaps, even when due again.
        self.assertIsNone(routine_claim.claim(state, epoch(2026, 10, 2, 9), KEY)[1])
        value = record.run(state, claim.run.run_id)
        routine_claim.require_lease(value, record.lease_of(claim.lease_token, KEY), NINE + 1)
        for token, key, now in (
            ("other", KEY, NINE + 1),
            (claim.lease_token, "f" * 64, NINE + 1),
            (claim.lease_token, KEY, NINE + record.LEASE_SECONDS),
        ):
            with self.subTest(key=key, now=now), self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
                routine_claim.require_lease(value, record.lease_of(token, key), now)
        self.assertEqual(routine_hold.rekeyed(state, "f" * 64), (value,))
        self.assertEqual(routine_hold.rekeyed(state, KEY), ())

    def test_the_oldest_due_routine_wins_and_the_rolling_team_ceiling_holds_to_the_second(self):
        eleven = epoch(2026, 10, 1, 23)
        state = at(at(added(routine("a" * 32), routine("b" * 32)), "a" * 32, eleven), "b" * 32, eleven - 60)
        self.assertEqual(routine_claim.claimable(state, eleven).routine_id, "b" * 32)
        # The Team's daily steps count every start in the last 24 hours, whatever Routine made it, even a deleted one.
        first = eleven - 86_400 + 30
        full = tuple(("c" * 32, first + index, 1) for index in range(record.routine_starts.MAX_STARTS))
        capped = dataclasses.replace(state, starts=full)
        self.assertIsNone(routine_claim.claimable(capped, eleven))
        # The window rolls to the second: the oldest start leaves it exactly 24 hours after it was made.
        self.assertIsNone(routine_claim.claimable(capped, first + 86_400 - 1))
        self.assertEqual(routine_claim.next_due(capped, eleven), first + 86_400)
        after, claim = routine_claim.claim(capped, first + 86_400, KEY)
        self.assertEqual((claim.run.routine_id, len(after.starts)), ("b" * 32, record.routine_starts.MAX_STARTS))
        self.assertEqual(after.starts[-1], ("b" * 32, first + 86_400, 1))

    def test_reconfirmation_and_deletion_stop_claims(self):
        state = record.mark_scope_changed(at(added(routine()), "a" * 32, NINE), "a" * 32, NINE, ["dns"])
        self.assertIsNone(routine_claim.claimable(state, NINE))
        self.assertEqual(
            [(notice.outcome, notice.detail) for notice in state.notices], [("scope-changed", {"assistants": ["dns"]})]
        )
        deleting, runs = record.begin_delete(at(added(routine()), "a" * 32, NINE), "a" * 32)
        self.assertEqual((runs, routine_claim.claimable(deleting, NINE)), ((), None))

    def test_only_one_late_firing_is_made_up_and_the_others_are_reported_once(self):
        state = at(added(routine("b" * 32, HOURLY)), "b" * 32, NINE)
        state, claim = routine_claim.claim(state, NINE + 40 * 60, KEY)
        value = record.routine(state, "b" * 32)
        self.assertEqual((claim.run.scheduled_at, value.next_run_at, state.notices), (NINE, NINE + 3600, ()))
        state = at(added(routine("b" * 32, HOURLY)), "b" * 32, NINE - 1800)
        state, _claim = routine_claim.claim(state, NINE + 1200, KEY)
        self.assertEqual([notice.detail for notice in state.notices], [{"missed": 1}])
        self.assertEqual(record.routine(state, "b" * 32).missed, 0)


class SweepTests(unittest.TestCase):
    def test_a_long_outage_is_one_skipped_notice_updated_as_the_gap_grows(self):
        now = NINE + 72 * 3600
        # Daily: a firing at most 12 hours late is still made up.
        daily = routine_claim.sweep(at(added(routine("a" * 32)), "a" * 32, NINE), now)
        self.assertEqual(
            ([notice.detail for notice in daily.notices], record.routine(daily, "a" * 32).next_run_at),
            (
                [{"missed": 3}],
                now,
            ),
        )
        # Hourly: every firing over one hour late is skipped; the one at the grace edge is still made up.
        state = routine_claim.sweep(at(added(routine("b" * 32, HOURLY)), "b" * 32, NINE), now)
        self.assertEqual([notice.detail for notice in state.notices], [{"missed": 71}])
        self.assertEqual(record.routine(state, "b" * 32).next_run_at, now - 3600)
        self.assertEqual(routine_claim.sweep(state, now), state)
        later = routine_claim.sweep(state, now + 5 * 3600)
        self.assertEqual(
            [(notice.notice_id, notice.detail) for notice in later.notices],
            [(state.notices[0].notice_id, {"missed": 76})],
        )
        # Acknowledging an older version keeps the updated notice; after delivery, the same gap is reported again
        # under the same notice, never as a second one.
        stale = record.acknowledge(later, frozenset({(state.notices[0].notice_id, state.notices[0].version)}))
        self.assertEqual(stale.notices, later.notices)
        delivered = record.acknowledge(later, frozenset((item.notice_id, item.version) for item in later.notices))
        again = routine_claim.sweep(delivered, now + 7 * 3600)
        self.assertEqual(
            [(notice.notice_id, notice.detail) for notice in again.notices],
            [(state.notices[0].notice_id, {"missed": 78})],
        )

    def test_counting_misses_is_bounded(self):
        hours = record.MAX_COUNTED_MISSES + 5
        state = routine_claim.sweep(at(added(routine("b" * 32, HOURLY)), "b" * 32, NINE), NINE + hours * 3600)
        self.assertEqual(state.notices[0].detail["missed"], record.MAX_COUNTED_MISSES)
        self.assertEqual(record.routine(state, "b" * 32).next_run_at, NINE + (hours - 1) * 3600)

    def test_a_full_notice_queue_blocks_claims_and_keeps_counting_skips(self):
        full = full_notices()
        state = dataclasses.replace(at(added(routine("b" * 32, HOURLY)), "b" * 32, NINE), notices=full)
        state = routine_claim.sweep(state, NINE + 5 * 3600)
        self.assertEqual((len(state.notices), record.routine(state, "b" * 32).missed), (32, 4))
        self.assertIsNone(routine_claim.claimable(state, NINE + 5 * 3600))
        state = routine_claim.sweep(record.acknowledge(state, frozenset({(full[0].notice_id, 1)})), NINE + 5 * 3600)
        self.assertEqual(state.notices[-1].detail, {"missed": 4})


if __name__ == "__main__":
    unittest.main()
