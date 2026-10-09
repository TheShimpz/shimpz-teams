"""A Routine definition is admitted only in its closed contract, changed by revision, and viewed exactly (ADR-0086)."""

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
from protocol.http.v1 import routine_notice as http_routine_notice
from protocol.http.v1 import routine_run as http_routine_run
from routine import claim as routine_claim
from routine import definition as routine_definition
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
    return routine_fixture.routine(routine_id, name="Daily DNS summary", anchor=anchor, schedule=schedule, plan=plan)


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
    state, claim = routine_claim.claim(at(added(routine()), "a" * 32, NINE), now, KEY)
    return state, claim, record.lease_of(claim.lease_token, KEY)


def bound(now: int = NINE) -> tuple[record.TeamRoutines, record.Claim, record.Lease]:
    state, claim, lease = claimed(now)
    return routine_claim.bind_generation(state, claim.run.run_id, lease, now, "net_1"), claim, lease


DEFINED = {
    "name": "Daily DNS summary",
    "plan": routine_definition.summary(routine_fixture.plan_document(), 1),
    "output": {"mode": "show", "step": 1},
    "schedule": {"kind": "daily", "time": "09:00"},
    "timezone": "UTC",
    "timezone_source": "browser",
    "state": "active",
    "permitted": {"total": 1, "changes": 0},
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
        self.assertEqual(http_routine_notice.canonical_notice_detail("stopped", stopped), stopped)
        long_assistant = {"actions": [["d" * (http_payload.MAX_ASSISTANT_ID_CHARS + 1), "x"]]}
        self.assertIsNone(http_routine_notice.canonical_notice_detail("stopped", long_assistant))

    def test_the_challenge_open_locales_match_the_chat_locales(self):
        self.assertEqual(http_routine.LOCALES, http_payload.CHAT_LOCALES)

    def test_notice_details_are_closed_and_never_carry_action_data(self):
        valid = {
            "done": {"plan": SUMMARY, "output": None},
            "recovered": {"plan": SUMMARY, "output": None},
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
                self.assertEqual(http_routine_notice.canonical_notice_detail(outcome, detail), detail)
        invalid = (
            ("done", {"plan": SUMMARY}),
            # A decision and its decision call are retired (ADR-0101 amendment, 2026-10-07): none is admitted.
            ("done", {"plan": SUMMARY, "output": None, "decision": None}),
            ("recovered", {"plan": SUMMARY, "output": None, "decision": None}),
            ("done", {"plan": SUMMARY, "output": None, "decision": {"state": "decided", "code": None, "message": "x"}}),
            ("deleted", {"name": "x"}),
            (
                "rehearsed",
                {"plan": SUMMARY, "output": None, "rehearsed": 1, "untested": 0, "not_permitted": 0},
            ),
            (
                "frozen",
                {
                    "request_kind": "permission",
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
                    "assistant_id": "dns",
                    "action": "x",
                    "position": {"phase": "decision", "call": 1},
                    "steps": 1,
                },
            ),
            ("done", {"actions": [["dns", "check"]], "output": None}),
            ("done", {"plan": {**SUMMARY, "steps": 3}, "output": None}),
            ("done", {"plan": SUMMARY, "output": None, "result": {"ip": "1.2.3.4"}}),
            (
                "done",
                {
                    "plan": SUMMARY,
                    "output": {"step": 3, "state": "unchanged", "value": None, "truncated": False},
                },
            ),
            ("done", {"reply": "Done."}),
            ("recovered", {"plan": {**SUMMARY, "actions": [["dns"]]}, "output": None}),
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
            ("created", {**DEFINED, "model": None}),
            ("created", {**DEFINED, "allowance": 0}),
            ("created", {**DEFINED, "output": {"mode": "show", "step": 1, "when": None}}),
            ("created", {**DEFINED, "output": {"mode": "decide", "step": None}}),
            ("created", {**DEFINED, "plan": {**DEFINED["plan"], "actions": []}}),
            ("created", {**DEFINED, "output": {"mode": "show", "step": 2}}),
            ("created", {**DEFINED, "output": {"mode": "show", "step": "check"}}),
            ("created", {**{key: value for key, value in DEFINED.items() if key != "plan"}, "steps": []}),
            ("created", {key: value for key, value in DEFINED.items() if key != "plan"}),
            ("changed", {**DEFINED, "schedule": {"kind": "daily"}}),
            ("changed", {**DEFINED, "timezone": "../etc"}),
            ("changed", {**DEFINED, "input": {"zone": "example.com"}}),
        )
        for outcome, detail in invalid:
            with self.subTest(outcome=outcome, detail=detail):
                self.assertIsNone(http_routine_notice.canonical_notice_detail(outcome, detail))


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
            dataclasses.replace(good, permitted=tuple(good.permitted) * (http_routine.MAX_PERMITTED + 1)),
            # The permitted set is exactly the plan's Actions: no extra one, as a retired decision once allowed.
            dataclasses.replace(
                good, permitted=(*good.permitted, {**good.permitted[0], "action": "zzz-extra", "read_only": False})
            ),
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
            plan["output"] = {"mode": "show", "step": f"s{steps - 1}"}
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
        plan["output"] = {"mode": "none", "step": None}
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
            "output": http_routine.canonical_output,
            "routine": http_routine_notice.canonical_routine_view,
            "run": http_routine_notice.canonical_run_view,
            "notice_batch": http_routine_notice.canonical_notice_batch,
            "claim": http_routine_run.canonical_claim,
            "claim_request": http_routine_run.canonical_claim_request,
            "incident": http_routine_notice.canonical_incident_view,
            "card": http_routine_run.canonical_card,
            "card_answer_request": http_routine_run.canonical_card_answer_request,
            "card_answer": http_routine_run.canonical_card_answer,
            "segment_request": http_routine_run.canonical_segment_request,
            "page": http_routine.canonical_page,
            "summary": http_routine.canonical_summary,
            "run_steps": http_routine_run.canonical_run_steps,
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
        self.assertIsNone(http_routine_notice.canonical_notice_batch({"notices": "none", "more": False}))
        self.assertIsNone(http_routine_notice.canonical_notice_batch({"notices": ["x"], "more": False}))
        # The largest notice, a completed run's shown output beside its summary, fits a batch several times over, and
        # a batch past its encoded bound is refused.
        first = views["notice_batch"]["valid"][0]["notices"][0]
        largest = [
            dict(first, notice_id=f"{index:032x}", run_id=f"{index:032x}", detail=routine_fixture.large_completion())
            for index in range(1, 33)
        ]
        size = http_routine.encoded_bytes(largest[0])
        self.assertGreater(size, http_routine.MAX_OUTPUT_BYTES // 2)
        fits = http_routine_notice.MAX_NOTICE_BATCH_BYTES // (size + 1)
        self.assertGreaterEqual(fits, 4)
        self.assertIsNotNone(http_routine_notice.canonical_notice_batch({"notices": largest[:fits], "more": True}))
        self.assertIsNone(http_routine_notice.canonical_notice_batch({"notices": largest[: fits + 1], "more": False}))
        self.assertIsNone(http_routine_run.canonical_claim({"run": ["x"]}))


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


class DefinitionZoneTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
