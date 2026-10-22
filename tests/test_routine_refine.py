"""A Routine's next revision from its plan and the agent's explicit edits, and its confirmed write (ADR-0122)."""

import dataclasses
import datetime
import unittest
from unittest import mock

import routine_fixture
from test_routine_recording import CONTRACTS, PIN, SHIMPZ_ID, ZONES_CALL, _send

from routine import plan as routine_plan
from routine import record, trace
from routine import recording as routine_recording
from routine import refine as routine_refine

ZONE = "America/Sao_Paulo"
SELECTED = {"kind": "step_output", "step": "s1", "pointer": "/result", "where": {"name": "shimpz.com"}, "item": "/id"}
BASE = {
    "version": routine_plan.VERSION,
    "timezone": ZONE,
    "steps": [
        {"id": "s1", "assistant": "cloudflare", "action": "list-zones", "pin": PIN, "input": {}},
        {
            "id": "s2",
            "assistant": "cloudflare",
            "action": "list-dns-records",
            "pin": PIN,
            "input": {"zone_id": SELECTED},
        },
        {
            "id": "s3",
            "assistant": "reports",
            "action": "fetch",
            "pin": PIN,
            "input": {"q": {"kind": "step_output", "step": "s2", "pointer": "/result"}},
        },
    ],
    "output": {"mode": "show", "step": "s3"},
}
FETCH_A = ("reports/fetch", {"q": "dns", "page": 2}, {"result": []})
FETCH_B = ("reports/fetch", {"q": "zones", "page": 3}, {"result": []})


def _refined(*edits: dict[str, object], calls=(FETCH_A, FETCH_B), message: str = "Também busca relatórios", base=BASE):
    sends = (_send(*calls, message=message),)
    span = routine_recording.span_calls(sends, CONTRACTS)
    texts = [line for send in sends for line in routine_recording.segment_lines(send.person)]
    known = routine_recording.known_texts(texts, trace.Protection())
    context = routine_recording.Context(sends, span, known, (ZONE, "browser"), None, CONTRACTS)
    return routine_refine.refine(base, list(edits), context, span)


def _code(test: unittest.TestCase, *edits: dict[str, object], **options) -> str:
    with test.assertRaises(routine_recording.RecordingError) as raised:
        _refined(*edits, **options)
    return raised.exception.code


def _insert(step: str, *calls: str) -> dict[str, object]:
    return {"kind": "insert", "step": step, "calls": list(calls)}


def _replace(step: str, call: str) -> dict[str, object]:
    return {"kind": "replace", "step": step, "calls": [call]}


def _remove(step: str) -> dict[str, object]:
    return {"kind": "remove", "step": step, "calls": []}


