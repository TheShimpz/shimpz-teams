"""Routine runs are claimed fairly and once, freeze and hold safely, and every outcome is delivered (ADR-0086)."""

from __future__ import annotations

import dataclasses
import datetime
import json
import unittest
from pathlib import Path

from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine as http_routine
from routine import record

UTC = datetime.UTC
KEY = "e" * 64
DIGEST = "sha256:" + "c" * 64
DAILY = {"kind": "daily", "time": "09:00"}
HOURLY = {"kind": "hourly", "every": 1}
BATCH = ("net_1:routine:" + "f" * 32, "d" * 64)


def epoch(*parts: int) -> int:
    return int(datetime.datetime(*parts, tzinfo=UTC).timestamp())


NINE = epoch(2026, 10, 1, 9)
ANCHOR = epoch(2026, 9, 1)


def routine(routine_id: str = "a" * 32, schedule: dict | None = None, *, anchor: int = ANCHOR) -> record.Routine:
    value = record.Routine(
        routine_id=routine_id,
        quote="Every day at 9, summarize the DNS changes.",
        schedule=dict(schedule or DAILY),
        timezone="UTC",
        assistants=(("dns", DIGEST),),
        anchor=anchor,
        next_run_at=0,
    )
    return dataclasses.replace(value, next_run_at=record.next_after(value, anchor))


def added(*routines: record.Routine) -> record.TeamRoutines:
    state = record.TeamRoutines()
    for value in routines:
        state = record.add_routine(state, value)
    return state


def at(state: record.TeamRoutines, routine_id: str, next_run_at: int) -> record.TeamRoutines:
    return record._replace_routine(
        state, dataclasses.replace(record.routine(state, routine_id), next_run_at=next_run_at)
    )


def claimed(now: int = NINE) -> tuple[record.TeamRoutines, record.Claim, record.Lease]:
    state, claim = record.claim(at(added(routine()), "a" * 32, NINE), now, KEY)
    return state, claim, record.lease_of(claim.lease_token, KEY)


def bound(now: int = NINE) -> tuple[record.TeamRoutines, record.Claim, record.Lease]:
    state, claim, lease = claimed(now)
    return record.bind_generation(state, claim.run.run_id, lease, now, "net_1"), claim, lease


def full_notices(count: int = record.MAX_UNDELIVERED_NOTICES) -> tuple[record.Notice, ...]:
    return tuple(
        record.Notice(f"{index:032x}", "c" * 32, "", "done", NINE, {"reply": "Done."}) for index in range(count)
    )


class ContractTests(unittest.TestCase):
    def test_the_notice_identifier_grammar_matches_the_payload_grammar(self):
        self.assertEqual(
            http_routine.ASSISTANT_ID_RE.pattern.removesuffix(r"\Z"), http_payload.ASSISTANT_ID_PATTERN[1:-1]
        )
        self.assertEqual(http_routine.ACTION_ID_RE.pattern.removesuffix(r"\Z"), http_payload.ACTION_ID_PATTERN[1:-1])

    def test_the_challenge_open_locales_match_the_chat_locales(self):
        self.assertEqual(http_routine.LOCALES, http_payload.CHAT_LOCALES)

    def test_notice_details_are_closed_and_never_carry_action_data(self):
        valid = {
            "done": {"reply": "Updated."},
            "needs-input": {"question": "Which zone?"},
            "skipped": {"missed": 3},
            "scope-changed": {"assistants": ["dns"]},
            "failed": {"code": "assistant-rpc-failed", "actions": [["dns", "list-zones"]]},
            "denied": {"actions": []},
            "stopped": {"actions": [["dns", "list-zones"]]},
            "uncertain": {"actions": [["dns", "replace-dns-record"]]},
            "frozen": {"request_kind": "human", "assistant_id": "dns", "action": "replace-dns-record"},
        }
        self.assertEqual(set(valid), http_routine.OUTCOMES)
        for outcome, detail in valid.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(http_routine.canonical_notice_detail(outcome, detail), detail)
        invalid = (
            ("done", {"reply": ""}),
            ("done", {"reply": " padded "}),
            ("done", {"reply": "x", "result": {"ip": "1.2.3.4"}}),
            ("needs-input", {"question": "x" * 241}),
            ("skipped", {"missed": 0}),
            ("skipped", {"missed": True}),
            ("scope-changed", {"assistants": []}),
            ("scope-changed", {"assistants": ["Bad"]}),
            ("failed", {"code": "Bad Code", "actions": []}),
            ("failed", {"code": "x", "actions": [["dns"]]}),
            ("stopped", {"actions": [["dns", {"input": 1}]]}),
            ("stopped", {"actions": "dns"}),
            ("interrupted", {"actions": []}),
            ("frozen", {"request_kind": "email", "assistant_id": "dns", "action": "x"}),
            ("frozen", {"request_kind": "human", "assistant_id": "Bad", "action": "x"}),
            ("frozen", {"request_kind": "human", "assistant_id": "dns", "action": ["x"]}),
            ("frozen", {"request_kind": "human", "assistant_id": "dns"}),
            (["done"], {"reply": "x"}),
            ("done", ["reply"]),
        )
        for outcome, detail in invalid:
            with self.subTest(outcome=outcome, detail=detail):
                self.assertIsNone(http_routine.canonical_notice_detail(outcome, detail))


