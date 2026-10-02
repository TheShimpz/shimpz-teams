"""Routine runs are claimed fairly and once, freeze and hold safely, and every outcome is delivered (ADR-0086)."""

from __future__ import annotations

import dataclasses
import datetime
import json
import unittest
from pathlib import Path

import routine_fixture

from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine as http_routine
from routine import record

UTC = datetime.UTC
KEY = "e" * 64
DIGEST = "sha256:" + "c" * 64
DAILY = {"kind": "daily", "time": "09:00"}
HOURLY = {"kind": "hourly", "every": 1}
WEEKLY = {"kind": "weekly", "weekday": 0, "time": "09:00"}
BATCH = ("net_1:routine:" + "f" * 32, "d" * 64)


def epoch(*parts: int) -> int:
    return int(datetime.datetime(*parts, tzinfo=UTC).timestamp())


NINE = epoch(2026, 10, 1, 9)
ANCHOR = epoch(2026, 9, 1)


def routine(routine_id: str = "a" * 32, schedule: dict | None = None, *, anchor: int = ANCHOR) -> record.Routine:
    value = routine_fixture.granted(
        record.Routine(
            routine_id=routine_id,
            name="Daily DNS summary",
            quote="Every day at 9, summarize the DNS changes.",
            plan=routine_fixture.plan_document(),
            schedule=dict(schedule or DAILY),
            timezone="UTC",
            assistants=(("dns", DIGEST),),
            anchor=anchor,
            next_run_at=0,
        )
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


DEFINED = {
    "name": "Daily DNS summary",
    "steps": [{"id": "check", "assistant": "dns", "action": "check", "inputs": [], "stored_inputs": []}],
    "schedule": {"kind": "daily", "time": "09:00"},
    "timezone": "UTC",
}


def full_notices(count: int = record.MAX_UNDELIVERED_NOTICES) -> tuple[record.Notice, ...]:
    return tuple(
        record.Notice(f"{index:032x}", "c" * 32, "", "done", NINE, routine_fixture.DONE) for index in range(count)
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
            "done": {"actions": [["dns", "list-zones"], ["dns", "replace-dns-record"]]},
            "recovered": {"actions": [["dns", "replace-dns-record"]]},
            "held": {"assistant_id": "dns", "action": "replace-dns-record"},
            "paused": {"assistant_id": None, "action": None, "reason": "exhausted"},
            "user-skipped": {"assistant_id": "dns", "action": "replace-dns-record"},
            "skipped": {"missed": 3},
            "scope-changed": {"assistants": ["dns"]},
            "failed": {"code": "assistant-rpc-failed", "actions": [["dns", "list-zones"]]},
            "denied": {"actions": []},
            "stopped": {"actions": [["dns", "list-zones"]]},
            "frozen": {"request_kind": "human", "assistant_id": "dns", "action": "replace-dns-record"},
            "created": DEFINED,
            "changed": DEFINED,
        }
        self.assertEqual(set(valid), http_routine.OUTCOMES)
        for outcome, detail in valid.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(http_routine.canonical_notice_detail(outcome, detail), detail)
        invalid = (
            ("done", {"actions": []}),
            ("done", {"actions": [["dns", "check"]] * 9}),
            ("done", {"actions": [["dns", "check"]], "result": {"ip": "1.2.3.4"}}),
            ("done", {"reply": "Done."}),
            ("recovered", {"actions": [["dns"]]}),
            ("held", {"assistant_id": "dns", "action": None}),
            ("held", {"assistant_id": "Bad", "action": "x"}),
            ("paused", {"assistant_id": "dns", "action": "x", "reason": "tired"}),
            ("paused", {"assistant_id": "dns", "action": "x"}),
            ("user-skipped", {"assistant_id": "dns", "action": "x", "input": {}}),
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
            ("created", {**DEFINED, "name": ""}),
            ("created", {**DEFINED, "steps": []}),
            ("created", {**DEFINED, "steps": DEFINED["steps"] * 9}),
            ("created", {**DEFINED, "steps": [{**DEFINED["steps"][0], "stored_inputs": ["Bad"]}]}),
            ("created", {key: value for key, value in DEFINED.items() if key != "steps"}),
            ("changed", {**DEFINED, "schedule": {"kind": "daily"}}),
            ("changed", {**DEFINED, "timezone": "../etc"}),
            ("changed", {**DEFINED, "input": {"zone": "example.com"}}),
        )
        for outcome, detail in invalid:
            with self.subTest(outcome=outcome, detail=detail):
                self.assertIsNone(http_routine.canonical_notice_detail(outcome, detail))


class NameContractTests(unittest.TestCase):
    def test_a_routine_name_is_one_short_canonical_line(self):
        self.assertEqual(http_routine.canonical_name("Resumo diário de DNS"), "Resumo diário de DNS")
        for value in (None, 7, "", " padded ", "x" * 81, "two\nlines", "Cafe\u0301"):
            with self.subTest(value=value):
                self.assertIsNone(http_routine.canonical_name(value))


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
            dataclasses.replace(good, name=""),
            dataclasses.replace(good, plan={}),
            dataclasses.replace(good, plan=routine_fixture.plan_document(timezone="Europe/Lisbon")),
            dataclasses.replace(good, plan=routine_fixture.plan_document("web")),
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
            lambda value: record.finish(state, run_id, value, NINE + 1, "done", routine_fixture.DONE),
            lambda value: record.bind_generation(state, run_id, value, NINE + 1, "net_2"),
        )
        for attempt in attempts:
            with self.subTest(attempt=attempt), self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
                attempt(forged)
        with self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
            record.finish(state, run_id, lease, expired, "done", routine_fixture.DONE)

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

    def test_team_ends_runs_without_their_lease_by_state(self):
        state, claim, _lease = bound()
        run_id = claim.run.run_id
        self.assertEqual(record.end(state, run_id, NINE, "stopped", {"actions": []}).runs, ())
        for outcome in ("done", "denied", "uncertain"):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
                record.end(state, run_id, NINE, outcome, {"actions": [["dns", "x"]]})
        state, claim, lease = bound()
        frozen = record.freeze(state, claim.run.run_id, lease, NINE, "human", "dns", "replace-dns-record")
        denied = record.end(frozen, claim.run.run_id, NINE, "denied", {"actions": []})
        self.assertEqual(denied.notices[0].outcome, "denied")
        with self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
            record.end(frozen, claim.run.run_id, NINE, "done", routine_fixture.DONE)

    def test_worker_outcomes_are_closed(self):
        state, claim, lease = claimed()
        done = record.finish(state, claim.run.run_id, lease, NINE + 9, "done", routine_fixture.DONE)
        self.assertEqual((done.runs, done.notices[0].outcome), ((), "done"))
        for outcome in ("skipped", "scope-changed", "uncertain", "unknown"):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
                record.finish(state, claim.run.run_id, lease, NINE, outcome, {"missed": 1})
        with self.assertRaisesRegex(record.RoutineStateError, "notice-invalid"):
            record.finish(state, claim.run.run_id, lease, NINE, "done", {"actions": [["dns", "check"]], "result": {}})
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
        state = record.finish(state, claim.run.run_id, lease, NINE, "done", routine_fixture.DONE)
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