class EditTests(unittest.TestCase):
    def test_an_insertion_keeps_its_traced_order_and_every_reference_follows_its_renumbered_step(self) -> None:
        refined = _refined(_insert("s1", "c1", "c2"))
        steps = refined.document["steps"]
        self.assertEqual([step["id"] for step in steps], ["s1", "s2", "s3", "s4", "s5"])
        self.assertEqual(
            [step["action"] for step in steps], ["list-zones", "fetch", "fetch", "list-dns-records", "fetch"]
        )
        self.assertEqual(
            steps[1]["input"], {"page": {"kind": "literal", "value": 2}, "q": {"kind": "literal", "value": "dns"}}
        )
        self.assertEqual(steps[2]["input"]["q"], {"kind": "literal", "value": "zones"})
        self.assertEqual(steps[3]["input"]["zone_id"], SELECTED)
        self.assertEqual(steps[4]["input"]["q"]["step"], "s4")
        self.assertEqual(refined.document["output"], {"mode": "show", "step": "s5"})
        self.assertEqual(refined.changes, (("kept", 1), ("added", None), ("added", None), ("kept", 2), ("kept", 3)))
        self.assertEqual((refined.removed, refined.last_changed), ((), False))
        self.assertEqual(refined.origins["s2"], {"page": "assistant", "q": "assistant"})
        self.assertEqual(refined.origins["s4"], {"zone_id": "selector"})

    def test_inserted_calls_keep_their_traced_order_whatever_order_the_edit_names(self) -> None:
        refined = _refined(_insert("s3", "c2", "c1"))
        queries = [step["input"]["q"]["value"] for step in refined.document["steps"][3:]]
        self.assertEqual(queries, ["dns", "zones"])

    def test_edits_apply_in_plan_order_whatever_order_they_are_given_in(self) -> None:
        later = _refined(_insert("s3", "c2"), _insert("s1", "c1"))
        self.assertEqual(
            [(step["action"], step["input"].get("q")) for step in later.document["steps"]][1:2],
            [("fetch", {"kind": "literal", "value": "dns"})],
        )
        self.assertEqual(later.changes[-1], ("added", None))
        self.assertTrue(later.last_changed)
        self.assertEqual(later.document["output"]["step"], "s5")

    def test_replacing_or_removing_the_last_step_says_the_shown_result_changes(self) -> None:
        replaced = _refined(_replace("s3", "c1"))
        self.assertEqual(replaced.changes, (("kept", 1), ("kept", 2), ("replaced", 3)))
        self.assertEqual((replaced.removed, replaced.last_changed), (((3, "replaced"),), True))
        removed = _refined(_remove("s3"))
        self.assertEqual(removed.changes, (("kept", 1), ("kept", 2)))
        self.assertEqual((removed.removed, removed.last_changed), (((3, "removed"),), True))
        self.assertEqual(removed.document["output"], {"mode": "show", "step": "s2"})

    def test_a_plan_that_shows_nothing_keeps_showing_nothing(self) -> None:
        quiet = {**BASE, "output": {"mode": "none", "step": None}}
        self.assertEqual(_refined(_insert("s3", "c1"), base=quiet).document["output"], {"mode": "none", "step": None})

    def test_a_description_alone_keeps_every_step(self) -> None:
        refined = _refined()
        self.assertEqual(refined.document, BASE)
        self.assertEqual(refined.changes, (("kept", 1), ("kept", 2), ("kept", 3)))
        self.assertFalse(refined.last_changed)

    def test_a_whole_dependency_group_may_go_together(self) -> None:
        refined = _refined(_remove("s2"), _remove("s3"))
        self.assertEqual([step["id"] for step in refined.document["steps"]], ["s1"])
        self.assertEqual(refined.removed, ((2, "removed"), (3, "removed")))

    def test_removing_or_replacing_a_step_a_kept_step_reads_is_refused(self) -> None:
        for edit in (_remove("s2"), _replace("s2", "c1"), _replace("s1", "c1"), _remove("s1")):
            with self.subTest(edit=edit):
                self.assertEqual(_code(self, edit), routine_refine.DEPENDENCY)

    def test_an_unknown_step_or_call_or_an_empty_plan_is_refused(self) -> None:
        self.assertEqual(_code(self, _insert("s9", "c1")), routine_refine.INVALID)
        self.assertEqual(_code(self, _insert("s1", "c3")), routine_refine.INVALID)
        self.assertEqual(_code(self, _remove("s1"), _remove("s2"), _remove("s3")), routine_refine.INVALID)


class InputTests(unittest.TestCase):
    def test_a_value_an_earlier_result_holds_or_no_result_sourced_is_refused_in_this_slice(self) -> None:
        records = ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {"result": []})
        self.assertEqual(_code(self, _insert("s3", "c2"), calls=(ZONES_CALL, records)), routine_refine.INPUT)
        self.assertEqual(_code(self, _insert("s3", "c1"), calls=(records,)), routine_refine.INPUT)
        named = "Também leia os registros de shimpz.com"
        self.assertEqual(
            _code(self, _insert("s3", "c2"), calls=(ZONES_CALL, records), message=named), routine_refine.INPUT
        )

    def test_a_value_the_person_named_is_a_literal(self) -> None:
        records = ("cloudflare/list-dns-records", {"zone_id": SHIMPZ_ID}, {"result": []})
        refined = _refined(_insert("s3", "c1"), calls=(records,), message=f"Inclua a zona {SHIMPZ_ID}")
        self.assertEqual(refined.document["steps"][3]["input"]["zone_id"], {"kind": "literal", "value": SHIMPZ_ID})
        self.assertEqual(refined.origins["s4"], {"zone_id": "request"})

    def test_the_send_date_in_the_routines_zone_is_each_runs_date(self) -> None:
        start = _send().started_at
        today = datetime.datetime.fromtimestamp(start, datetime.UTC).astimezone(
            datetime.timezone(datetime.timedelta(hours=-3))
        )
        fetch = ("reports/fetch", {"t": today.date().isoformat()}, {"result": []})
        refined = _refined(_insert("s3", "c1"), calls=(fetch,))
        self.assertEqual(refined.document["steps"][3]["input"]["t"], {"kind": "run_clock", "format": "date"})
        self.assertEqual(refined.origins["s4"], {"t": "clock"})

    def test_a_withheld_input_refuses_as_a_secret(self) -> None:
        withheld = (*FETCH_A[:3], trace.Kept({"q": None}, frozenset({"/q"})))
        self.assertEqual(_code(self, _insert("s3", "c1"), calls=(withheld,)), "routine-secret-literal")

    def test_every_pin_is_revalidated_and_each_new_step_resolves_to_what_its_call_sent(self) -> None:
        drifted = {**BASE, "steps": [{**BASE["steps"][0], "pin": "sha256:" + "f" * 64}, *BASE["steps"][1:]]}
        self.assertEqual(_code(self, base=drifted), "plan-pin-drift")
        with mock.patch.object(routine_plan, "resolve", return_value={"q": "other"}):
            self.assertEqual(_code(self, _insert("s3", "c1")), "routine-recording-unverified")
        too_large = routine_plan.PlanError("plan-input-too-large")
        with mock.patch.object(routine_plan, "resolve", side_effect=too_large):
            self.assertEqual(_code(self, _insert("s3", "c1")), "routine-recording-too-large")
        with mock.patch.object(routine_plan, "resolve", side_effect=routine_plan.PlanError("plan-input-type")):
            self.assertEqual(_code(self, _insert("s3", "c1")), "routine-recording-unverified")

    def test_a_call_of_an_earlier_send_is_never_a_handle_of_this_one(self) -> None:
        earlier = _send(FETCH_A, message="Busca relatórios")
        current = _send(FETCH_B, message="Também busca zonas")
        sends = (earlier, current)
        span = routine_recording.span_calls(sends, CONTRACTS)
        known = routine_recording.known_texts([], trace.Protection())
        context = routine_recording.Context(sends, span, known, (ZONE, "browser"), None, CONTRACTS)
        mine = [call for call in span if call.send == 1]
        refined = routine_refine.refine(BASE, [_insert("s3", "c1")], context, mine)
        self.assertEqual(refined.document["steps"][3]["input"]["q"], {"kind": "literal", "value": "zones"})
        with self.assertRaises(routine_recording.RecordingError):
            routine_refine.refine(BASE, [_insert("s3", "c2")], context, mine)