class ChangeContractTests(unittest.TestCase):
    def test_a_brain_routine_change_is_one_closed_propose_or_cancel(self):
        propose = {
            "op": "propose",
            "quote": "Every Monday at 9, check the DNS",
            "schedule": {"kind": "weekly", "weekday": 0, "time": "09:00"},
            "timezone": None,
            "routine_id": None,
        }
        cancel = {
            "op": "cancel",
            "quote": "stop the daily summary",
            "schedule": None,
            "timezone": None,
            "routine_id": "a" * 32,
        }
        for value in (propose, {**propose, "timezone": "Europe/Lisbon"}, cancel):
            with self.subTest(value=value):
                self.assertEqual(http_routine.canonical_routine_change(value), value)
        for value in (
            None,
            {**propose, "extra": 1},
            {**propose, "quote": 7},
            {**propose, "quote": ""},
            {**propose, "quote": " padded "},
            {**propose, "quote": "x" * 501},
            {**propose, "quote": "check\nthe DNS"},
            {**propose, "quote": "check\u2028the DNS"},
            {**propose, "quote": "Cafe\u0301 check"},
            {**propose, "schedule": {"kind": "daily"}},
            {**propose, "routine_id": "a" * 32},
            {**propose, "timezone": "../etc"},
            {**cancel, "schedule": propose["schedule"]},
            {**cancel, "timezone": "UTC"},
            {**cancel, "routine_id": ["a" * 32]},
            {**cancel, "routine_id": "A" * 32},
            {**cancel, "op": "pause"},
        ):
            with self.subTest(value=value):
                self.assertIsNone(http_routine.canonical_routine_change(value))


