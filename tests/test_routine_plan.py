"""The compiled Routine plan: admission, same-run JSON Pointer references, clock tokens, and bounds (ADR-0092)."""

from __future__ import annotations

import copy
import datetime
import json
import unittest
from unittest import mock

from assistant import action_schema
from assistant import manifest as assistant_manifest
from routine import plan as routine_plan

PIN = "sha256:" + "a" * 64
OTHER_PIN = "sha256:" + "b" * 64
PUBLISH = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 80},
        "day": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
        "count": {"type": "integer"},
        "note": {"type": ["string", "null"]},
    },
    "required": ["title"],
    "additionalProperties": False,
}
SHARE = {
    "type": "object",
    "properties": {
        "post_id": {"type": "string"},
        "tags": {"type": "array", "items": {"$ref": "#/$defs/tag"}},
        "at": {"type": "integer"},
        "api_token": {"type": "string"},
        "secret_note": {"type": "string", "writeOnly": True},
        "code": {"type": "string", "format": "password"},
    },
    "required": ["post_id"],
    "additionalProperties": False,
    "$defs": {"tag": {"type": "string", "minLength": 1}},
}
CONTRACTS = {
    ("shimpz-blog", "publish-post"): routine_plan.ActionContract(PIN, PUBLISH),
    ("shimpz-blog", "share-post"): routine_plan.ActionContract(OTHER_PIN, SHARE),
}


def _document(**changes: object) -> dict[str, object]:
    document = {
        "version": 1,
        "timezone": "America/Sao_Paulo",
        "steps": [
            {
                "id": "publish",
                "assistant": "shimpz-blog",
                "action": "publish-post",
                "pin": PIN,
                "input": {
                    "title": {"kind": "literal", "value": "Weekly report"},
                    "day": {"kind": "run_clock", "format": "date"},
                },
            },
            {
                "id": "share",
                "assistant": "shimpz-blog",
                "action": "share-post",
                "pin": OTHER_PIN,
                "input": {
                    "post_id": {"kind": "step_output", "step": "publish", "pointer": "/id"},
                    "tags": {"kind": "step_output", "step": "publish", "pointer": "/meta/a~1b/0"},
                    "at": {"kind": "run_clock", "format": "epoch_seconds"},
                },
            },
        ],
    }
    document.update(changes)
    return document


def _validator(schema: dict[str, object]):
    validator = assistant_manifest.action_schema_validator(schema)
    return lambda value: assistant_manifest.validate_schema_payload(validator, value)