def _routine(schedule: dict[str, object] | None = None) -> record.Routine:
    return routine_fixture.routine(schedule=schedule, plan=routine_fixture.plan_document())


def _refinement(current: record.Routine) -> record.Routine:
    plan = {**current.plan, "steps": [*current.plan["steps"], {**current.plan["steps"][0], "id": "s2"}]}
    plan["output"] = {"mode": "show", "step": "s2"}
    return dataclasses.replace(current, description="Checks the DNS zone twice.", plan=plan, revision=1)


class CommitTests(unittest.TestCase):
    def test_a_refinement_keeps_the_scheduling_state_its_write_finds(self) -> None:
        state = routine_fixture.added(_routine())
        card = _refinement(state.routines[0])
        # Meanwhile a run ended, the Routine was paused, missed a firing, and a run was asked for.
        moved = dataclasses.replace(
            state.routines[0],
            next_run_at=state.routines[0].next_run_at + 86_400,
            paused=True,
            gap_started_at=5,
            missed=2,
            reported_missed=1,
            failures=2,
            run_requested=7,
            rollup_minute=60,
            rollup_runs=3,
            output_digest="d" * 64,
        )
        state = record.replace_routine(state, moved)
        refined = record.refine(state, card, 1, 100)
        (value,) = refined.routines
        self.assertEqual((value.revision, value.description), (2, "Checks the DNS zone twice."))
        self.assertEqual(value.plan, card.plan)
        kept = ("anchor", "next_run_at", "paused", "gap_started_at", "missed", "reported_missed", "run_requested")
        self.assertEqual({name: getattr(value, name) for name in kept}, {name: getattr(moved, name) for name in kept})
        self.assertEqual((value.rollup_minute, value.rollup_runs), (60, 3))
        self.assertEqual((value.failures, value.output_digest, value.needs_reconfirm), (0, "", False))
        (notice,) = refined.notices
        self.assertEqual((notice.outcome, notice.detail), ("changed", {}))

    def test_a_stale_busy_deleting_rezoned_or_oversized_refinement_is_refused(self) -> None:
        state = routine_fixture.added(_routine())
        card = _refinement(state.routines[0])
        cases = {
            "routine-revision-changed": (state, card, 2),
            "routine-not-found": (
                record.replace_routine(state, dataclasses.replace(state.routines[0], deleting=True)),
                card,
                1,
            ),
            "routine-invalid": (
                state,
                dataclasses.replace(card, plan={**card.plan, "timezone": "Asia/Tokyo"}),
                1,
            ),
        }
        busy = dataclasses.replace(state, runs=(record.Run("f" * 32, card.routine_id, "frozen"),))
        cases["routine-busy"] = (busy, card, 1)
        often = routine_fixture.added(_routine(schedule={"kind": "continuous", "gap": 5}))
        cases["routine-step-budget"] = (often, _refinement(often.routines[0]), 1)
        for code, (given, value, revision) in cases.items():
            with self.subTest(code=code), self.assertRaises(record.RoutineStateError) as raised:
                record.refine(given, value, revision, 100)
            self.assertEqual(str(raised.exception), code)


if __name__ == "__main__":
    unittest.main()