class AddTests(unittest.TestCase):
    def test_only_a_closed_routine_is_admitted_and_it_is_copied(self):
        schedule = dict(DAILY)
        value = routine(schedule=schedule)
        state = added(value)
        value.schedule["time"] = "10:00"
        self.assertEqual(record.routine(state, "a" * 32).schedule, DAILY)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-exists"):
            record.add_routine(state, routine())
        good = routine()
        bad = (
            dataclasses.replace(good, routine_id="A" * 32),
            dataclasses.replace(good, quote=""),
            dataclasses.replace(good, quote="x" * 501),
            dataclasses.replace(good, quote="Every day\nat 9"),
            dataclasses.replace(good, schedule={"kind": "daily", "time": "25:00"}),
            dataclasses.replace(good, timezone="Mars/Olympus"),
            dataclasses.replace(good, assistants=()),
            dataclasses.replace(good, assistants=(("dns", DIGEST), ("dns", "sha256:" + "d" * 64))),
            dataclasses.replace(good, assistants=(("web", DIGEST), ("dns", DIGEST))),
            dataclasses.replace(good, assistants=(("Dns", DIGEST),)),
            dataclasses.replace(good, assistants=(("dns", "md5:x"),)),
            dataclasses.replace(good, anchor=float(ANCHOR)),
            dataclasses.replace(good, next_run_at=good.next_run_at + 1),
        )
        for candidate in bad:
            with self.subTest(candidate=candidate), self.assertRaisesRegex(record.RoutineStateError, "routine-invalid"):
                record.add_routine(record.TeamRoutines(), candidate)

    def test_a_team_holds_at_most_eight_routines_and_24_scheduled_runs_a_day(self):
        state = added(*(routine(f"{index:032x}") for index in range(record.MAX_ROUTINES)))
        with self.assertRaisesRegex(record.RoutineStateError, "routine-limit"):
            record.add_routine(state, routine("f" * 32))
        busy = added(routine("b" * 32, HOURLY))
        with self.assertRaisesRegex(record.RoutineStateError, "routine-rate-limit"):
            record.add_routine(busy, routine())
        with self.assertRaisesRegex(record.RoutineStateError, "routine-not-found"):
            record.routine(busy, "0" * 32)

    def test_the_first_firing_is_strictly_after_confirmation(self):
        self.assertEqual(routine(anchor=epoch(2026, 9, 1, 9, 30)).next_run_at, epoch(2026, 9, 2, 9))
        self.assertEqual(routine(anchor=epoch(2026, 9, 1, 9)).next_run_at, epoch(2026, 9, 2, 9))
        self.assertEqual(routine(schedule=HOURLY, anchor=NINE).next_run_at, NINE + 3600)


class ClaimTests(unittest.TestCase):
    def test_a_due_routine_is_claimed_once_under_a_lease_bound_to_the_routine_key(self):
        state = at(added(routine()), "a" * 32, NINE)
        self.assertIsNone(record.claimable(state, NINE - 1))
        self.assertEqual(record.claim(state, NINE - 1, KEY), (state, None))
        with self.assertRaisesRegex(record.RoutineStateError, "routine-key-invalid"):
            record.claim(state, NINE, "short")
        state, claim = record.claim(state, NINE, KEY)
        self.assertEqual((claim.run.status, claim.run.scheduled_at, claim.run.lease_key), ("leased", NINE, KEY))
        self.assertEqual(record.routine(state, "a" * 32).next_run_at, epoch(2026, 10, 2, 9))
        # A second claim on the same state finds nothing: the Routine never overlaps, even when due again.
        self.assertIsNone(record.claim(state, epoch(2026, 10, 2, 9), KEY)[1])
        value = record.run(state, claim.run.run_id)
        record.require_lease(value, record.lease_of(claim.lease_token, KEY), NINE + 1)
        for token, key, now in (
            ("other", KEY, NINE + 1),
            (claim.lease_token, "f" * 64, NINE + 1),
            (claim.lease_token, KEY, NINE + record.LEASE_SECONDS),
        ):
            with self.subTest(key=key, now=now), self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
                record.require_lease(value, record.lease_of(token, key), now)
        self.assertEqual(record.rekeyed(state, "f" * 64), (value,))
        self.assertEqual(record.rekeyed(state, KEY), ())

    def test_the_oldest_due_routine_wins_and_the_daily_start_cap_holds(self):
        eleven = epoch(2026, 10, 1, 23)
        state = at(at(added(routine("a" * 32), routine("b" * 32)), "a" * 32, eleven), "b" * 32, eleven - 60)
        self.assertEqual(record.claimable(state, eleven).routine_id, "b" * 32)
        capped = dataclasses.replace(state, starts_day="2026-10-01", starts=record.MAX_DAILY_STARTS)
        self.assertIsNone(record.claimable(capped, eleven))
        # The cap is per UTC day: the same late firings may start once the next day begins, within their grace.
        next_day = epoch(2026, 10, 2, 0, 30)
        after, claim = record.claim(capped, next_day, KEY)
        self.assertEqual((after.starts_day, after.starts, after.served_at), ("2026-10-02", 1, next_day))
        self.assertEqual(claim.run.routine_id, "b" * 32)

    def test_reconfirmation_and_deletion_stop_claims(self):
        state = record.mark_scope_changed(at(added(routine()), "a" * 32, NINE), "a" * 32, NINE, ["dns"])
        self.assertIsNone(record.claimable(state, NINE))
        self.assertEqual(
            [(notice.outcome, notice.detail) for notice in state.notices], [("scope-changed", {"assistants": ["dns"]})]
        )
        deleting, runs = record.begin_delete(at(added(routine()), "a" * 32, NINE), "a" * 32)
        self.assertEqual((runs, record.claimable(deleting, NINE)), ((), None))

    def test_only_one_late_firing_is_made_up_and_the_others_are_reported_once(self):
        state = at(added(routine("b" * 32, HOURLY)), "b" * 32, NINE)
        state, claim = record.claim(state, NINE + 40 * 60, KEY)
        value = record.routine(state, "b" * 32)
        self.assertEqual((claim.run.scheduled_at, value.next_run_at, state.notices), (NINE, NINE + 3600, ()))
        state = at(added(routine("b" * 32, HOURLY)), "b" * 32, NINE - 1800)
        state, _claim = record.claim(state, NINE + 1200, KEY)
        self.assertEqual([notice.detail for notice in state.notices], [{"missed": 1}])
        self.assertEqual(record.routine(state, "b" * 32).missed, 0)