class FailureStreakTests(unittest.TestCase):
    def test_three_failures_in_a_row_pause_the_routine_and_a_success_resets_the_streak(self):
        state = added(routine())
        for index, outcome in enumerate(("failed", "failed", "done", "failed", "failed", "failed")):
            claimed_state, claim = record.claim(at(state, "a" * 32, NINE), NINE, KEY)
            state = (
                record.end(claimed_state, claim.run.run_id, NINE + index, outcome, {"code": "x", "actions": []})
                if (outcome == "failed")
                else record.finish(
                    claimed_state,
                    claim.run.run_id,
                    record.lease_of(claim.lease_token, KEY),
                    NINE,
                    "done",
                    routine_fixture.DONE,
                )
            )
            state = dataclasses.replace(state, discards=(), starts=0)
            current = record.routine(state, "a" * 32)
            with self.subTest(index=index):
                self.assertEqual(current.failures, (1, 2, 0, 1, 2, 3)[index])
                self.assertEqual(current.paused, index == 5)
        # A Stop or a denial is no execution failure.
        claimed_state, claim = record.claim(
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
        stopped = record.end(claimed_state, claim.run.run_id, NINE, "stopped", {"actions": []})
        self.assertEqual(record.routine(stopped, "a" * 32).failures, 2)


class IncidentNoticeTests(unittest.TestCase):
    """A held run's one notice goes on through its incident: held, then paused or user-skipped (ADR-0092)."""

    def held(self) -> tuple[record.TeamRoutines, str]:
        state, claim, lease = bound()
        run_id = claim.run.run_id
        return record.fence(state, run_id, lease, NINE), run_id

    def test_a_hold_names_its_step_and_its_notice_goes_on_through_the_incident(self):
        state, run_id = self.held()
        state = record.settle_hold(state, run_id, NINE + 1, 1, ("dns", "replace-dns-record"))
        held = record.incident(state, run_id)
        self.assertEqual((held.quote, held.assistant_id, held.action), (routine().quote, "dns", "replace-dns-record"))
        notice = state.notices[-1]
        self.assertEqual(
            (notice.notice_id, notice.outcome, notice.detail, notice.version),
            (run_id, "held", {"assistant_id": "dns", "action": "replace-dns-record"}, 1),
        )
        self.assertEqual(held.notice_version, 1)
        paused = record.pause_incident(state, run_id, NINE + 2, "decided")
        self.assertTrue(record.routine(paused, "a" * 32).paused)
        self.assertEqual(
            (paused.notices[-1].outcome, paused.notices[-1].detail, paused.notices[-1].version),
            ("paused", {"assistant_id": "dns", "action": "replace-dns-record", "reason": "decided"}, 2),
        )
        with self.assertRaisesRegex(record.RoutineStateError, "incident-not-unresolved"):
            record.pause_incident(state, run_id, NINE, "bored")
        # Pular is the person's skip of this run, never the Routine's missed-schedule skip.
        skipped = record.skip_incident(paused, run_id, NINE + 3)
        self.assertEqual(record.incident(skipped, run_id).status, "skipped")
        self.assertEqual(
            (skipped.notices[-1].outcome, skipped.notices[-1].run_id, skipped.notices[-1].version),
            ("user-skipped", run_id, 3),
        )
        with self.assertRaisesRegex(record.RoutineStateError, "incident-not-unresolved"):
            record.pause_incident(skipped, run_id, NINE, "person")
        # A deleted Routine's incident still says what it was, from its own quote.
        gone = dataclasses.replace(state, routines=())
        self.assertEqual(record.skip_incident(gone, run_id, NINE).notices[-1].quote, routine().quote)

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
        self.assertEqual(record.next_due(state, NINE), NINE + 40)
        # A Routine with a live run or an unresolved incident wakes nothing; its own end or resolution does.
        busy = dataclasses.replace(state, runs=(record.Run("f" * 32, ids[4], "leased", 0),))
        self.assertEqual(record.next_due(busy, NINE), NINE + 50)
        held = dataclasses.replace(busy, incidents=(record.Incident("e" * 32, ids[5], "g", 0),))
        self.assertIsNone(record.next_due(held, NINE))

    def test_a_hold_without_a_sealed_cursor_names_no_step(self):
        state, run_id = self.held()
        state = record.settle_hold(state, run_id, NINE + 1)
        self.assertEqual(state.notices[-1].detail, {"assistant_id": None, "action": None})

    def test_a_completed_continuation_is_recovered_and_resets_the_streak(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        self.assertEqual(record.completed(record.run(state, run_id)), "done")
        continued = record.run(state, run_id)
        continued = dataclasses.replace(continued, generation=record.generation_for("net_1", run_id, "s1"))
        self.assertEqual(record.completed(continued), "recovered")
        streak = dataclasses.replace(record.routine(state, "a" * 32), failures=2)
        state = dataclasses.replace(state, routines=(streak,))
        ended = record.finish(state, run_id, lease, NINE + 1, "recovered", routine_fixture.DONE)
        self.assertEqual((ended.notices[-1].outcome, record.routine(ended, "a" * 32).failures), ("recovered", 0))


class RecoveredRunTests(unittest.TestCase):
    """The watchdog's lease-less endings touch only the exact leased run it read."""

    def test_a_recovered_run_is_held_or_done_only_under_the_lease_the_watchdog_read(self):
        state, claim, _lease = bound()
        run_id, lease_sha256 = claim.run.run_id, claim.run.lease_sha256
        held = record.run(record.hold_recovered(state, run_id, lease_sha256), run_id)
        self.assertEqual((held.status, held.lease_sha256, held.lease_expires_at), ("held", "", 0))
        done = record.complete_recovered(state, run_id, lease_sha256, NINE + 5)
        self.assertEqual(done.runs, ())
        self.assertEqual((done.notices[-1].outcome, done.notices[-1].detail), ("done", {"actions": [["dns", "check"]]}))
        unbound, unclaimed, _lease = claimed()
        for transition, code in (
            (lambda: record.hold_recovered(state, run_id, "0" * 64), "run-changed"),
            (lambda: record.complete_recovered(state, run_id, "0" * 64, NINE), "run-changed"),
            (
                lambda: record.hold_recovered(unbound, unclaimed.run.run_id, unclaimed.run.lease_sha256),
                "generation-invalid",
            ),
            (
                lambda: record.hold_recovered(record.hold_recovered(state, run_id, lease_sha256), run_id, lease_sha256),
                "run-not-running",
            ),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(record.RoutineStateError, code):
                transition()


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
            "routine": http_routine.canonical_routine_view,
            "run": http_routine.canonical_run_view,
            "notice_batch": http_routine.canonical_notice_batch,
            "claim": http_routine.canonical_claim,
            "claim_request": http_routine.canonical_claim_request,
            "incident": http_routine.canonical_incident_view,
            "card": http_routine.canonical_card,
            "card_answer_request": http_routine.canonical_card_answer_request,
            "card_answer": http_routine.canonical_card_answer,
            "segment_request": http_routine.canonical_segment_request,
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
        self.assertIsNone(http_routine.canonical_notice_batch({"notices": "none", "more": False}))
        self.assertIsNone(http_routine.canonical_notice_batch({"notices": ["x"], "more": False}))
        # Two notices at their bound exceed a batch's encoded bound; one alone fits.
        largest = dict(
            views["notice_batch"]["valid"][0]["notices"][0],
            run_id=None,
            outcome="created",
            detail=routine_fixture.large_definition(),
        )
        second = dict(largest, notice_id="9" * 32)
        self.assertIsNotNone(http_routine.canonical_notice_batch({"notices": [largest], "more": True}))
        self.assertIsNone(http_routine.canonical_notice_batch({"notices": [largest, second], "more": False}))
        self.assertIsNone(http_routine.canonical_claim({"run": ["x"]}))


RECEIPT = "e" * 64


class CompiledChangeTests(unittest.TestCase):
    """A request creates or changes a Routine with its notice and receipt in one transition (ADR-0092)."""

    def test_a_defined_routine_first_fires_no_sooner_than_thirty_seconds_after_it_is_durable(self):
        now = epoch(2026, 9, 1, 8, 59, 45)
        value = record.scheduled(dataclasses.replace(routine(), anchor=0, next_run_at=0), now)
        self.assertEqual(value.anchor, now + record.INITIAL_DELAY_SECONDS)
        self.assertEqual(value.next_run_at, epoch(2026, 9, 2, 9))
        with self.assertRaisesRegex(record.RoutineStateError, "routine-invalid"):
            record.scheduled(dataclasses.replace(routine(), schedule={"kind": "yearly"}), now)

    def test_a_request_creates_once_with_its_notice_and_its_receipt(self):
        state, created = record.create(record.TeamRoutines(), routine(), NINE, RECEIPT, NINE + 900)
        self.assertTrue(created)
        self.assertEqual([item.routine_id for item in state.routines], ["a" * 32])
        self.assertEqual(state.receipts, ((RECEIPT, NINE + 900),))
        (notice,) = state.notices
        self.assertEqual((notice.outcome, notice.run_id, notice.detail), ("created", "", DEFINED))
        again, created = record.create(state, routine("b" * 32), NINE + 5, RECEIPT, NINE + 900)
        self.assertFalse(created)
        self.assertEqual(again, state)
        # Deleting the Routine keeps its receipt, so a resend of the same request never recreates it.
        deleted = record.complete_delete(record.begin_delete(state, "a" * 32)[0], "a" * 32)
        self.assertEqual(record.create(deleted, routine(), NINE + 9, RECEIPT, NINE + 900), (deleted, False))

    def test_receipts_expire_but_saturation_refuses_without_evicting_a_live_one(self):
        expired = dataclasses.replace(record.TeamRoutines(), receipts=(("f" * 64, NINE),))
        state, created = record.create(expired, routine(), NINE, RECEIPT, NINE + 900)
        self.assertTrue(created)
        self.assertEqual(state.receipts, ((RECEIPT, NINE + 900),))
        live = tuple((f"{index:064x}", NINE + 900) for index in range(record.MAX_RECEIPTS))
        full = dataclasses.replace(record.TeamRoutines(), receipts=live)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-receipts-full"):
            record.create(full, routine(), NINE, RECEIPT, NINE + 900)
        # A request whose identity expired this very second never acts, though no receipt of it is live any more.
        for moment in (NINE + 900, NINE + 901):
            with self.subTest(moment=moment), self.assertRaisesRegex(record.RoutineStateError, "request-expired"):
                record.create(record.TeamRoutines(), routine(), moment, RECEIPT, NINE + 900)
        self.assertTrue(record.create(record.TeamRoutines(), routine(), NINE + 899, RECEIPT, NINE + 900)[1])
        for receipt, expires_at in (("E" * 64, NINE + 900), (RECEIPT, float(NINE))):
            with self.subTest(receipt=receipt), self.assertRaisesRegex(record.RoutineStateError, "receipt-invalid"):
                record.create(record.TeamRoutines(), routine(), NINE, receipt, expires_at)

    def test_an_update_is_the_next_revision_of_exactly_the_revision_the_request_saw(self):
        state = dataclasses.replace(added(routine(), routine("b" * 32)))
        state = record.mark_scope_changed(state, "a" * 32, NINE, ["dns"])
        state = record.set_paused(state, "a" * 32, True)
        changed = record.scheduled(dataclasses.replace(routine(), name="DNS summary", schedule=WEEKLY), NINE)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-revision-changed"):
            record.update(state, changed, 2, NINE, RECEIPT, NINE + 900)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-rate-limit"):
            record.update(
                added(routine(), routine("b" * 32, {"kind": "hourly", "every": 2})),
                record.scheduled(dataclasses.replace(routine(), schedule=HOURLY), NINE),
                1,
                NINE,
                RECEIPT,
                NINE + 900,
            )
        after, updated = record.update(state, changed, 1, NINE, RECEIPT, NINE + 900)
        self.assertTrue(updated)
        current = record.routine(after, "a" * 32)
        self.assertEqual(
            (current.revision, current.name, current.paused, current.needs_reconfirm), (2, changed.name, True, False)
        )
        self.assertEqual(after.notices[-1].outcome, "changed")
        self.assertEqual(after.notices[-1].detail["schedule"], WEEKLY)
        self.assertEqual(record.update(after, changed, 2, NINE, RECEIPT, NINE + 900), (after, False))

    def test_an_update_never_lands_on_a_running_or_deleting_routine(self):
        claimed_state, _claim, _lease = claimed()
        with self.assertRaisesRegex(record.RoutineStateError, "routine-busy"):
            record.update(claimed_state, routine(), 1, NINE, RECEIPT, NINE + 900)
        deleting = record.begin_delete(added(routine()), "a" * 32)[0]
        with self.assertRaisesRegex(record.RoutineStateError, "routine-not-found"):
            record.update(deleting, routine(), 1, NINE, RECEIPT, NINE + 900)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-not-found"):
            record.update(record.TeamRoutines(), routine(), 1, NINE, RECEIPT, NINE + 900)
