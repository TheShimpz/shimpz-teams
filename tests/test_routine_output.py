"""What a completed Routine run does with its result (ADR-0092 amendment, 2026-10-05, output).

A plan names one closed output disposition; a shown result is Team's bounded, redacted projection of one step's
validated result, never a model's summary; ``changes`` shows it only when its keyed digest differs from the last one
shown; ``none`` publishes nothing new; and a ``step_text`` source hands a selected value on as unambiguous text.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import unittest
from unittest import mock

import routine_fixture
import test_routine_record as base
from test_routine_plan import CONTRACTS, _document

from protocol.http.v1 import routine as http_routine
from routine import change as routine_change
from routine import cursor as routine_cursor
from routine import plan as routine_plan
from routine import record
from routine import request as routine_request

ZONES = {
    "zones": [
        {"id": "z1", "name": "example.com", "paused": False, "status": "active"},
        {"id": "z2", "name": "example.org", "paused": True, "status": "pending"},
    ],
    "pagination": {"page": 1, "per_page": 50},
}


def _text(value: str, cut: bool = False) -> dict[str, object]:
    return {"kind": "text", "value": value, "cut": cut}


def _number(value: float) -> dict[str, object]:
    return {"kind": "number", "value": value}


def _shown_number(text: str) -> dict[str, object]:
    """A number as shown: its exact JSON text, so no consumer rounds it."""
    return {"kind": "number", "value": text}


class DispositionTests(unittest.TestCase):
    def test_a_plan_names_one_closed_disposition_whose_shown_step_is_its_own(self) -> None:
        for output in (
            {"mode": "show", "step": "share"},
            {"mode": "changes", "step": "publish"},
            {"mode": "chain", "step": None},
            {"mode": "none", "step": None},
        ):
            with self.subTest(output=output):
                plan = routine_plan.admit(_document(output=output), CONTRACTS)
                self.assertEqual(plan.output, output)
                shown = plan.shown()
                self.assertEqual(None if shown is None else shown.step_id, output["step"])
                self.assertTrue(routine_plan.well_formed(_document(output=output)))
        unbound = _document(output={"mode": "chain", "step": None})
        unbound["steps"] = unbound["steps"][:1]
        for output, document in (
            (None, None),
            ({"mode": "show"}, None),
            ({"mode": "show", "step": None}, None),
            ({"mode": "show", "step": "missing"}, None),
            ({"mode": "changes", "step": 1}, None),
            ({"mode": "none", "step": "publish"}, None),
            ({"mode": "loud", "step": None}, None),
            ({"mode": "show", "step": "share", "extra": 1}, None),
            # Handing the result on needs a step that takes an earlier step's value.
            (None, unbound),
        ):
            with self.subTest(output=output, document=document):
                candidate = document or _document(output=output)
                with self.assertRaisesRegex(routine_plan.PlanError, "plan-output-invalid"):
                    routine_plan.admit(candidate, CONTRACTS)
                self.assertFalse(routine_plan.well_formed(candidate))
        missing = _document()
        del missing["output"]
        with self.assertRaisesRegex(routine_plan.PlanError, "plan-invalid"):
            routine_plan.admit(missing, CONTRACTS)

    def test_the_protocol_admits_exactly_a_disposition_of_the_projected_steps(self) -> None:
        steps = [{"id": "zones"}, {"id": "records"}]
        self.assertEqual(
            http_routine.canonical_disposition({"mode": "show", "step": "records"}, steps),
            {"mode": "show", "step": "records"},
        )
        self.assertEqual(
            http_routine.canonical_disposition({"mode": "none", "step": None}, steps), {"mode": "none", "step": None}
        )
        for value, projected in (
            ({"mode": "show", "step": "other"}, steps),
            ({"mode": "show", "step": "zones"}, "steps"),
            ({"mode": "chain", "step": "zones"}, steps),
            ({"mode": "show"}, steps),
            ([], steps),
        ):
            with self.subTest(value=value):
                self.assertIsNone(http_routine.canonical_disposition(value, projected))


class ProjectionTests(unittest.TestCase):
    def test_a_result_is_shown_complete_in_sorted_order_when_it_fits(self) -> None:
        safe = routine_plan.output_safe(ZONES, {})
        shown = routine_plan.output_shown("zones", safe)
        first = {
            "kind": "fields",
            "fields": [
                ["id", _text("z1")],
                ["name", _text("example.com")],
                ["paused", {"kind": "bool", "value": False}],
                ["status", _text("active")],
            ],
            "omitted": 0,
        }
        self.assertEqual(shown["state"], "shown")
        self.assertFalse(shown["truncated"])
        pagination, zones = shown["value"]["fields"]
        self.assertEqual(
            pagination,
            [
                "pagination",
                {
                    "kind": "fields",
                    "fields": [["page", _shown_number("1")], ["per_page", _shown_number("50")]],
                    "omitted": 0,
                },
            ],
        )
        self.assertEqual(zones[1]["items"][0], first)
        self.assertEqual(http_routine.canonical_output(shown), shown)
        self.assertEqual(routine_plan.output_safe(None, {}), {"kind": "null"})
        self.assertEqual(routine_plan.output_safe(1.5, {}), _number(1.5))

    def test_secrets_are_redacted_before_any_cut_by_value_key_and_schema(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "sealed": {"type": "string", "writeOnly": True},
                "nested": {"$ref": "#/$defs/hidden"},
                "items": {"type": "array", "items": {"type": "string", "format": "password"}},
            },
            "$defs": {"hidden": {"type": "string", "format": "password"}},
        }
        result = {
            "sealed": "visible?",
            "nested": "also?",
            "items": ["a", "b"],
            "api_token": "tokenvalue",
            "note": "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789": "x",
            "Authorization: Bearer zyxwvutsrqponmlkjihgfedcba9876543210": "y",
            "plain": "ok",
        }
        safe = routine_plan.output_safe(result, schema)
        fields = dict(safe["fields"])
        for key in ("sealed", "nested", "api_token", "note", "[redacted]", "[redacted] 2"):
            with self.subTest(key=key):
                self.assertEqual(fields[key], {"kind": "redacted"})
        self.assertEqual(fields["items"], {"kind": "list", "items": [{"kind": "redacted"}] * 2, "omitted": 0})
        self.assertEqual(fields["plain"], _text("ok"))
        encoded = json.dumps(safe)
        for secret in ("visible?", "also?", "abcdefghijklmnopqrstuvwxyz", "tokenvalue"):
            self.assertNotIn(secret, encoded)
        # A schema the walk cannot follow redacts that position instead of refusing the result.
        unreadable = {"type": "object", "patternProperties": {"(?<=a)b": {"type": "string"}}}
        self.assertEqual(routine_plan.output_safe({"ab": "x"}, unreadable)["fields"], [["ab", {"kind": "redacted"}]])

    def test_keys_and_text_are_escaped_and_labels_stay_distinct_only_when_shown(self) -> None:
        long = "k" * 100
        result = {"\u202e": "a\u0000b", "\\u202e": 1, "": 2, long: 3, long + "x": 4}
        safe = routine_plan.output_safe(result, {})
        # The safe form keeps every key and text exactly; only the shown form escapes and shortens them.
        self.assertEqual([key for key, _value in safe["fields"]], sorted(result))
        self.assertIn(["\u202e", _text("a\u0000b")], safe["fields"])
        shown = routine_plan.output_shown("zones", safe)
        labels = [key for key, _value in shown["value"]["fields"]]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertIn('""', labels)
        self.assertIn("\\u202e", labels)
        self.assertIn("\\u202e (2)", labels)
        self.assertIn(["\\u202e (2)", _text("a\\u0000b")], shown["value"]["fields"])
        self.assertTrue(all(len(label) <= http_routine.MAX_OUTPUT_KEY_CHARS for label in labels))
        # A shortened key is a cut, so the output says it is truncated.
        self.assertTrue(shown["truncated"])
        self.assertEqual(http_routine.canonical_output(shown), shown)
        self.assertFalse(routine_plan.output_shown("zones", routine_plan.output_safe({"k": 1}, {}))["truncated"])
        many = routine_plan.output_shown("zones", routine_plan.output_safe({"same": 0, "same (2)": 1}, {}))
        self.assertEqual([label for label, _value in many["value"]["fields"]], ["same", "same (2)"])
        labels, shortened = routine_plan._output_labels(["same"] * 5)
        self.assertEqual((labels, shortened), (["same", "same (2)", "same (3)", "same (4)", "same (5)"], False))

    def test_a_change_is_compared_on_the_exact_data_never_its_shown_form(self) -> None:
        long = "k" * 100
        label, _cut = routine_plan.output_label(long)
        pairs = (
            ({"v": "\u202e"}, {"v": "\\u202e"}),
            ({long: 1}, {label: 1}),
            ({"n": 0.1}, {"n": 0.10000000000000002}),
            ({"n": 10**400}, {"n": 10**400 + 1}),
        )
        for first, second in pairs:
            with self.subTest(first=str(first)[:40]):
                material = [
                    routine_plan.output_compared(routine_plan.output_safe(item, {})) for item in (first, second)
                ]
                self.assertNotEqual(*material)
        # A change only inside redacted content compares equal.
        hidden = [routine_plan.output_safe({"token": value}, {}) for value in ("a", "b")]
        self.assertEqual(*[routine_plan.output_compared(node) for node in hidden])
        self.assertIsNone(routine_plan.output_compared({"kind": "number", "value": float("inf")}))

    def test_a_number_is_shown_as_its_exact_text(self) -> None:
        shown = routine_plan.output_shown("zones", routine_plan.output_safe([0.0001, 1.23456, 10**30, 10**400], {}))
        items = shown["value"]["items"]
        self.assertEqual(items[:3], [_shown_number("0.0001"), _shown_number("1.23456"), _shown_number(str(10**30))])
        # A number too long for a number node is shown as its exact text, cut like any text.
        self.assertEqual(items[3], _text(str(10**400)[:299] + "…", cut=True))
        self.assertTrue(shown["truncated"])
        self.assertEqual(http_routine.canonical_output(shown), shown)
        sixty_five = routine_plan.output_shown("zones", routine_plan.output_safe(10**64, {}))
        self.assertEqual(sixty_five["value"], _text(str(10**64)))

    def test_a_large_result_is_cut_down_a_fixed_ladder_until_it_fits(self) -> None:
        rows = [{"name": f"zone-{index}.example", "note": "é" * 400} for index in range(400)]
        shown = routine_plan.output_shown("zones", routine_plan.output_safe({"zones": rows}, {}))
        self.assertTrue(shown["truncated"])
        self.assertLessEqual(http_routine.encoded_bytes(shown), http_routine.MAX_OUTPUT_BYTES)
        self.assertEqual(http_routine.canonical_output(shown), shown)
        zones = shown["value"]["fields"][0][1]
        self.assertEqual(zones["omitted"], 400 - len(zones["items"]))
        self.assertTrue(zones["items"][0]["fields"][1][1]["cut"])
        # Containers past the depth bound are elided; a value nothing can shrink enough is elided whole.
        deep = routine_plan.output_shown("zones", routine_plan.output_safe({"a": {"b": {"c": {"d": {"e": 1}}}}}, {}))
        self.assertEqual(json.dumps(deep).count('"elided"'), 1)
        self.assertTrue(deep["truncated"])
        huge = {"kind": "text", "value": "é" * 20_000, "cut": False}
        cut = routine_plan.output_shown("zones", huge)
        self.assertLessEqual(len(cut["value"]["value"]), http_routine.MAX_OUTPUT_TEXT_CHARS)
        wide = {"kind": "fields", "fields": [[f"{index:02d}" + "é" * 60, huge] for index in range(24)], "omitted": 0}
        with mock.patch.object(routine_plan, "OUTPUT_LEVELS", ((24, 24, 300),)):
            self.assertEqual(
                routine_plan.output_shown("zones", wide),
                {"step": "zones", "state": "shown", "value": {"kind": "elided"}, "truncated": True},
            )
        self.assertEqual(routine_plan.output_safe([[[[[[1]]]]]], {})["kind"], "list")
        with mock.patch.object(routine_plan, "MAX_SAFE_OUTPUT_DEPTH", 1):
            self.assertEqual(routine_plan.output_safe([[1]], {})["items"][0]["items"][0], {"kind": "elided"})

    def test_a_result_that_cannot_be_projected_is_unavailable_and_compared_only_when_small(self) -> None:
        for result in (float("nan"), float("inf"), {"bad": object()}):
            with self.subTest(result=result), self.assertRaises(routine_plan.OutputError):
                routine_plan.output_safe(result, {})
        self.assertEqual(
            routine_plan.output_state("zones", "unavailable"),
            {"step": "zones", "state": "unavailable", "value": None, "truncated": False},
        )
        small = routine_plan.output_safe(ZONES, {})
        self.assertEqual(routine_plan.output_compared(small), routine_plan.canonical(small))
        with mock.patch.object(routine_plan, "MAX_COMPARED_OUTPUT_BYTES", 10):
            self.assertIsNone(routine_plan.output_compared(small))

    def test_the_protocol_admits_exactly_closed_output_nodes(self) -> None:
        shown = routine_plan.output_shown("zones", routine_plan.output_safe(ZONES, {}))
        self.assertEqual(http_routine.canonical_output(shown), shown)
        for state in ("unchanged", "unavailable"):
            value = routine_plan.output_state("zones", state)
            self.assertEqual(http_routine.canonical_output(value), value)
        nested = {"kind": "list", "items": [], "omitted": 0}
        for _depth in range(http_routine.MAX_OUTPUT_DEPTH):
            nested = {"kind": "list", "items": [nested], "omitted": 0}
        invalid = (
            {**shown, "extra": 1},
            {**shown, "step": "Zones"},
            {**shown, "state": "hidden"},
            {**shown, "truncated": 0},
            {**shown, "value": None},
            {**shown, "state": "unchanged"},
            {**routine_plan.output_state("zones", "unchanged"), "truncated": True},
            {**shown, "value": {"kind": "text", "value": "a\u202eb", "cut": False}},
            {**shown, "value": {"kind": "text", "value": "x" * 301, "cut": False}},
            {**shown, "value": {"kind": "text", "value": "x"}},
            {**shown, "value": {"kind": "number", "value": True}},
            {**shown, "value": {"kind": "number", "value": 12}},
            {**shown, "value": {"kind": "number", "value": "01"}},
            {**shown, "value": {"kind": "number", "value": "1."}},
            {**shown, "value": {"kind": "number", "value": "NaN"}},
            {**shown, "value": {"kind": "number", "value": "1" * 65}},
            {**shown, "value": {"kind": "number", "value": float("inf")}},
            {**shown, "value": {"kind": "bool", "value": 1}},
            {**shown, "value": {"kind": "null", "value": None}},
            {**shown, "value": {"kind": "secret"}},
            {**shown, "value": nested},
            {**shown, "value": {"kind": "list", "items": [], "omitted": -1}},
            {**shown, "value": {"kind": "list", "items": [_number(1)] * 51, "omitted": 0}},
            {**shown, "value": {"kind": "list", "items": "x", "omitted": 0}},
            {**shown, "value": {"kind": "fields", "fields": [["a", _number(1)], ["a", _number(2)]], "omitted": 0}},
            {**shown, "value": {"kind": "fields", "fields": [["", _number(1)]], "omitted": 0}},
            {**shown, "value": {"kind": "fields", "fields": [["a"]], "omitted": 0}},
            {**shown, "value": {"kind": "fields", "fields": [["k", _number(1)]] * 25, "omitted": 0}},
            {**shown, "value": {"kind": "fields", "fields": [], "omitted": 0, "x": 1}},
            {**shown, "value": "text"},
            {
                **shown,
                "value": {"kind": "list", "items": [_text("é" * 300)] * 50, "omitted": 0},
            },
            [],
        )
        for value in invalid:
            with self.subTest(value=str(value)[:120]):
                self.assertIsNone(http_routine.canonical_output(value))


class TextTests(unittest.TestCase):
    def test_a_selected_value_becomes_unambiguous_text(self) -> None:
        self.assertEqual(routine_plan.text("line one\nline two"), "line one\nline two")
        self.assertEqual(routine_plan.text(5), "5")
        self.assertEqual(routine_plan.text(None), "null")
        self.assertEqual(routine_plan.text([]), "[]")
        self.assertEqual(routine_plan.text({}), "{}")
        value = {"zones": [{"name": "a\n- forged: line", "id": 1}, "b", []], "count": 2}
        self.assertEqual(
            routine_plan.text(value),
            '"count": 2\n"zones":\n  -\n    "id": 1\n    "name": "a\\n- forged: line"\n  - "b"\n  - []',
        )

    def test_a_step_text_source_copies_its_value_as_text_or_refuses_before_dispatch(self) -> None:
        document = _document(output={"mode": "chain", "step": None})
        document["steps"][1]["input"]["post_id"] = {"kind": "step_text", "step": "publish", "pointer": "/meta"}
        plan = routine_plan.admit(document, CONTRACTS)
        self.assertEqual(plan.references("publish"), ("/meta", "/meta/a~1b/0"))
        selected = {("publish", "/meta"): {"a/b": ["x"]}, ("publish", "/meta/a~1b/0"): ["x"]}
        resolved = routine_plan.resolve(plan, plan.steps[1], selected, 0, lambda value: value)
        self.assertEqual(resolved["post_id"], '"a/b":\n  - "x"')
        with (
            mock.patch.object(routine_plan, "MAX_TEXT_BYTES", 5),
            self.assertRaisesRegex(routine_plan.PlanError, "plan-input-type"),
        ):
            routine_plan.resolve(plan, plan.steps[1], selected, 0, lambda value: value)
        document["steps"][1]["input"]["post_id"]["step"] = "share"
        # A value that cannot be encoded as text is refused as a mistyped input, before any dispatch.
        unencodable = {("publish", "/meta"): "lone \ud800 surrogate", ("publish", "/meta/a~1b/0"): ["x"]}
        with self.assertRaisesRegex(routine_plan.PlanError, "plan-input-type"):
            routine_plan.resolve(plan, plan.steps[1], unencodable, 0, lambda value: value)
        with self.assertRaisesRegex(routine_plan.PlanError, "plan-reference-invalid"):
            routine_plan.admit(document, CONTRACTS)


class CursorSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = routine_plan.admit(_document(output={"mode": "show", "step": "publish"}), CONTRACTS)
        binding = routine_cursor.Binding("a" * 64, "b" * 32, 1, "c" * 32)
        self.cursor = routine_cursor.dispatch(
            routine_cursor.start(self.plan, binding, 0), self.plan, "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6", "d" * 64
        )
        self.result = {"id": "post-1", "meta": {"a/b": [["news"]]}}
        node = routine_plan.output_safe(self.result, {})
        self.slot = {"step": "publish", "output": routine_plan.output_shown("publish", node), "digest": "e" * 64}

    def test_only_the_shown_step_keeps_its_slot_and_it_outlives_later_steps(self) -> None:
        advanced = routine_cursor.complete(self.cursor, self.plan, self.result, self.slot)
        self.assertEqual(advanced.shown, self.slot)
        decoded = routine_cursor.decode(routine_cursor.encode(advanced), advanced.binding)
        self.assertEqual(decoded.shown, self.slot)
        last = routine_cursor.complete(
            routine_cursor.dispatch(advanced, self.plan, "7a2d3c9f-4b5e-4d6f-8a70-829304b5c6d7", "d" * 64),
            self.plan,
            {},
        )
        self.assertEqual(last.shown, self.slot)
        self.assertEqual(routine_cursor.continued(last).shown, self.slot)
        for slot in (None, {**self.slot, "step": "share"}):
            with self.subTest(slot=slot), self.assertRaisesRegex(routine_cursor.CursorError, "cursor-shown-invalid"):
                routine_cursor.complete(self.cursor, self.plan, self.result, slot)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-shown-invalid"):
            routine_cursor.complete(
                routine_cursor.dispatch(advanced, self.plan, "7a2d3c9f-4b5e-4d6f-8a70-829304b5c6d7", "d" * 64),
                self.plan,
                {},
                {**self.slot, "step": "share"},
            )

    def test_a_slot_outside_its_closed_shape_is_refused(self) -> None:
        unavailable = {"step": "publish", "output": routine_plan.output_state("publish", "unavailable"), "digest": None}
        self.assertEqual(routine_cursor.complete(self.cursor, self.plan, self.result, unavailable).shown, unavailable)
        for slot in (
            {**self.slot, "extra": 1},
            {**self.slot, "digest": "E" * 64},
            {**self.slot, "output": {"step": "publish"}},
            {**self.slot, "output": routine_plan.output_state("other", "unavailable")},
            {**self.slot, "output": routine_plan.output_state("publish", "unchanged")},
            {**unavailable, "digest": "e" * 64},
            [],
        ):
            with self.subTest(slot=slot), self.assertRaises(routine_cursor.CursorError):
                routine_cursor.complete(self.cursor, self.plan, self.result, slot)


def _completed(mode: str, *, notice_version: int = 0, digest: str = "", shown: dict | None = None):
    """A claimed run of a one-step Routine with this disposition, its Routine's last digest, and its finish."""
    output = {"mode": mode, "step": "check" if mode in ("show", "changes") else None}
    plan = routine_fixture.chain_document() if mode == "chain" else routine_fixture.plan_document(output=output)
    if mode == "chain":
        plan["output"] = {"mode": "chain", "step": None}
    value = base.routine(plan=plan)
    state = base.at(base.added(value), value.routine_id, base.NINE)
    current = dataclasses.replace(record.routine(state, value.routine_id), output_digest=digest, failures=2)
    state, claim = record.claim(record._replace_routine(state, current), base.NINE, base.KEY)
    if notice_version:
        state = record._replace_run(state, dataclasses.replace(record.run(state, claim.run.run_id), notice_version=1))
    lease = record.lease_of(claim.lease_token, base.KEY)
    return record.finish(state, claim.run.run_id, lease, base.NINE + 5, "done", {}, shown)


class CompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        node = routine_plan.output_safe(ZONES, {})
        self.output = routine_plan.output_shown("check", node)
        self.shown = {"step": "check", "output": self.output, "digest": "a" * 64}

    def test_show_publishes_the_result_every_run_and_unavailable_when_it_was_not_kept(self) -> None:
        state = _completed("show", shown=self.shown)
        (notice,) = state.notices
        self.assertEqual(notice.detail, {"actions": [["dns", "check"]], "output": self.output})
        self.assertEqual(state.routines[0].failures, 0)
        self.assertEqual(state.routines[0].output_digest, "")
        (missing,) = _completed("show").notices
        self.assertEqual(missing.detail["output"], routine_plan.output_state("check", "unavailable"))
        wrong = {**self.shown, "step": "other"}
        self.assertEqual(_completed("show", shown=wrong).notices[0].detail["output"]["state"], "unavailable")

    def test_changes_publishes_only_a_changed_result_and_records_what_it_showed(self) -> None:
        state = _completed("changes", shown=self.shown)
        self.assertEqual(state.notices[0].detail["output"], self.output)
        self.assertEqual(state.routines[0].output_digest, "a" * 64)
        quiet = _completed("changes", digest="a" * 64, shown=self.shown)
        self.assertEqual((quiet.notices, quiet.routines[0].failures, quiet.runs), ((), 0, ()))
        # A run with a notice of its own gets its terminal version, saying unchanged instead of repeating the result.
        answered = _completed("changes", notice_version=1, digest="a" * 64, shown=self.shown)
        self.assertEqual(answered.notices[0].detail["output"], routine_plan.output_state("check", "unchanged"))
        # A result too large to compare is always shown, and leaves no baseline a later result could match.
        incomparable = _completed("changes", digest="a" * 64, shown={**self.shown, "digest": None})
        self.assertEqual(incomparable.notices[0].detail["output"], self.output)
        self.assertEqual(incomparable.routines[0].output_digest, "")
        # An unavailable result never moves the baseline.
        unavailable = _completed("changes", digest="b" * 64)
        self.assertEqual(unavailable.notices[0].detail["output"]["state"], "unavailable")
        self.assertEqual(unavailable.routines[0].output_digest, "b" * 64)

    def test_none_publishes_nothing_new_and_chain_keeps_the_compact_completion(self) -> None:
        quiet = _completed("none")
        self.assertEqual((quiet.notices, quiet.routines[0].failures), ((), 0))
        answered = _completed("none", notice_version=1)
        self.assertEqual(answered.notices[0].detail, {"actions": [["dns", "check"]], "output": None})
        chained = _completed("chain")
        self.assertEqual(chained.notices[0].detail, {"actions": [["dns", "check"], ["dns", "notify"]], "output": None})

    def test_the_watchdog_completes_a_run_through_the_same_disposition(self) -> None:
        value = dataclasses.replace(base.routine(), output_digest="")
        state, claim = record.claim(base.at(base.added(value), value.routine_id, base.NINE), base.NINE, base.KEY)
        lease = record.lease_of(claim.lease_token, base.KEY)
        state = record.bind_generation(state, claim.run.run_id, lease, base.NINE, "net_1")
        done = record.complete_recovered(state, claim.run.run_id, claim.run.lease_sha256, base.NINE + 5, self.shown)
        self.assertEqual(done.notices[-1].detail["output"], self.output)

    def test_an_update_starts_a_fresh_baseline(self) -> None:
        state = _completed("changes", shown=self.shown)
        current = state.routines[0]
        changed = record.scheduled(dataclasses.replace(current, output_digest="a" * 64), base.NINE + 10)
        updated, _fresh = record.update(state, changed, 1, base.NINE + 10, base.RECEIPT, base.NINE + 900)
        self.assertEqual(updated.routines[0].output_digest, "")