class SweepTests(unittest.TestCase):
    def test_a_long_outage_is_one_skipped_notice_updated_as_the_gap_grows(self):
        now = NINE + 72 * 3600
        # Daily: a firing at most 12 hours late is still made up.
        daily = record.sweep(at(added(routine("a" * 32)), "a" * 32, NINE), now)
        self.assertEqual(
            ([notice.detail for notice in daily.notices], record.routine(daily, "a" * 32).next_run_at),
            (
                [{"missed": 3}],
                now,
            ),
        )
        # Hourly: every firing over one hour late is skipped; the one at the grace edge is still made up.
        state = record.sweep(at(added(routine("b" * 32, HOURLY)), "b" * 32, NINE), now)
        self.assertEqual([notice.detail for notice in state.notices], [{"missed": 71}])
        self.assertEqual(record.routine(state, "b" * 32).next_run_at, now - 3600)
        self.assertEqual(record.sweep(state, now), state)
        later = record.sweep(state, now + 5 * 3600)
        self.assertEqual(
            [(notice.notice_id, notice.detail) for notice in later.notices],
            [(state.notices[0].notice_id, {"missed": 76})],
        )
        # Acknowledging an older version keeps the updated notice; after delivery, the same gap is reported again
        # under the same notice, never as a second one.
        stale = record.acknowledge(later, frozenset({(state.notices[0].notice_id, state.notices[0].version)}))
        self.assertEqual(stale.notices, later.notices)
        delivered = record.acknowledge(later, frozenset((item.notice_id, item.version) for item in later.notices))
        again = record.sweep(delivered, now + 7 * 3600)
        self.assertEqual(
            [(notice.notice_id, notice.detail) for notice in again.notices],
            [(state.notices[0].notice_id, {"missed": 78})],
        )

    def test_counting_misses_is_bounded(self):
        hours = record.MAX_COUNTED_MISSES + 5
        state = record.sweep(at(added(routine("b" * 32, HOURLY)), "b" * 32, NINE), NINE + hours * 3600)
        self.assertEqual(state.notices[0].detail["missed"], record.MAX_COUNTED_MISSES)
        self.assertEqual(record.routine(state, "b" * 32).next_run_at, NINE + (hours - 1) * 3600)

    def test_a_full_notice_queue_blocks_claims_and_keeps_counting_skips(self):
        full = full_notices()
        state = dataclasses.replace(at(added(routine("b" * 32, HOURLY)), "b" * 32, NINE), notices=full)
        state = record.sweep(state, NINE + 5 * 3600)
        self.assertEqual((len(state.notices), record.routine(state, "b" * 32).missed), (32, 4))
        self.assertIsNone(record.claimable(state, NINE + 5 * 3600))
        state = record.sweep(record.acknowledge(state, frozenset({(full[0].notice_id, 1)})), NINE + 5 * 3600)
        self.assertEqual(state.notices[-1].detail, {"missed": 4})


