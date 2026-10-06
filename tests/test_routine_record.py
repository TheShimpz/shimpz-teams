"""Routine runs are claimed fairly and once, freeze and hold safely, and every outcome is delivered (ADR-0086)."""

from __future__ import annotations

import copy
import dataclasses
import datetime
import json
import unittest
from pathlib import Path
from unittest import mock

import routine_fixture

from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine as http_routine
from routine import definition as routine_definition
from routine import hold as routine_hold
from routine import plan as routine_plan
from routine import record

UTC = datetime.UTC
KEY = "e" * 64
DIGEST = "sha256:" + "c" * 64
DAILY = {"kind": "daily", "time": "09:00"}
HOURLY = {"kind": "hourly", "every": 1}
WEEKLY = {"kind": "weekly", "weekday": 0, "time": "09:00"}
CONTINUOUS = {"kind": "continuous", "gap": 5, "cap": 17_280}
BATCH = ("net_1:routine:" + "f" * 32, "d" * 64)
STEP = {"phase": "replay", "step": 1}


def epoch(*parts: int) -> int:
    return int(datetime.datetime(*parts, tzinfo=UTC).timestamp())


NINE = epoch(2026, 10, 1, 9)
ANCHOR = epoch(2026, 9, 1)


def routine(
    routine_id: str = "a" * 32, schedule: dict | None = None, *, anchor: int = ANCHOR, plan: dict | None = None
) -> record.Routine:
    value = routine_fixture.confirmed(
        record.Routine(
            routine_id=routine_id,
            name="Daily DNS summary",
            plan=plan or routine_fixture.plan_document(),
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
    "plan": routine_definition.summary(routine_fixture.plan_document(), 1),
    "output": {"mode": "show", "step": 1, "when": None},
    "schedule": {"kind": "daily", "time": "09:00"},
    "timezone": "UTC",
    "timezone_source": "browser",
    "state": "active",
    "permitted": {"total": 1, "changes": 0},
    "model": None,
    "allowance": 0,
}
# The compact summary of a run that carried out a list of zones, then replaced one DNS record.
SUMMARY = {
    "revision": 1,
    "plan_digest": "sha256:" + "d" * 64,
    "steps": 2,
    "actions": [["dns", "list-zones", 1], ["dns", "replace-dns-record", 1]],
    "more": 0,
}


def full_notices(count: int = record.MAX_UNDELIVERED_NOTICES) -> tuple[record.Notice, ...]:
    return tuple(
        record.Notice(f"{index:032x}", "c" * 32, "", "done", NINE, routine_fixture.DONE) for index in range(count)
    )


class ContractTests(unittest.TestCase):
    def test_notice_identities_follow_the_protocol_identifier_grammar(self):
        stopped = {"actions": [["dns", "list.zones_v2"]]}
        self.assertEqual(http_routine.canonical_notice_detail("stopped", stopped), stopped)
        long_assistant = {"actions": [["d" * (http_payload.MAX_ASSISTANT_ID_CHARS + 1), "x"]]}
        self.assertIsNone(http_routine.canonical_notice_detail("stopped", long_assistant))

    def test_the_challenge_open_locales_match_the_chat_locales(self):
        self.assertEqual(http_routine.LOCALES, http_payload.CHAT_LOCALES)

    def test_notice_details_are_closed_and_never_carry_action_data(self):
        valid = {
            "done": {"plan": SUMMARY, "output": None, "decision": None},
            "recovered": {"plan": SUMMARY, "output": None, "decision": None},
            "held": {
                "assistant_id": "dns",
                "action": "replace-dns-record",
                "position": {"phase": "replay", "step": 2},
                "steps": 2,
            },
            "paused": {"assistant_id": None, "action": None, "position": None, "steps": None, "reason": "exhausted"},
            "user-skipped": {
                "assistant_id": "dns",
                "action": "replace-dns-record",
                "position": {"phase": "replay", "step": 2},
                "steps": 2,
                "choice": "run",
            },
            "skipped": {"missed": 3},
            "healthy": {"runs": http_routine.MAX_ROLLUP_RUNS},
            "scope-changed": {"assistants": ["dns"]},
            "failed": {
                "code": "assistant-rpc-failed",
                "actions": [["dns", "list-zones"]],
                "position": {"phase": "replay", "step": 2},
                "steps": 3,
            },
            "denied": {"actions": []},
            "stopped": {"actions": [["dns", "list-zones"]]},
            "frozen": {
                "request_kind": "human",
                "assistant_id": "dns",
                "action": "replace-dns-record",
                "position": {"phase": "replay", "step": 2},
                "steps": 2,
            },
            "created": DEFINED,
            "changed": DEFINED,
            "deleted": {},
        }
        self.assertEqual(set(valid), http_routine.OUTCOMES)
        for outcome, detail in valid.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(http_routine.canonical_notice_detail(outcome, detail), detail)
        invalid = (
            ("done", {"plan": SUMMARY}),
            ("done", {"plan": SUMMARY, "output": None}),
            ("done", {"plan": SUMMARY, "output": None, "decision": {"state": "decided", "code": "x", "message": None}}),
            ("deleted", {"name": "x"}),
            (
                "rehearsed",
                {"plan": SUMMARY, "output": None, "decision": None, "rehearsed": 1, "untested": 0, "not_permitted": 0},
            ),
            (
                "frozen",
                {
                    "request_kind": "permission",
                    "assistant_id": "dns",
                    "action": "x",
                    "position": {"phase": "decision", "call": 65},
                    "steps": 1,
                },
            ),
            ("done", {"actions": [["dns", "check"]], "output": None, "decision": None}),
            ("done", {"plan": {**SUMMARY, "steps": 3}, "output": None, "decision": None}),
            ("done", {"plan": SUMMARY, "output": None, "decision": None, "result": {"ip": "1.2.3.4"}}),
            (
                "done",
                {
                    "plan": SUMMARY,
                    "output": {"step": 3, "state": "unchanged", "value": None, "truncated": False},
                    "decision": None,
                },
            ),
            ("done", {"reply": "Done."}),
            ("recovered", {"plan": {**SUMMARY, "actions": [["dns"]]}, "output": None, "decision": None}),
            ("held", {"assistant_id": "dns", "action": None, "position": {"phase": "replay", "step": 1}, "steps": 1}),
            ("held", {"assistant_id": "Bad", "action": "x", "position": {"phase": "replay", "step": 1}, "steps": 1}),
            ("held", {"assistant_id": "dns", "action": "x"}),
            ("held", {"assistant_id": "dns", "action": "x", "position": None, "steps": None}),
            ("held", {"assistant_id": "dns", "action": "x", "position": {"phase": "replay", "step": 3}, "steps": 2}),
            ("held", {"assistant_id": "dns", "action": "x", "position": {"phase": "replay", "step": 0}, "steps": 2}),
            ("held", {"assistant_id": "dns", "action": "x", "position": {"phase": "replay", "step": 1}, "steps": 257}),
            ("held", {"assistant_id": None, "action": None, "position": {"phase": "replay", "step": 1}, "steps": 1}),
            (
                "paused",
                {
                    "assistant_id": "dns",
                    "action": "x",
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                    "reason": "tired",
                },
            ),
            ("paused", {"assistant_id": "dns", "action": "x", "position": {"phase": "replay", "step": 1}, "steps": 1}),
            (
                "user-skipped",
                {
                    "assistant_id": "dns",
                    "action": "x",
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                    "input": {},
                },
            ),
            (
                "user-skipped",
                {"assistant_id": "dns", "action": "x", "position": {"phase": "replay", "step": 1}, "steps": 1},
            ),
            (
                "user-skipped",
                {
                    "assistant_id": "dns",
                    "action": "x",
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                    "choice": "skip",
                },
            ),
            (
                "paused",
                {
                    "assistant_id": "dns",
                    "action": "x",
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                    "reason": "person",
                },
            ),
            ("skipped", {"missed": 0}),
            ("skipped", {"missed": True}),
            ("healthy", {"runs": 0}),
            ("healthy", {"runs": http_routine.MAX_ROLLUP_RUNS + 1}),
            ("healthy", {"runs": 1, "actions": [["dns", "list-zones"]]}),
            ("scope-changed", {"assistants": []}),
            ("scope-changed", {"assistants": ["Bad"]}),
            ("failed", {"code": "Bad Code", "actions": [], "position": None, "steps": None}),
            ("failed", {"code": "x", "actions": [["dns"]]}),
            ("stopped", {"actions": [["dns", {"input": 1}]]}),
            ("stopped", {"actions": "dns"}),
            ("interrupted", {"actions": []}),
            (
                "frozen",
                {
                    "request_kind": "email",
                    "assistant_id": "dns",
                    "action": "x",
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                },
            ),
            (
                "frozen",
                {
                    "request_kind": "human",
                    "assistant_id": "Bad",
                    "action": "x",
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                },
            ),
            (
                "frozen",
                {
                    "request_kind": "human",
                    "assistant_id": "dns",
                    "action": ["x"],
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                },
            ),
            (
                "frozen",
                {
                    "request_kind": "human",
                    "assistant_id": "dns",
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                },
            ),
            ("frozen", {"request_kind": "human", "assistant_id": "dns", "action": "x"}),
            (
                "frozen",
                {"request_kind": "human", "assistant_id": "dns", "action": "x", "position": None, "steps": None},
            ),
            (["done"], {"reply": "x"}),
            ("done", ["reply"]),
            ("created", {**DEFINED, "name": ""}),
            ("created", {**DEFINED, "plan": {**DEFINED["plan"], "steps": 0}}),
            ("created", {**DEFINED, "allowance": 1}),
            ("created", {**DEFINED, "output": {"mode": "decide", "step": None, "when": "always"}, "allowance": 1}),
            ("created", {**DEFINED, "plan": {**DEFINED["plan"], "actions": []}}),
            ("created", {**DEFINED, "output": {"mode": "show", "step": 2, "when": None}}),
            ("created", {**DEFINED, "output": {"mode": "show", "step": "check", "when": None}}),
            ("created", {**{key: value for key, value in DEFINED.items() if key != "plan"}, "steps": []}),
            ("created", {key: value for key, value in DEFINED.items() if key != "plan"}),
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
            dataclasses.replace(good, confirmation=None),
            dataclasses.replace(good, confirmation={**good.confirmation, "principal": "x"}),
            dataclasses.replace(good, permitted=()),
            dataclasses.replace(good, permitted=(*good.permitted, *good.permitted)),
            dataclasses.replace(good, permitted=({**good.permitted[0], "pin": "sha256:" + "0" * 64},)),
            dataclasses.replace(good, permitted=({**good.permitted[0], "stored_inputs": ["b", "a"]},)),
            dataclasses.replace(good, permitted=({**good.permitted[0], "read_only": 1},)),
            dataclasses.replace(good, permissions_revision=-1),
            dataclasses.replace(good, permissions_revision=2**31),
            dataclasses.replace(good, allowance=1),
            dataclasses.replace(good, model={"provider": "openai", "model": "m", "effort": "low"}),
            dataclasses.replace(good, prompt="sha256:" + "0" * 64),
            dataclasses.replace(good, baseline={"id": "0" * 32, "digest": "0" * 64}),
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

    def test_a_step_whose_projection_outgrows_its_protocol_bound_is_refused(self):
        """A plan within its byte bound whose one step's previews escape past that step's projection is never held."""
        members = [
            f"{chr(97 + index // 26)}{chr(97 + index % 26)}" + "é" * 120
            for index in range(http_routine.MAX_STEP_INPUTS)
        ]
        inputs = {member: {"kind": "literal", "value": "€" * 120} for member in members}
        step = {"id": "check", "assistant": "dns", "action": "check", "pin": routine_fixture.PIN, "input": inputs}
        plan = {**routine_fixture.plan_document(), "steps": [step]}
        self.assertLessEqual(len(routine_plan.canonical(plan)), routine_plan.MAX_PLAN_BYTES)
        large = routine_fixture.confirmed(dataclasses.replace(routine(), plan=plan))
        projected = routine_definition.step(plan, large.permitted, 1)
        self.assertGreater(http_routine.encoded_bytes(projected), http_routine.MAX_STEP_VIEW_BYTES)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-too-large"):
            record.add_routine(record.TeamRoutines(), large)

    def test_a_definition_over_its_own_budget_or_the_teams_is_refused(self):
        """Each Routine's definition fits its budget, and every Routine's together fit the Team's (scale)."""

        def sized(routine_id: str, steps: int, inputs: int) -> record.Routine:
            plan = routine_fixture.plan_document()
            plan["steps"] = [
                {
                    "id": f"s{index}",
                    "assistant": "dns",
                    "action": "check",
                    "pin": routine_fixture.PIN,
                    "input": {f"m{member}": {"kind": "literal", "value": member} for member in range(inputs)},
                }
                for index in range(steps)
            ]
            plan["output"] = {"mode": "show", "step": f"s{steps - 1}", "when": None}
            return routine_fixture.confirmed(dataclasses.replace(routine(routine_id), plan=plan))

        # 256 steps of one Action with their own inputs fit their definition's budget beside its standing scope.
        many = sized("a" * 32, 256, 2)
        self.assertLessEqual(routine_definition.definition_bytes(many), routine_plan.MAX_DEFINITION_BYTES)
        record.add_routine(record.TeamRoutines(), many)
        bound_bytes = routine_definition.definition_bytes(many) - 1
        with (
            mock.patch.object(routine_plan, "MAX_DEFINITION_BYTES", bound_bytes),
            self.assertRaisesRegex(record.RoutineStateError, "routine-too-large"),
        ):
            record.add_routine(record.TeamRoutines(), many)
        # Each fits its own budget, but together they outgrow the Team's.
        large = [sized(f"{index:032x}", 256, 22) for index in range(5)]
        self.assertTrue(all(len(routine_plan.canonical(item.plan)) <= routine_plan.MAX_PLAN_BYTES for item in large))
        state = added(*large[:4])
        with self.assertRaisesRegex(record.RoutineStateError, "routine-team-budget"):
            record.add_routine(state, large[4])

    def test_a_routine_whose_daily_steps_outgrow_the_team_budget_is_refused(self):
        """A cap of runs a day reserves every step of each run; paused Routines keep their share (scale)."""
        plan = routine_fixture.plan_document()
        plan["steps"] = [{**plan["steps"][0], "id": f"s{index}"} for index in range(100)]
        plan["output"] = {"mode": "none", "step": None, "when": None}
        hundred = routine_fixture.confirmed(
            dataclasses.replace(routine(), plan=plan, schedule={"kind": "continuous", "gap": 432, "cap": 200})
        )
        hundred = dataclasses.replace(hundred, next_run_at=record.next_after(hundred, ANCHOR))
        # 200 runs of 100 steps is exactly the Team's 20,000 daily steps.
        self.assertEqual(routine_definition.daily_steps(hundred), routine_plan.MAX_DAILY_STEPS)
        state = record.set_paused(record.add_routine(record.TeamRoutines(), hundred), "a" * 32, True)
        self.assertEqual(routine_definition.capacity(state.routines), 0)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-step-budget"):
            record.add_routine(state, routine("b" * 32))
        over = dataclasses.replace(hundred, schedule={"kind": "continuous", "gap": 430, "cap": 201})
        over = dataclasses.replace(over, next_run_at=record.next_after(over, ANCHOR))
        with self.assertRaisesRegex(record.RoutineStateError, "routine-step-budget"):
            record.add_routine(record.TeamRoutines(), over)

    def test_a_team_holds_at_most_eight_routines_whose_caps_fit_its_daily_budget(self):
        state = added(*(routine(f"{index:032x}") for index in range(record.MAX_ROUTINES)))
        with self.assertRaisesRegex(record.RoutineStateError, "routine-limit"):
            record.add_routine(state, routine("f" * 32))
        # A one-step Routine every five seconds takes 17,280 of the Team's 20,000 daily steps: one every 30 seconds,
        # 2,880 more, no longer fits beside it, while a daily one does.
        busy = added(routine("b" * 32, CONTINUOUS))
        record.add_routine(busy, routine())
        every_thirty = {"kind": "continuous", "gap": 30, "cap": 2880}
        with self.assertRaisesRegex(record.RoutineStateError, "routine-step-budget"):
            record.add_routine(busy, routine("c" * 32, every_thirty))
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
        self.assertEqual(routine_hold.rekeyed(state, "f" * 64), (value,))
        self.assertEqual(routine_hold.rekeyed(state, KEY), ())

    def test_the_oldest_due_routine_wins_and_the_rolling_team_ceiling_holds_to_the_second(self):
        eleven = epoch(2026, 10, 1, 23)
        state = at(at(added(routine("a" * 32), routine("b" * 32)), "a" * 32, eleven), "b" * 32, eleven - 60)
        self.assertEqual(record.claimable(state, eleven).routine_id, "b" * 32)
        # The Team's daily steps count every start in the last 24 hours, whatever Routine made it, even a deleted one.
        first = eleven - 86_400 + 30
        full = tuple(("c" * 32, first + index, 1) for index in range(record.routine_starts.MAX_STARTS))
        capped = dataclasses.replace(state, starts=full)
        self.assertIsNone(record.claimable(capped, eleven))
        # The window rolls to the second: the oldest start leaves it exactly 24 hours after it was made.
        self.assertIsNone(record.claimable(capped, first + 86_400 - 1))
        self.assertEqual(record.next_due(capped, eleven), first + 86_400)
        after, claim = record.claim(capped, first + 86_400, KEY)
        self.assertEqual((claim.run.routine_id, len(after.starts)), ("b" * 32, record.routine_starts.MAX_STARTS))
        self.assertEqual(after.starts[-1], ("b" * 32, first + 86_400, 1))

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
            lambda value: record.spend(state, run_id, value, NINE + 1, (1, 1 * 1000)),
            lambda value: record.freeze(state, run_id, value, NINE + 1, ("human", "dns", "check", STEP)),
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
        state = record.spend(state, run_id, lease, NINE, (30, 30 * 1000))
        state = record.freeze(state, run_id, lease, NINE, ("human", "dns", "check", STEP))
        frozen = record.run(state, run_id)
        left = routine_plan.active_seconds(1) - 30
        self.assertEqual((frozen.status, frozen.lease_sha256, frozen.active_seconds_left), ("frozen", "", left))
        self.assertIsNone(record.claimable(state, epoch(2026, 10, 9, 9)))
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-running"):
            record.freeze(state, run_id, lease, NINE, ("human", "dns", "check", STEP))
        state, token = record.thaw(state, run_id, NINE + 50, 0)
        human = record.lease_of(token, record.HUMAN_LEASE)
        record.require_lease(record.run(state, run_id), record.lease_of(token, record.HUMAN_LEASE), NINE + 51)
        self.assertEqual(routine_hold.rekeyed(state, "f" * 64), ())
        state = record.spend(state, run_id, human, NINE + 51, (10, 10 * 1000))
        self.assertEqual(record.run(state, run_id).active_seconds_left, left - 10)
        # The person's answer runs at once, so its lease covers the run's active time left and a margin.
        self.assertEqual(
            record.run(state, run_id).lease_expires_at, NINE + 50 + left + routine_plan.LEASE_MARGIN_SECONDS
        )
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-frozen"):
            record.thaw(state, run_id, NINE + 60, 0)

    def test_invalid_freezes_and_durations_are_refused(self):
        state, claim, lease = claimed()
        for kind, assistant, action in (("mail", "dns", "list"), ("human", "Dns", "list"), ("human", "dns", "Bad")):
            with (
                self.subTest(kind=kind, assistant=assistant, action=action),
                self.assertRaisesRegex(record.RoutineStateError, "freeze-invalid"),
            ):
                record.freeze(state, claim.run.run_id, lease, NINE, (kind, assistant, action, STEP))
        for position in (
            {"phase": "replay", "step": 2},
            {"phase": "replay", "step": 0},
            {"phase": "decision", "call": 65},
            {"phase": "other", "step": 1},
            1,
        ):
            with self.subTest(position=position), self.assertRaisesRegex(record.RoutineStateError, "freeze-invalid"):
                record.freeze(state, claim.run.run_id, lease, NINE, ("human", "dns", "check", position))
        with self.assertRaisesRegex(record.RoutineStateError, "freeze-invalid"):
            record.freeze(state, claim.run.run_id, lease, NINE, ("human", "dns", "notify", STEP))
        for elapsed in ((-1, 0), (1.5, 0), (True, 0), (1, -1), (1, 1.5)):
            with self.subTest(elapsed=elapsed), self.assertRaisesRegex(record.RoutineStateError, "invalid-duration"):
                record.spend(state, claim.run.run_id, lease, NINE, elapsed)

    def test_a_team_freezes_at_most_eight_runs(self):
        state, claim, lease = claimed()
        frozen = tuple(record.Run(f"{index:032x}", f"{index + 1:032x}", "frozen", NINE) for index in range(8))
        state = dataclasses.replace(state, runs=(*state.runs, *frozen))
        self.assertEqual(record.run(state, frozen[-1].run_id), frozen[-1])
        with self.assertRaisesRegex(record.RoutineStateError, "frozen-limit"):
            record.freeze(state, claim.run.run_id, lease, NINE, ("human", "dns", "check", STEP))

    def test_team_ends_runs_without_their_lease_by_state(self):
        state, claim, _lease = bound()
        run_id = claim.run.run_id
        self.assertEqual(record.end(state, run_id, NINE, "stopped", {"actions": []}).runs, ())
        for outcome in ("done", "denied", "uncertain"):
            with self.subTest(outcome=outcome), self.assertRaisesRegex(record.RoutineStateError, "invalid-outcome"):
                record.end(state, run_id, NINE, outcome, {"actions": [["dns", "x"]]})
        state, claim, lease = bound()
        frozen = record.freeze(state, claim.run.run_id, lease, NINE, ("human", "dns", "check", STEP))
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
            record.finish(
                state, claim.run.run_id, lease, NINE, "stopped", {"actions": [["dns", "check"]], "result": {}}
            )
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
        over = dataclasses.replace(
            state, notices=full_notices(record.MAX_UNDELIVERED_NOTICES + record.MAX_ROUTINE_NOTICES)
        )
        with self.assertRaisesRegex(record.RoutineStateError, "notices-full"):
            record.mark_scope_changed(over, "a" * 32, NINE, ["dns"])

    def test_leases_and_active_time_expire(self):
        state, claim, lease = claimed()
        self.assertEqual(routine_hold.expired(state, NINE + 10), ())
        spent = record.spend(
            state, claim.run.run_id, lease, NINE, (record.ACTIVE_SECONDS, record.ACTIVE_SECONDS * 1000)
        )
        self.assertEqual([item.run_id for item in routine_hold.expired(spent, NINE + 10)], [claim.run.run_id])
        with self.assertRaisesRegex(record.RoutineStateError, "lease-invalid"):
            record.require_lease(
                record.run(spent, claim.run.run_id), record.lease_of(claim.lease_token, KEY), NINE + 10
            )
        self.assertEqual(len(routine_hold.expired(state, NINE + record.LEASE_SECONDS)), 1)

    def test_deletion_keeps_runs_until_they_end_and_keeps_notices(self):
        state, claim, lease = claimed()
        state = record.finish(state, claim.run.run_id, lease, NINE, "stopped", {"actions": []})
        state, again = record.claim(at(state, "a" * 32, NINE + 86_400), NINE + 86_400, KEY)
        state, runs = record.begin_delete(state, "a" * 32)
        self.assertEqual([item.run_id for item in runs], [again.run.run_id])
        with self.assertRaisesRegex(record.RoutineStateError, "routine-busy"):
            record.complete_delete(state, "a" * 32, NINE)
        state = record.end(state, again.run.run_id, NINE + 86_400, "stopped", {"actions": []})
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
        self.assertEqual(record.generation_for("net_1", "a" * 32), "net_1:routine:" + "a" * 32)


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
                record.end(
                    claimed_state,
                    claim.run.run_id,
                    NINE + index,
                    outcome,
                    {"code": "x", "actions": [], "position": None, "steps": None},
                )
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
            state = dataclasses.replace(state, discards=(), starts=())
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
        return routine_hold.fence(state, run_id, lease, NINE), run_id

    def test_a_hold_names_its_step_and_its_notice_goes_on_through_the_incident(self):
        state, run_id = self.held()
        state = routine_hold.settle_hold(state, run_id, NINE + 1, 1, ("dns", "replace-dns-record", STEP, 1))
        held = routine_hold.incident(state, run_id)
        self.assertEqual((held.name, held.assistant_id, held.action), (routine().name, "dns", "replace-dns-record"))
        notice = state.notices[-1]
        self.assertEqual(
            (notice.notice_id, notice.outcome, notice.detail, notice.version),
            (
                run_id,
                "held",
                {
                    "assistant_id": "dns",
                    "action": "replace-dns-record",
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                },
                1,
            ),
        )
        self.assertEqual(held.notice_version, 1)
        paused = routine_hold.pause_incident(state, run_id, NINE + 2, "decided")
        self.assertTrue(record.routine(paused, "a" * 32).paused)
        self.assertEqual(
            (paused.notices[-1].outcome, paused.notices[-1].detail, paused.notices[-1].version),
            (
                "paused",
                {
                    "assistant_id": "dns",
                    "action": "replace-dns-record",
                    "position": {"phase": "replay", "step": 1},
                    "steps": 1,
                    "reason": "decided",
                },
                2,
            ),
        )
        with self.assertRaisesRegex(record.RoutineStateError, "incident-not-unresolved"):
            routine_hold.pause_incident(state, run_id, NINE, "bored")
        # A person setting this run aside is never the Routine's missed-schedule skip.
        skipped = routine_hold.skip_incident(paused, run_id, NINE + 3, choice="run")
        self.assertEqual(routine_hold.incident(skipped, run_id).status, "skipped")
        self.assertEqual(
            (skipped.notices[-1].outcome, skipped.notices[-1].run_id, skipped.notices[-1].version),
            ("user-skipped", run_id, 3),
        )
        with self.assertRaisesRegex(record.RoutineStateError, "incident-not-unresolved"):
            routine_hold.pause_incident(skipped, run_id, NINE, "person")
        # A deleted Routine's incident still says what it was, by the name it had.
        gone = dataclasses.replace(state, routines=())
        self.assertEqual(routine_hold.skip_incident(gone, run_id, NINE, choice="run").notices[-1].name, routine().name)

    def test_rodar_keeps_one_fresh_run_pending_through_any_delay_and_never_moves_the_cadence(self):
        state, run_id = self.held()
        state = routine_hold.settle_hold(state, run_id, NINE + 1, 1, ("dns", "replace-dns-record", STEP, 1))
        state = routine_hold.pause_incident(state, run_id, NINE + 2, "exhausted")
        cadence = record.routine(state, "a" * 32).next_run_at
        expected = routine_hold.Expected(1, routine_hold.incident(state, run_id).generation, 1)
        asked = routine_hold.run_incident(state, run_id, NINE + 10, expected)
        requested = record.routine(asked, "a" * 32)
        self.assertEqual(
            (requested.paused, requested.run_requested, requested.next_run_at), (False, NINE + 10, cadence)
        )
        notice = asked.notices[-1]
        self.assertEqual((notice.outcome, notice.detail["choice"]), ("user-skipped", "run"))
        self.assertEqual(record.next_due(asked, NINE + 5), NINE + 10)
        # A Team catching up its notices waits; the request outlasts the schedule's own grace and is never missed.
        late = cadence - 60
        self.assertGreater(late - (NINE + 10), record.grace_seconds(requested))
        claimed_state, claim = record.claim(asked, late, KEY)
        after = record.routine(claimed_state, "a" * 32)
        self.assertEqual((claim.run.scheduled_at, after.run_requested, after.next_run_at), (NINE + 10, 0, cadence))
        self.assertEqual(claimed_state.starts[-1], ("a" * 32, late, 1))
        # A firing due at the same time serves the request too: one run, never two.
        both, claim = record.claim(asked, cadence, KEY)
        self.assertEqual((claim.run.scheduled_at, record.routine(both, "a" * 32).run_requested), (cadence, 0))
        self.assertEqual(len(both.runs), 1)
        # The card's state is checked in the same write.
        with self.assertRaisesRegex(record.RoutineStateError, "incident-changed"):
            routine_hold.run_incident(state, run_id, NINE + 10, dataclasses.replace(expected, current=2))

    def test_deleting_a_routine_sets_a_run_held_afterwards_aside_as_it_is_indexed(self):
        state, run_id = self.held()
        state, _runs = record.begin_delete(state, "a" * 32)
        state = routine_hold.settle_hold(state, run_id, NINE + 1, 1, ("dns", "replace-dns-record", STEP, 1))
        self.assertEqual(routine_hold.incident(state, run_id).status, "skipped")
        self.assertEqual(
            state.notices[-1].detail,
            {
                "assistant_id": "dns",
                "action": "replace-dns-record",
                "position": {"phase": "replay", "step": 1},
                "steps": 1,
                "choice": "delete",
            },
        )

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
        busy = dataclasses.replace(state, runs=(record.Run("f" * 32, ids[4], "frozen", 0),))
        self.assertEqual(record.next_due(busy, NINE), NINE + 50)
        # A leased run holds the Team's one slot: no other Routine of the Team is claimed or hinted until it ends.
        leased = dataclasses.replace(state, runs=(record.Run("f" * 32, ids[4], "leased", 0),))
        self.assertEqual(record.claimable(busy, NINE).routine_id, ids[0])
        self.assertIsNone(record.claimable(leased, NINE))
        self.assertIsNone(record.next_due(leased, NINE))
        held = dataclasses.replace(busy, incidents=(record.Incident("e" * 32, ids[5], "g", 0),))
        self.assertIsNone(record.next_due(held, NINE))

    def test_a_hold_without_a_sealed_cursor_names_no_step(self):
        state, run_id = self.held()
        state = routine_hold.settle_hold(state, run_id, NINE + 1)
        self.assertEqual(
            state.notices[-1].detail, {"assistant_id": None, "action": None, "position": None, "steps": None}
        )

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
        held = record.run(routine_hold.hold_recovered(state, run_id, lease_sha256), run_id)
        self.assertEqual((held.status, held.lease_sha256, held.lease_expires_at), ("held", "", 0))
        done = record.complete_recovered(state, run_id, lease_sha256, NINE + 5)
        self.assertEqual(done.runs, ())
        unavailable = {"step": 1, "state": "unavailable", "value": None, "truncated": False}
        self.assertEqual(
            (done.notices[-1].outcome, done.notices[-1].detail),
            (
                "done",
                {
                    "plan": routine_definition.summary(routine_fixture.plan_document(), 1),
                    "output": unavailable,
                    "decision": None,
                },
            ),
        )
        unbound, unclaimed, _lease = claimed()
        for transition, code in (
            (lambda: routine_hold.hold_recovered(state, run_id, "0" * 64), "run-changed"),
            (lambda: record.complete_recovered(state, run_id, "0" * 64, NINE), "run-changed"),
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
            "output": http_routine.canonical_output,
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
            "page": http_routine.canonical_page,
            "summary": http_routine.canonical_summary,
            "run_steps": http_routine.canonical_run_steps,
        }
        self.assertEqual(set(admit), set(views))
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
        # The largest notice, a completed run's shown output beside its summary, fits a batch several times over, and
        # a batch past its encoded bound is refused.
        first = views["notice_batch"]["valid"][0]["notices"][0]
        largest = [
            dict(first, notice_id=f"{index:032x}", run_id=f"{index:032x}", detail=routine_fixture.large_completion())
            for index in range(1, 33)
        ]
        size = http_routine.encoded_bytes(largest[0])
        self.assertGreater(size, http_routine.MAX_OUTPUT_BYTES // 2)
        fits = http_routine.MAX_NOTICE_BATCH_BYTES // (size + 1)
        self.assertGreaterEqual(fits, 4)
        self.assertIsNotNone(http_routine.canonical_notice_batch({"notices": largest[:fits], "more": True}))
        self.assertIsNone(http_routine.canonical_notice_batch({"notices": largest[: fits + 1], "more": False}))
        self.assertIsNone(http_routine.canonical_claim({"run": ["x"]}))


class ConfirmedChangeTests(unittest.TestCase):
    """A confirmed card creates or changes a Routine with its notice in one transition (ADR-0101)."""

    def test_a_defined_routine_first_fires_no_sooner_than_thirty_seconds_after_it_is_durable(self):
        now = epoch(2026, 9, 1, 8, 59, 45)
        value = record.scheduled(dataclasses.replace(routine(), anchor=0, next_run_at=0), now)
        self.assertEqual(value.anchor, now + record.INITIAL_DELAY_SECONDS)
        self.assertEqual(value.next_run_at, epoch(2026, 9, 2, 9))
        with self.assertRaisesRegex(record.RoutineStateError, "routine-invalid"):
            record.scheduled(dataclasses.replace(routine(), schedule={"kind": "yearly"}), now)

    def test_a_confirmed_card_creates_once_with_its_notice_named_as_the_routine_is(self):
        state = record.create(record.TeamRoutines(), routine(), NINE)
        self.assertEqual([item.routine_id for item in state.routines], ["a" * 32])
        (notice,) = state.notices
        self.assertEqual((notice.outcome, notice.run_id, notice.detail), ("created", "", DEFINED))
        self.assertEqual((notice.name, notice.usage, notice.protection_lost), ("Daily DNS summary", None, False))
        # A minted id never exists twice, so a repeated confirmation never creates twice.
        with self.assertRaisesRegex(record.RoutineStateError, "routine-exists"):
            record.create(state, routine(), NINE + 5)

    def test_a_notice_keeps_the_timezone_source_it_was_written_with(self):
        unzoned = dataclasses.replace(routine(schedule=HOURLY), timezone_source="none")
        state = record.create(record.TeamRoutines(), unzoned, NINE)
        lisbon = {**unzoned.plan, "timezone": "Europe/Lisbon"}
        zoned = dataclasses.replace(unzoned, timezone="Europe/Lisbon", timezone_source="person", plan=lisbon)
        zoned = record.scheduled(zoned, NINE)
        state = record.update(state, zoned, 1, NINE + 1)
        state, _runs = record.begin_delete(state, "a" * 32)
        state = record.complete_delete(state, "a" * 32, NINE + 2)
        details = [(item.outcome, item.detail.get("timezone_source")) for item in state.notices]
        self.assertEqual(details, [("created", "none"), ("changed", "person"), ("deleted", None)])

    def test_room_for_a_change_counts_only_undelivered_notices(self):
        self.assertIsNone(record.change_room(record.TeamRoutines(), 1))
        full = dataclasses.replace(
            record.TeamRoutines(),
            notices=full_notices(record.MAX_UNDELIVERED_NOTICES + record.MAX_ROUTINE_NOTICES),
        )
        self.assertEqual(record.change_room(full, 1), "notices-full")

    def test_an_update_is_the_next_revision_of_exactly_the_revision_the_card_saw(self):
        state = dataclasses.replace(added(routine(), routine("b" * 32)))
        state = record.mark_scope_changed(state, "a" * 32, NINE, ["dns"])
        state = record.set_paused(state, "a" * 32, True)
        changed = record.scheduled(dataclasses.replace(routine(), name="DNS summary", schedule=WEEKLY), NINE)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-revision-changed"):
            record.update(state, changed, 2, NINE)
        every_thirty = {"kind": "continuous", "gap": 30, "cap": 2880}
        with self.assertRaisesRegex(record.RoutineStateError, "routine-step-budget"):
            record.update(
                added(routine(), routine("b" * 32, CONTINUOUS)),
                record.scheduled(dataclasses.replace(routine(), schedule=every_thirty), NINE),
                1,
                NINE,
            )
        after = record.update(state, changed, 1, NINE)
        current = record.routine(after, "a" * 32)
        self.assertEqual(
            (current.revision, current.name, current.paused, current.needs_reconfirm), (2, changed.name, True, False)
        )
        self.assertEqual(after.notices[-1].outcome, "changed")
        self.assertEqual(after.notices[-1].detail["schedule"], WEEKLY)
        self.assertEqual(after.notices[-1].detail["state"], "paused")
        self.assertEqual(after.notices[-1].name, "DNS summary")
        # An earlier version keeps the name it was written with.
        self.assertEqual(after.notices[-2].name, "Daily DNS summary")

    def test_an_update_never_lands_on_a_running_or_deleting_routine(self):
        claimed_state, _claim, _lease = claimed()
        with self.assertRaisesRegex(record.RoutineStateError, "routine-busy"):
            record.update(claimed_state, routine(), 1, NINE)
        deleting = record.begin_delete(added(routine()), "a" * 32)[0]
        with self.assertRaisesRegex(record.RoutineStateError, "routine-not-found"):
            record.update(deleting, routine(), 1, NINE)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-not-found"):
            record.update(record.TeamRoutines(), routine(), 1, NINE)

    def test_deletion_completes_with_one_deleted_notice_that_outlives_the_routine(self):
        state = record.begin_delete(added(routine()), "a" * 32)[0]
        gone = record.complete_delete(state, "a" * 32, NINE)
        self.assertEqual(gone.routines, ())
        (notice,) = [item for item in gone.notices if item.outcome == "deleted"]
        self.assertEqual(
            (notice.routine_id, notice.run_id, notice.name, notice.detail, notice.usage),
            ("a" * 32, "", "Daily DNS summary", {}, None),
        )
        with self.assertRaisesRegex(record.RoutineStateError, "routine-not-found"):
            record.complete_delete(gone, "a" * 32, NINE)


MODEL = {"provider": "openai", "model": "gpt-6-luna", "effort": "low"}
DECIDE = {"mode": "decide", "step": None, "when": "changes"}


class DecisionDefinitionTests(unittest.TestCase):
    """A decide definition's base prompt, model, allowance, and baseline (ADR-0101)."""

    def decided(self, **changes: object) -> record.Routine:
        plan = {**routine_fixture.plan_document(), "output": dict(DECIDE)}
        values = {"prompt": "sha256:" + "1" * 64, "model": dict(MODEL), "allowance": 16, **changes}
        value = dataclasses.replace(routine(plan=plan), **values)
        return dataclasses.replace(value, next_run_at=record.next_after(value, ANCHOR))

    def test_a_decision_holds_its_scope_and_nothing_else_does(self):
        baseline = {"id": "b" * 32, "digest": "c" * 64}
        admitted = record.routine(added(self.decided(baseline=baseline)), "a" * 32)
        self.assertEqual((admitted.allowance, admitted.model, admitted.baseline), (16, MODEL, baseline))
        for changes in (
            {"prompt": None},
            {"model": None},
            {"model": {**MODEL, "effort": "max"}},
            {"allowance": 0},
            {"allowance": 65},
            {"baseline": {"id": "b" * 32}},
            {"baseline": {"id": "B" * 32, "digest": "c" * 64}},
            {"permitted": list(routine().permitted)},
            {"permitted": tuple(routine().permitted) * (http_routine.MAX_PERMITTED + 1)},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(record.definition_valid(self.decided(**changes)))
        many = {**routine_fixture.plan_document(), "output": dict(DECIDE)}
        many["steps"] = [{**many["steps"][0], "id": f"s{index}"} for index in range(250)]
        self.assertFalse(record.definition_valid(self.decided(plan=many)))

    def test_a_definition_out_of_its_zone_or_revision_is_invalid(self):
        self.assertFalse(record.definition_valid(dataclasses.replace(routine(), timezone="Mars/Olympus")))
        # A Routine with no known zone runs on UTC by convention, its run date included; it is never another zone.
        unzoned = dataclasses.replace(routine(schedule=HOURLY), timezone_source="none")
        self.assertTrue(record.definition_valid(unzoned))
        self.assertTrue(record.definition_valid(dataclasses.replace(routine(), timezone_source="none")))
        self.assertFalse(record.definition_valid(dataclasses.replace(routine(), timezone_source="phone")))
        clocked = copy.deepcopy(unzoned.plan)
        clocked["steps"][0]["input"] = {"day": {"kind": "run_clock", "format": "date"}}
        self.assertTrue(record.definition_valid(dataclasses.replace(unzoned, plan=clocked)))
        lisbon = dataclasses.replace(
            unzoned, timezone="Europe/Lisbon", plan={**unzoned.plan, "timezone": "Europe/Lisbon"}
        )
        self.assertFalse(record.definition_valid(lisbon))
        self.assertFalse(record.definition_valid(dataclasses.replace(routine(), revision=0)))
        self.assertTrue(record.definition_valid(routine()))


class TransitionEdgeTests(unittest.TestCase):
    def test_stale_or_misplaced_transitions_are_refused(self):
        state, claim, lease = bound()
        run_id = claim.run.run_id
        with self.assertRaisesRegex(record.RoutineStateError, "run-changed"):
            record.end(state, run_id, NINE, "stopped", {"actions": []}, status="frozen")
        deleting, _runs = record.begin_delete(state, "a" * 32)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-deleting"):
            record.freeze(deleting, run_id, lease, NINE, ("human", "dns", "check", STEP))
        with self.assertRaisesRegex(record.RoutineStateError, "generation-invalid"):
            record.generation_for("net_1", run_id, "x9")
        queued = dataclasses.replace(state, discards=((run_id, "g1"), (run_id, "g2")))
        self.assertEqual(record.discarded(queued, run_id, "g1").discards, ((run_id, "g2"),))


class HoldEdgeTests(unittest.TestCase):
    def held(self) -> tuple[record.TeamRoutines, str]:
        state, claim, lease = bound()
        return routine_hold.fence(state, claim.run.run_id, lease, NINE), claim.run.run_id

    def test_a_hold_settles_only_a_held_run_of_a_known_revision_within_the_incident_bound(self):
        state, claim, _lease = bound()
        with self.assertRaisesRegex(record.RoutineStateError, "run-not-held"):
            routine_hold.settle_hold(state, claim.run.run_id, NINE + 1)
        unbound, unbound_claim, unbound_lease = claimed()
        with self.assertRaisesRegex(record.RoutineStateError, "generation-invalid"):
            routine_hold.fence(unbound, unbound_claim.run.run_id, unbound_lease, NINE)
        held, run_id = self.held()
        with self.assertRaisesRegex(record.RoutineStateError, "incident-invalid"):
            routine_hold.settle_hold(held, run_id, NINE + 1, 0)
        released = tuple(
            record.Incident(f"{index:032x}", "c" * 32, f"net_1:routine:{index:032x}", NINE, status="released")
            for index in range(record.MAX_INCIDENTS)
        )
        settled = routine_hold.settle_hold(dataclasses.replace(held, incidents=released), run_id, NINE + 1)
        self.assertEqual(len(settled.incidents), record.MAX_INCIDENTS)
        full = tuple(dataclasses.replace(item, status="skipped") for item in released)
        with self.assertRaisesRegex(record.RoutineStateError, "incident-limit"):
            routine_hold.settle_hold(dataclasses.replace(held, incidents=full), run_id, NINE + 1)

    def test_an_incident_reopens_only_once_unresolved_resumable_and_idle(self):
        held, run_id = self.held()
        state = routine_hold.settle_hold(held, run_id, NINE + 1, 1)
        generation = record.generation_for("net_1", run_id, "s1")
        with self.assertRaisesRegex(record.RoutineStateError, "incident-not-found"):
            routine_hold.incident(state, "0" * 32)
        paused = record.set_paused(state, "a" * 32, True)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-not-resumable"):
            routine_hold.reopen_incident(paused, run_id, NINE + 2, generation)
        with self.assertRaisesRegex(record.RoutineStateError, "routine-busy"):
            routine_hold.reopen_incident(state, run_id, NINE + 2, "net_1:routine:" + "0" * 32)
        skipped = routine_hold.skip_incident(state, run_id, NINE + 2, choice="run")
        for transition in (
            lambda: routine_hold.reopen_incident(skipped, run_id, NINE + 3, generation),
            lambda: routine_hold.skip_incident(skipped, run_id, NINE + 3, choice="run"),
            lambda: routine_hold.release_incident(state, run_id),
        ):
            with self.subTest(transition=transition), self.assertRaises(record.RoutineStateError):
                transition()
        released = routine_hold.release_incident(skipped, run_id)
        self.assertEqual(routine_hold.incident(released, run_id).status, "released")
        self.assertIs(routine_hold.release_incident(released, run_id), released)


if __name__ == "__main__":
    unittest.main()