class PlanAdmissionTests(unittest.TestCase):
    def assert_refused(self, document: object, code: str) -> None:
        with self.assertRaises(routine_plan.PlanError) as caught:
            routine_plan.admit(document, CONTRACTS)
        self.assertEqual(caught.exception.code, code)

    def test_a_plan_is_admitted_against_the_exact_current_contracts(self) -> None:
        document = _document()
        plan = routine_plan.admit(document, CONTRACTS)
        self.assertEqual([step.step_id for step in plan.steps], ["publish", "share"])
        self.assertEqual(plan.references("publish"), ("/id", "/meta/a~1b/0"))
        self.assertEqual(plan.references("share"), ())
        self.assertRegex(plan.digest, r"\Asha256:[0-9a-f]{64}\Z")
        self.assertEqual(routine_plan.admit(copy.deepcopy(document), CONTRACTS).digest, plan.digest)
        document["steps"][0]["input"]["title"]["value"] = "Changed"
        self.assertNotEqual(routine_plan.admit(document, CONTRACTS).digest, plan.digest)
        plan.steps[0].inputs["title"]["value"] = "mutated"
        self.assertEqual(document["steps"][0]["input"]["title"]["value"], "Changed")

    def test_the_closed_shape_version_timezone_and_bounds_are_enforced(self) -> None:
        nine = [{**_document()["steps"][0], "id": f"step{index}"} for index in range(9)]
        cases = (
            ([], "plan-invalid"),
            ({**_document(), "extra": 1}, "plan-invalid"),
            (_document(version=2), "plan-invalid"),
            (_document(steps=[]), "plan-invalid"),
            (_document(steps=nine), "plan-invalid"),
            (_document(steps={}), "plan-invalid"),
            (_document(timezone="Mars/Olympus"), "plan-timezone-invalid"),
            (_document(timezone=None), "plan-timezone-invalid"),
            (_document(steps=[{"id": "x"}]), "plan-step-invalid"),
            ({**_document(), "version": float("nan")}, "plan-invalid"),
        )
        for document, code in cases:
            with self.subTest(code=code, document=str(document)[:60]):
                self.assert_refused(document, code)
        large = _document()
        large["steps"][0]["input"]["title"]["value"] = "x" * routine_plan.MAX_PLAN_BYTES
        self.assert_refused(large, "plan-invalid")

    def test_each_step_names_one_exact_pinned_action_and_closed_inputs(self) -> None:
        def step(**changes: object) -> dict[str, object]:
            document = _document()
            document["steps"][0].update(changes)
            return document

        cases = (
            (step(id="Publish"), "plan-step-invalid"),
            (step(assistant="Shimpz"), "plan-step-invalid"),
            (step(action="Publish"), "plan-step-invalid"),
            (step(pin="sha256:x"), "plan-step-invalid"),
            (step(input=[]), "plan-step-invalid"),
            (step(pin=OTHER_PIN), "plan-pin-drift"),
            (step(action="delete-post"), "plan-pin-drift"),
            (
                step(input={"title": {"kind": "literal", "value": "x"}, "extra": {"kind": "literal", "value": 1}}),
                "plan-input-mismatch",
            ),
            (step(input={"day": {"kind": "run_clock", "format": "date"}}), "plan-input-mismatch"),
        )
        for document, code in cases:
            with self.subTest(code=code):
                self.assert_refused(document, code)
        duplicate = _document()
        duplicate["steps"][1]["id"] = "publish"
        self.assert_refused(duplicate, "plan-step-invalid")

    def test_every_value_source_is_closed_and_typed_at_creation(self) -> None:
        def source(name: str, value: object, step: int = 0) -> dict[str, object]:
            document = _document()
            document["steps"][step]["input"][name] = value
            return document

        cases = (
            (source("title", {"kind": "literal"}), "plan-input-invalid"),
            (source("title", {"kind": "template", "value": "{x}"}), "plan-input-invalid"),
            (source("title", "Weekly"), "plan-input-invalid"),
            (source("title", {"kind": "literal", "value": 7}), "plan-input-type"),
            (source("title", {"kind": "literal", "value": "x" * 81}), "plan-input-type"),
            (source("count", {"kind": "run_clock", "format": "date"}), "plan-input-type"),
            (source("day", {"kind": "run_clock", "format": "weekday"}), "plan-input-invalid"),
            (
                source("post_id", {"kind": "step_output", "step": "share", "pointer": "/id"}, 1),
                "plan-reference-invalid",
            ),
            (
                source("post_id", {"kind": "step_output", "step": "later", "pointer": "/id"}, 1),
                "plan-reference-invalid",
            ),
            (
                source("post_id", {"kind": "step_output", "step": "publish", "pointer": "id"}, 1),
                "plan-reference-invalid",
            ),
            (
                source("post_id", {"kind": "step_output", "step": "publish", "pointer": "/a~2"}, 1),
                "plan-reference-invalid",
            ),
            (source("title", {"kind": "step_output", "step": "publish", "pointer": "/id"}), "plan-reference-invalid"),
        )
        for document, code in cases:
            with self.subTest(code=code):
                self.assert_refused(document, code)
        nullable = source("note", {"kind": "literal", "value": None})
        self.assertEqual(routine_plan.admit(nullable, CONTRACTS).steps[0].inputs["note"]["value"], None)
        self.assert_refused(source("title", {"kind": "literal", "value": None}), "plan-input-type")

    def test_a_secret_is_never_a_literal(self) -> None:
        def literal(name: str, value: object, step: int = 1) -> dict[str, object]:
            document = _document()
            document["steps"][step]["input"][name] = {"kind": "literal", "value": value}
            return document

        for document in (
            literal("api_token", "plain"),
            literal("secret_note", "plain"),
            literal("code", "plain"),
            literal("title", "Bearer abcdefghijklmnop", 0),
            literal("title", "api_key=abc", 0),
            literal("tags", ["ok", "sk-abcdefghijklmnop"]),
            literal("note", "eyJhbGciOiJIUzI1.eyJzdWIiOjEyMzQ1.c2lnbmF0dXJlMTIz", 0),
        ):
            with self.subTest(document=json.dumps(document)[:200]):
                self.assert_refused(document, "plan-secret-literal")
        self.assertFalse(routine_plan._holds_credential({"tags": [1, None, {"k": "v"}]}))

    def test_a_secret_nested_anywhere_in_a_literal_is_refused(self) -> None:
        nested = {
            "type": "object",
            "properties": {
                "options": {
                    "type": "object",
                    "properties": {
                        "password": {"type": "string"},
                        "label": {"type": "string"},
                        "hidden": {"type": "string", "writeOnly": True},
                        "ref": {"$ref": "#/$defs/sealed"},
                        "either": {"anyOf": [{"type": "string", "format": "password"}, {"type": "integer"}]},
                        "pairs": {"type": "array", "items": {"$ref": "#/$defs/pair"}},
                        "tuple": {"type": "array", "prefixItems": [{"type": "string"}, {"$ref": "#/$defs/sealed"}]},
                    },
                    "additionalProperties": False,
                }
            },
            "required": ["options"],
            "additionalProperties": False,
            "$defs": {
                "sealed": {"type": "string", "writeOnly": True},
                "pair": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "value": {"$ref": "#/$defs/sealed"}},
                    "additionalProperties": False,
                },
            },
        }
        contracts = {("shimpz-blog", "publish-post"): routine_plan.ActionContract(PIN, nested)}

        def plan(options: object) -> dict[str, object]:
            document = _document()
            document["steps"] = [
                {
                    "id": "publish",
                    "assistant": "shimpz-blog",
                    "action": "publish-post",
                    "pin": PIN,
                    "input": {"options": {"kind": "literal", "value": options}},
                }
            ]
            return document

        for options in (
            {"password": "hunter2"},
            {"hidden": "plain"},
            {"ref": "plain"},
            {"either": "plain"},
            {"pairs": [{"name": "a"}, {"name": "b", "value": "plain"}]},
            {"tuple": ["first", "plain"]},
        ):
            with self.subTest(options=options), self.assertRaises(routine_plan.PlanError) as caught:
                routine_plan.admit(plan(options), contracts)
            self.assertEqual(caught.exception.code, "plan-secret-literal")
        admitted = routine_plan.admit(plan({"label": "x", "pairs": [{"name": "a"}], "tuple": ["first"]}), contracts)
        self.assertEqual(admitted.steps[0].inputs["options"]["value"]["label"], "x")
        deep: object = "x"
        for _ in range(routine_plan.MAX_SECRET_DEPTH + 1):
            deep = [deep]
        self.assertTrue(routine_plan._secret_literal(nested, "items", deep, {"type": "array"}, 0))
        chain = {"$defs": {f"d{index}": {"$ref": f"#/$defs/d{index + 1}"} for index in range(70)}}
        with self.assertRaises(routine_plan.PlanError) as indirect:
            routine_plan._applicable(chain, {"$ref": "#/$defs/d0"}, 0, None)
        self.assertEqual(indirect.exception.code, "plan-secret-literal")
        self.assertEqual(routine_plan._applicable(nested, None, 0, None), [])

    def test_every_admitted_applicator_is_followed_or_refused_for_secret_literals(self) -> None:
        sealed = {"type": "string", "writeOnly": True}
        plain = {"type": "string"}

        def options_schema(options: dict[str, object], **defs: object) -> dict[str, object]:
            schema = {
                "type": "object",
                "properties": {"options": options},
                "required": ["options"],
                "additionalProperties": False,
            }
            if defs:
                schema["$defs"] = defs
            # Each regression schema is one normal Action admission accepts.
            return action_schema.admitted(schema)

        def admit(schema: dict[str, object], options: object) -> routine_plan.Plan:
            document = _document()
            document["steps"] = [
                {
                    "id": "publish",
                    "assistant": "shimpz-blog",
                    "action": "publish-post",
                    "pin": PIN,
                    "input": {"options": {"kind": "literal", "value": options}},
                }
            ]
            return routine_plan.admit(
                document, {("shimpz-blog", "publish-post"): routine_plan.ActionContract(PIN, schema)}
            )

        pattern = options_schema(
            {
                "type": "object",
                "properties": {},
                "patternProperties": {"^value$": sealed, "^note$": plain},
                "additionalProperties": False,
            }
        )
        dependent = options_schema(
            {
                "type": "object",
                "properties": {"mode": plain, "x": plain},
                "dependentSchemas": {
                    "mode": {
                        "type": "object",
                        "properties": {"mode": plain, "x": sealed},
                        "additionalProperties": False,
                    }
                },
                "additionalProperties": False,
            }
        )
        conditional = options_schema(
            {
                "type": "object",
                "properties": {"x": plain},
                "if": {"type": "object", "properties": {"x": {"const": "a"}}, "additionalProperties": False},
                "then": {"type": "object", "properties": {"x": sealed}, "additionalProperties": False},
                "additionalProperties": False,
            }
        )
        negated = options_schema({"type": "string", "not": {"$ref": "#/$defs/sealed"}}, sealed=sealed)
        contained = options_schema({"type": "array", "items": plain, "contains": sealed})
        named = options_schema(
            {
                "type": "array",
                "items": plain,
                "contains": {"type": "object", "properties": {"api_key": plain}, "additionalProperties": False},
            }
        )
        harmless = options_schema(
            {
                "type": "object",
                "properties": {"x": plain},
                "if": {"type": "object", "properties": {"x": {"const": "a"}}, "additionalProperties": False},
                "then": {"type": "object", "properties": {"x": {"maxLength": 4}}, "additionalProperties": False},
                "additionalProperties": False,
            }
        )
        for schema, options in (
            (pattern, {"value": "hunter2"}),
            (dependent, {"mode": "a", "x": "hunter2"}),
            (conditional, {"x": "b"}),
            (negated, "plain"),
            (contained, ["plain"]),
            (named, ["plain"]),
        ):
            with self.subTest(options=options), self.assertRaises(routine_plan.PlanError) as caught:
                admit(schema, options)
            self.assertEqual(caught.exception.code, "plan-secret-literal")
        self.assertEqual(admit(pattern, {"note": "n"}).steps[0].inputs["options"]["value"], {"note": "n"})
        self.assertEqual(admit(dependent, {"x": "plain"}).steps[0].inputs["options"]["value"], {"x": "plain"})
        self.assertEqual(admit(harmless, {"x": "a"}).steps[0].inputs["options"]["value"], {"x": "a"})
        # An additional-properties schema applies only to members no property or pattern names.
        extra = {"properties": {"a": plain}, "patternProperties": {"^b$": plain}, "additionalProperties": sealed}
        self.assertEqual(routine_plan._member([extra], "c"), {"allOf": [sealed]})
        self.assertEqual(routine_plan._member([extra], "a"), {"allOf": [plain]})
        self.assertEqual(routine_plan._member([extra], "b"), {"allOf": [plain]})
        with self.assertRaises(routine_plan.PlanError) as unmatchable:
            routine_plan._member([{"patternProperties": {"(?<=a)b": plain}}], "ab")
        self.assertEqual(unmatchable.exception.code, "plan-secret-literal")
        deep: object = {"type": "string"}
        for _ in range(routine_plan.MAX_SECRET_DEPTH + 2):
            deep = {"not": deep}
        self.assertTrue(routine_plan._could_hold_secret({}, deep, 0))
        self.assertFalse(routine_plan._could_hold_secret({}, None, 0))

    def test_the_input_schema_root_is_the_first_position_of_the_secret_check(self) -> None:
        sealed = {"type": "string", "writeOnly": True}
        plain = {"type": "string"}

        def root(**members: object) -> dict[str, object]:
            schema = {
                "type": "object",
                "properties": {"value": plain, "mode": plain},
                "required": ["value"],
                "additionalProperties": False,
                **members,
            }
            # Each regression schema is one normal Action admission accepts.
            return action_schema.admitted(schema)

        def admit(schema: dict[str, object], inputs: dict[str, object]) -> routine_plan.Plan:
            document = _document()
            document["steps"] = [
                {"id": "publish", "assistant": "shimpz-blog", "action": "publish-post", "pin": PIN, "input": inputs}
            ]
            return routine_plan.admit(
                document, {("shimpz-blog", "publish-post"): routine_plan.ActionContract(PIN, schema)}
            )

        def closed(**properties: object) -> dict[str, object]:
            return {"type": "object", "properties": properties, "additionalProperties": False}

        literal = {"value": {"kind": "literal", "value": "hunter2"}}
        with_mode = {**literal, "mode": {"kind": "literal", "value": "a"}}
        cases = (
            (root(allOf=[closed(value=sealed, mode=plain)]), literal),
            (root(anyOf=[closed(value=sealed, mode=plain)]), literal),
            (root(patternProperties={"^val": sealed}), literal),
            (root(dependentSchemas={"mode": closed(value=sealed, mode=plain)}), with_mode),
            (root(**{"if": closed(mode={"const": "a"}), "then": closed(value=sealed, mode=plain)}), literal),
            (
                root(
                    **{"$ref": "#/$defs/sealed_value"}, **{"$defs": {"sealed_value": closed(value=sealed, mode=plain)}}
                ),
                literal,
            ),
        )
        for schema, inputs in cases:
            with self.subTest(schema=sorted(schema)), self.assertRaises(routine_plan.PlanError) as caught:
                admit(schema, inputs)
            self.assertEqual(caught.exception.code, "plan-secret-literal")
        dependent = cases[3][0]
        self.assertEqual(admit(dependent, literal).steps[0].inputs, literal)
        # A member filled at run time counts for presence: it triggers the dependent schema but is never a literal.
        step_output = {"value": {"kind": "run_clock", "format": "date"}, "mode": {"kind": "literal", "value": "a"}}
        self.assertEqual(admit(dependent, step_output).steps[0].inputs["value"]["kind"], "run_clock")
        harmless = root(allOf=[closed(value={"maxLength": 40}, mode=plain)])
        self.assertEqual(admit(harmless, literal).steps[0].inputs, literal)
        self.assertTrue(routine_plan._holds_credential({"password=abc": "v"}))


class PlanResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = routine_plan.admit(_document(), CONTRACTS)
        self.result = {"id": "post-1", "meta": {"a/b": [["news"], "x"], "~k": 1}, "list": [10, 20]}
        self.started_at = int(datetime.datetime(2026, 1, 1, 2, 30, tzinfo=datetime.UTC).timestamp())

    def test_a_pointer_selects_exactly_one_value_and_fails_closed_otherwise(self) -> None:
        self.assertEqual(routine_plan.select(self.result, "/meta/a~1b/0"), ["news"])
        self.assertEqual(routine_plan.select(self.result, "/meta/~0k"), 1)
        self.assertEqual(routine_plan.select(self.result, "/list/1"), 20)
        self.assertEqual(routine_plan.select(self.result, ""), self.result)
        selected = routine_plan.select(self.result, "/meta")
        selected["~k"] = 2
        self.assertEqual(self.result["meta"]["~k"], 1)
        for pointer in ("/missing", "/list/2", "/list/01", "/list/-", "/list/x", "/id/0", "/meta/a/b"):
            with self.subTest(pointer=pointer), self.assertRaises(routine_plan.PlanError) as caught:
                routine_plan.select(self.result, pointer)
            self.assertEqual(caught.exception.code, "plan-reference-missing")
        for pointer in ("id", "/~2", 7, "/" + "x" * 256):
            with self.subTest(pointer=pointer), self.assertRaises(routine_plan.PlanError) as caught:
                routine_plan.select(self.result, pointer)
            self.assertEqual(caught.exception.code, "plan-reference-invalid")

    def test_only_selected_values_are_retained_within_their_bound(self) -> None:
        chosen = routine_plan.selections(self.plan, "publish", self.result)
        self.assertEqual(chosen, {"/id": "post-1", "/meta/a~1b/0": ["news"]})
        self.assertEqual(routine_plan.selections(self.plan, "share", {"anything": 1}), {})
        self.assertTrue(routine_plan.retained_within({("publish", "/id"): "x"}))
        self.assertFalse(routine_plan.retained_within({("publish", "/id"): "x" * routine_plan.MAX_RETAINED_BYTES}))

    def test_resolution_copies_literals_renders_one_run_clock_and_validates_the_whole_input(self) -> None:
        publish, share = self.plan.steps
        resolved = routine_plan.resolve(self.plan, publish, {}, self.started_at, _validator(PUBLISH))
        # 02:30 UTC on 1 January is still 31 December in Sao Paulo.
        self.assertEqual(resolved, {"title": "Weekly report", "day": "2025-12-31"})
        selected = {("publish", "/id"): "post-1", ("publish", "/meta/a~1b/0"): ["news"]}
        shared = routine_plan.resolve(self.plan, share, selected, self.started_at, _validator(SHARE))
        self.assertEqual(shared, {"post_id": "post-1", "tags": ["news"], "at": self.started_at})
        shared["tags"].append("mutated")
        self.assertEqual(selected[("publish", "/meta/a~1b/0")], ["news"])
        with self.assertRaises(routine_plan.PlanError) as missing:
            routine_plan.resolve(self.plan, share, {("publish", "/id"): "post-1"}, self.started_at, _validator(SHARE))
        self.assertEqual(missing.exception.code, "plan-reference-missing")
        for wrong in (None, 7, [""]):
            with self.subTest(wrong=wrong), self.assertRaises(routine_plan.PlanError) as typed:
                routine_plan.resolve(
                    self.plan,
                    share,
                    {("publish", "/id"): "post-1", ("publish", "/meta/a~1b/0"): wrong},
                    self.started_at,
                    _validator(SHARE),
                )
            self.assertEqual(typed.exception.code, "plan-input-type")

    def test_clock_tokens_and_commitments_are_deterministic(self) -> None:
        instant = datetime.datetime(2026, 7, 4, 15, 5, 9, tzinfo=datetime.UTC)
        self.assertEqual(routine_plan.clock_value("date", instant, "Asia/Tokyo"), "2026-07-05")
        self.assertEqual(routine_plan.clock_value("time", instant, "Asia/Tokyo"), "00:05")
        self.assertEqual(routine_plan.clock_value("datetime", instant, "UTC"), "2026-07-04T15:05:09+00:00")
        self.assertEqual(routine_plan.clock_value("epoch_seconds", instant, "Asia/Tokyo"), int(instant.timestamp()))
        self.assertEqual(routine_plan.commitment({"b": 1, "a": "é"}), routine_plan.commitment({"a": "é", "b": 1}))
        self.assertNotEqual(routine_plan.commitment({"a": 1}), routine_plan.commitment({"a": "1"}))
        with (
            mock.patch.object(routine_plan.schedule, "zone", side_effect=routine_plan.schedule.ScheduleError("x")),
            self.assertRaises(routine_plan.PlanError),
        ):
            routine_plan.admit(_document(), CONTRACTS)


if __name__ == "__main__":
    unittest.main()