class RunLifecycleTests(unittest.TestCase):
    def test_a_claim_sweeps_first_so_a_stale_firing_never_starts(self):
        now = NINE + 30 * 86_400
        state, claim = record.claim(at(added(routine()), "a" * 32, NINE), now, KEY)
        self.assertEqual(claim.run.scheduled_at, now)
        self.assertEqual([notice.detail for notice in state.notices], [{"missed": 30}])

    def test_worker_transitions_need_the_live_lease(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        expired = NINE + record.LEASE_SECONDS
        forged = record.lease_of("forged", KEY)
        attempts = (
            lambda value: record.spend(state, run_id, value, NINE + 1, 1),
            lambda value: record.freeze(state, run_id, value, NINE + 1, "human", "dns", "replace-dns-record"),
            lambda value: record.finish(state, run_id, value, NINE + 1, "done", {"reply": "Done."}),
            lambda value: record.hold_uncertain(state, run_id, value, NINE + 1, "d" * 64, {"actions": [["dns", "x"]]}),
            lambda value: record.bind_generation(state, run_id, value, NINE + 1, "net_2"),
        )
        for attempt in attempts:
            with self.subTest(attempt=attempt), self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
                attempt(forged)
        with self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
            record.finish(state, run_id, lease, expired, "done", {"reply": "Done."})

    def test_a_run_binds_its_generation_from_the_trusted_network_once(self):
        state, claim, lease = claimed()
        run_id = claim.run.run_id
        with self.assertRaisesRegex(record.RoutineStateError, "generation-invalid"):
            record.bind_generation(state, run_id, lease, NINE, "bad id")
        state = record.bind_generation(state, run_id, lease, NINE, "net_1")
        self.assertEqual(record.run(state, run_id).generation, "net_1:routine:" + run_id)
        self.assertEqual(record.bind_generation(state, run_id, lease, NINE, "net_1"), state)
        with self.assertRaisesRegex(record.RoutineStateError, "generation-invalid"):
            record.bind_generation(state, run_id, lease, NINE, "net_2")

    def test_a_claim_with_nothing_due_still_returns_the_swept_state(self):
        # A daily 09:00 Routine checked at 22:00 after a long outage: its misses are skipped, and nothing is due.
        state = at(added(routine()), "a" * 32, NINE - 5 * 86_400)
        swept, claim = record.claim(state, epoch(2026, 10, 1, 22), KEY)
        self.assertIsNone(claim)
        self.assertEqual([notice.detail for notice in swept.notices], [{"missed": 6}])
        self.assertEqual(record.routine(swept, "a" * 32).next_run_at, epoch(2026, 10, 2, 9))

    def test_a_frozen_run_holds_its_routine_and_resumes_under_a_fresh_lease(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        state = record.spend(state, run_id, lease, NINE, 30)
        state = record.freeze(state, run_id, lease, NINE, "human", "dns", "replace-dns-record")
        frozen = record.run(state, run_id)
        self.assertEqual((frozen.status, frozen.lease_sha256, frozen.active_seconds_left), ("frozen", "", 570))
        self.assertIsNone(record.claimable(state, epoch(2026, 10, 9, 9)))
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-running"):
            record.freeze(state, run_id, lease, NINE, "human", "dns", "replace-dns-record")
        state, token = record.thaw(state, run_id, NINE + 50)
        human = record.lease_of(token, record.HUMAN_LEASE)
        record.require_lease(record.run(state, run_id), record.lease_of(token, record.HUMAN_LEASE), NINE + 51)
        self.assertEqual(record.rekeyed(state, "f" * 64), ())
        state = record.spend(state, run_id, human, NINE + 51, 10)
        self.assertEqual(record.run(state, run_id).active_seconds_left, 560)
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-frozen"):
            record.thaw(state, run_id, NINE + 60)

    def test_invalid_freezes_and_durations_are_refused(self):
        state, claim, lease = claimed()
        for kind, assistant, action in (("mail", "dns", "list"), ("human", "Dns", "list"), ("human", "dns", "Bad")):
            with (
                self.subTest(kind=kind, assistant=assistant, action=action),
                self.assertRaisesRegex(record.RoutineStateError, "freeze-invalid"),
            ):
                record.freeze(state, claim.run.run_id, lease, NINE, kind, assistant, action)
        for seconds in (-1, 1.5, True):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(record.RoutineStateError, "invalid-duration"):
                record.spend(state, claim.run.run_id, lease, NINE, seconds)

    def test_a_team_freezes_at_most_eight_runs(self):
        state, claim, lease = claimed()
        frozen = tuple(record.Run(f"{index:032x}", f"{index + 1:032x}", "frozen", NINE) for index in range(8))
        state = dataclasses.replace(state, runs=(*state.runs, *frozen))
        self.assertEqual(record.run(state, frozen[-1].run_id), frozen[-1])
        with self.assertRaisesRegex(record.RoutineStateError, "frozen-limit"):
            record.freeze(state, claim.run.run_id, lease, NINE, "human", "dns", "replace-dns-record")

    def test_only_a_resolution_of_the_exact_batch_releases_an_uncertain_run(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        uncertain = {"actions": [["dns", "replace-dns-record"]]}
        unbound, unbound_claim, unbound_lease = claimed()
        with self.assertRaisesRegex(record.RoutineStateError, "batch-invalid"):
            record.hold_uncertain(unbound, unbound_claim.run.run_id, unbound_lease, NINE, "d" * 64, uncertain)
        with self.assertRaisesRegex(record.RoutineStateError, "batch-invalid"):
            record.hold_uncertain(state, run_id, lease, NINE, "short", uncertain)
        state = record.hold_uncertain(state, run_id, lease, NINE + 5, "d" * 64, uncertain)
        held = record.run(state, run_id)
        self.assertEqual((held.status, held.batch), ("uncertain", (record.generation_for("net_1", run_id), "d" * 64)))
        # Neither another outcome nor delivering its notice releases it.
        for outcome in ("done", "stopped", "uncertain"):
            with self.subTest(outcome=outcome), self.assertRaises(record.RoutineStateError):
                record.end(state, run_id, NINE, outcome, {"actions": []})
        state = record.acknowledge(state, frozenset((item.notice_id, item.version) for item in state.notices))
        self.assertEqual((state.notices, record.claimable(state, epoch(2026, 10, 2, 9))), ((), None))
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-uncertain"):
            record.resolve_uncertain(state, run_id, "e" * 64)
        leased, running, _lease = bound()
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-uncertain"):
            record.resolve_uncertain(leased, running.run.run_id, "d" * 64)
        state = record.resolve_uncertain(state, run_id, "d" * 64)
        self.assertEqual(record.claimable(state, epoch(2026, 10, 2, 9)).routine_id, "a" * 32)

    def test_team_ends_runs_without_their_lease_by_state(self):
        state, claim, _lease = bound()
        run_id = claim.run.run_id
        self.assertEqual(record.end(state, run_id, NINE, "stopped", {"actions": []}).runs, ())
        held = record.end(state, run_id, NINE, "uncertain", {"actions": [["dns", "x"]]}, "d" * 64)
        self.assertEqual(record.run(held, run_id).status, "uncertain")
        with self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
            record.end(state, run_id, NINE, "stopped", {"actions": []}, "d" * 64)
        for outcome in ("done", "denied", "uncertain"):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
                record.end(state, run_id, NINE, outcome, {"actions": [["dns", "x"]]})
        state, claim, lease = bound()
        frozen = record.freeze(state, claim.run.run_id, lease, NINE, "human", "dns", "replace-dns-record")
        denied = record.end(frozen, claim.run.run_id, NINE, "denied", {"actions": []})
        self.assertEqual(denied.notices[0].outcome, "denied")
        with self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
            record.end(frozen, claim.run.run_id, NINE, "done", {"reply": "x"})

    def test_worker_outcomes_are_closed(self):
        state, claim, lease = claimed()
        done = record.finish(state, claim.run.run_id, lease, NINE + 9, "done", {"reply": "Done."})
        self.assertEqual((done.runs, done.notices[0].outcome), ((), "done"))
        for outcome in ("skipped", "scope-changed", "uncertain", "unknown"):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
                record.finish(state, claim.run.run_id, lease, NINE, outcome, {"missed": 1})
        with self.assertRaisesRegex(record.RoutineStateError, "notice-invalid"):
            record.finish(state, claim.run.run_id, lease, NINE, "done", {"reply": "x", "result": {}})
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-found"):
            record.run(done, claim.run.run_id)

    def test_a_stored_notice_is_a_deep_copy_and_versions_are_positive(self):
        actions = [["dns", "list-zones"]]
        state, claim, lease = claimed()
        state = record.finish(state, claim.run.run_id, lease, NINE, "stopped", {"actions": actions})
        actions[0][1] = "replace-dns-record"
        self.assertEqual(state.notices[0].detail, {"actions": [["dns", "list-zones"]]})
        with self.assertRaisesRegex(record.RoutineStateError, "notice-invalid"):
            record._notice(state, record.Notice("n" * 32, "a" * 32, "", "skipped", NINE, {"missed": 1}, 0))

    def test_in_flight_outcomes_always_fit_above_the_claim_bound(self):
        state, claim, lease = claimed()
        state = dataclasses.replace(state, notices=full_notices())
        state = record.finish(state, claim.run.run_id, lease, NINE, "done", {"reply": "Done."})
        self.assertEqual(len(state.notices), record.MAX_UNDELIVERED_NOTICES + 1)
        over = dataclasses.replace(state, notices=full_notices(record.MAX_UNDELIVERED_NOTICES + record.MAX_ROUTINES))
        with self.assertRaisesRegex(record.RoutineStateError, "notices-full"):
            record.mark_scope_changed(over, "a" * 32, NINE, ["dns"])

    def test_leases_and_active_time_expire(self):
        state, claim, lease = claimed()
        self.assertEqual(record.expired(state, NINE + 10), ())
        spent = record.spend(state, claim.run.run_id, lease, NINE, record.ACTIVE_SECONDS)
        self.assertEqual([item.run_id for item in record.expired(spent, NINE + 10)], [claim.run.run_id])
        with self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
            record.require_lease(
                record.run(spent, claim.run.run_id), record.lease_of(claim.lease_token, KEY), NINE + 10
            )
        self.assertEqual(len(record.expired(state, NINE + record.LEASE_SECONDS)), 1)

    def test_deletion_keeps_runs_until_they_end_and_keeps_notices(self):
        state, claim, lease = claimed()
        state = record.finish(state, claim.run.run_id, lease, NINE, "stopped", {"actions": []})
        state, again = record.claim(at(state, "a" * 32, NINE + 86_400), NINE + 86_400, KEY)
        state, runs = record.begin_delete(state, "a" * 32)
        self.assertEqual([item.run_id for item in runs], [again.run.run_id])
        with self.assertRaisesRegex(record.RoutineStateError, "routine-busy"):
            record.complete_delete(state, "a" * 32)
        state = record.end(state, again.run.run_id, NINE + 86_400, "stopped", {"actions": []})
        state = record.complete_delete(state, "a" * 32)
        self.assertEqual((state.routines, len(state.notices)), ((), 2))
        with self.assertRaisesRegex(record.RoutineStateError, "routine-busy"):
            record.complete_delete(added(routine()), "a" * 32)

    def test_ids_and_lease_digests(self):
        self.assertRegex(record.new_id(), r"\A[0-9a-f]{32}\Z")
        self.assertEqual(len(record.lease_sha256("token")), 64)
        self.assertEqual(record.grace_seconds(routine()), 12 * 3600)
        self.assertEqual(record.grace_seconds(routine(schedule=HOURLY)), 3600)
        self.assertEqual(record.generation_for("net_1", "a" * 32), "net_1:routine:" + "a" * 32)


if __name__ == "__main__":
    unittest.main()


_JSON_TYPES = (None, True, 0, 1.5, "", "x", [], [[]], {}, {"k": []})


def _field_mutations(value, path=()):
    """Every nested field path of a JSON value, paired with each JSON type it could be replaced by."""
    children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, child in children:
        for replaced in _JSON_TYPES:
            yield (*path, key), replaced
        yield from _field_mutations(child, (*path, key))


def _replace(value, path, replaced):
    if not path:
        return replaced
    copied = dict(value) if isinstance(value, dict) else list(value)
    copied[path[0]] = _replace(value[path[0]], path[1:], replaced)
    return copied


class RoutineViewContractTests(unittest.TestCase):
    """Admin admits every Routine response only in its closed view; the golden vectors pin each one."""

    def test_claim_providers_are_exactly_the_team_model_providers(self):
        from inference import config as inference_config

        self.assertEqual(http_routine.MODEL_PROVIDERS, tuple(sorted(inference_config.PROVIDERS)))

    def test_every_view_admits_exactly_its_valid_vectors(self):
        views = json.loads((Path(__file__).resolve().parents[1] / "protocol/http/v1/vectors.json").read_text())[
            "routine_views"
        ]
        admit = {
            "proposal": http_routine.canonical_proposal,
            "preview": http_routine.canonical_preview,
            "routine": http_routine.canonical_routine_view,
            "run": http_routine.canonical_run_view,
            "notice_batch": http_routine.canonical_notice_batch,
            "claim": http_routine.canonical_claim,
            "claim_request": http_routine.canonical_claim_request,
        }
        for kind, function in admit.items():
            for value in views[kind]["valid"]:
                with self.subTest(kind=kind, value=value):
                    self.assertEqual(function(value), value)
            for value in views[kind]["invalid"]:
                with self.subTest(kind=kind, value=value):
                    self.assertIsNone(function(value))
        for function in admit.values():
            self.assertIsNone(function(["not", "a", "view"]))
        # Any field of any valid view replaced by any other JSON type is refused cleanly, never raised on.
        for kind, function in admit.items():
            for value in views[kind]["valid"]:
                for path, replaced in _field_mutations(value):
                    with self.subTest(kind=kind, path=path, replaced=replaced):
                        admitted = function(_replace(value, path, replaced))
                        self.assertIn(admitted, (None, _replace(value, path, replaced)))
        proposal = views["preview"]["valid"][0]
        self.assertIsNone(http_routine.canonical_preview({**proposal, "expires_in": "soon"}))
        self.assertIsNone(http_routine.canonical_notice_batch({"notices": "none", "more": False}))
        self.assertIsNone(http_routine.canonical_notice_batch({"notices": ["x"], "more": False}))
        # Two notices at their bound exceed a batch's encoded bound; one alone fits.
        largest = dict(views["notice_batch"]["valid"][0]["notices"][0], detail={"reply": "\x01" * 16_000})
        second = dict(largest, notice_id="9" * 32, run_id="9" * 32)
        self.assertIsNotNone(http_routine.canonical_notice_batch({"notices": [largest], "more": True}))
        self.assertIsNone(http_routine.canonical_notice_batch({"notices": [largest, second], "more": False}))
        self.assertIsNone(http_routine.canonical_claim({"run": ["x"]}))