class ChangeDispositionTests(unittest.TestCase):
    MESSAGE = "Every Monday at 9, list my zones and show me the result"

    def change(self, **changes: object) -> dict[str, object]:
        value = {
            "op": "create",
            "routine_id": None,
            "expected_revision": None,
            "continues": False,
            "name": "Zones",
            "request": "Every Monday at 9, list my zones",
            "schedule": {"kind": "weekly", "weekday": 0, "time": "09:00"},
            "timezone": None,
            "steps": [{"id": "publish", "assistant": "shimpz-blog", "action": "publish-post", "input": {}}],
            "output": {"mode": "show", "step": "publish", "instruction": "show me the result"},
        }
        value["steps"][0]["input"]["title"] = {
            "kind": "literal",
            "value": "zones",
            "origins": [{"at": "", "from": "message", "text": "zones", "region": None, "instruction": None}],
        }
        value.update(changes)
        return value

    def compile(self, value: dict, message: str | None = None, earlier: tuple[str, ...] = (), current=None):
        words = routine_change.Words(
            routine_request.Request("p", message or self.MESSAGE, 0, "n", earlier=earlier).parts()
        )
        return routine_change.compile_change(routine_change.parse(value), words, CONTRACTS, current, "UTC")

    def test_said_words_choose_the_disposition_and_are_its_provenance(self) -> None:
        compiled = self.compile(self.change())
        self.assertEqual(compiled.document["output"], {"mode": "show", "step": "publish"})
        start = self.MESSAGE.index("show me the result")
        self.assertEqual(compiled.output, {"proof": {"instruction": [start, start + 18]}, "by": None})
        self.assertEqual(routine_change.parse(self.change()).to_dict()["output"], self.change()["output"])

    def test_a_disposition_only_a_cited_send_states_is_unproven(self) -> None:
        cited = "list my zones and show me the result"
        with self.assertRaisesRegex(routine_change.ChangeError, "routine-output-unproven"):
            self.compile(
                self.change(request="Every Monday at 9, do this"), "Every Monday at 9, do this", earlier=(cited,)
            )
        with self.assertRaisesRegex(routine_change.ChangeError, "routine-output-unproven"):
            self.compile(self.change(output={"mode": "show", "step": "publish", "instruction": "show it all"}))

    def test_the_closed_output_shape_is_enforced(self) -> None:
        for output in (
            None,
            {"mode": "kept"},
            {"mode": "show", "step": None, "instruction": "x"},
            {"mode": "show", "step": "Bad", "instruction": "x"},
            {"mode": "none", "step": "publish", "instruction": "x"},
            {"mode": "loud", "step": None, "instruction": "x"},
            {"mode": "none", "step": None, "instruction": ""},
            {"mode": "none", "step": None},
        ):
            with self.subTest(output=output), self.assertRaisesRegex(routine_change.ChangeError, "change-invalid"):
                routine_change.parse(self.change(output=output))
        update = self.change(op="update", routine_id="c" * 32, expected_revision=1, output={"mode": "kept"})
        self.assertEqual(routine_change.parse(update).output, {"mode": "kept"})

    def test_an_update_keeps_the_disposition_only_while_its_step_names_the_same_action(self) -> None:
        created = self.compile(self.change())
        current = (created.document, created.sources, {**created.output, "by": {"kept": True}})
        update = self.change(op="update", routine_id="c" * 32, expected_revision=1, output={"mode": "kept"})
        kept = self.compile(update, current=current)
        self.assertEqual((kept.document["output"], kept.output), (created.document["output"], current[2]))
        moved = copy.deepcopy(update)
        moved["steps"][0]["action"] = "share-post"
        moved["steps"][0]["input"] = {
            "post_id": {"kind": "literal", "value": "zones", "origins": update["steps"][0]["input"]["title"]["origins"]}
        }
        with self.assertRaisesRegex(routine_change.ChangeError, "routine-output-unproven"):
            self.compile(moved, current=current)
        unshown = {**created.document, "output": {"mode": "none", "step": None}}
        self.assertEqual(self.compile(moved, current=(unshown, {}, current[2])).document["output"]["mode"], "none")


if __name__ == "__main__":
    unittest.main()
