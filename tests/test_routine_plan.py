"""The recorded Routine plan: admission, same-run references and selectors, the run date, and bounds (ADR-0101)."""

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
        "version": 3,
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
                },
            },
        ],
        "output": {"mode": "show", "step": "publish", "when": None},
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
        self.assertEqual(plan.references("publish"), (("/id", "", ""), ("/meta/a~1b/0", "", "")))
        self.assertEqual(plan.references("share"), ())
        self.assertRegex(plan.digest, r"\Asha256:[0-9a-f]{64}\Z")
        self.assertEqual(routine_plan.admit(copy.deepcopy(document), CONTRACTS).digest, plan.digest)
        document["steps"][0]["input"]["title"]["value"] = "Changed"
        self.assertNotEqual(routine_plan.admit(document, CONTRACTS).digest, plan.digest)
        plan.steps[0].inputs["title"]["value"] = "mutated"
        self.assertEqual(document["steps"][0]["input"]["title"]["value"], "Changed")

    def test_the_closed_shape_version_timezone_and_bounds_are_enforced(self) -> None:
        too_many = [{**_document()["steps"][0], "id": f"step{index}"} for index in range(routine_plan.MAX_STEPS + 1)]
        cases = (
            ([], "plan-invalid"),
            ({**_document(), "extra": 1}, "plan-invalid"),
            (_document(version=1), "plan-invalid"),
            (_document(steps=[]), "plan-output-invalid"),
            (_document(steps=too_many), "plan-invalid"),
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

    def test_an_action_that_takes_a_file_cannot_be_compiled(self) -> None:
        # A Routine holds no file grant, so even a literal id never stands for an attached file (ADR-0093).
        contracts = {
            **CONTRACTS,
            ("shimpz-blog", "publish-post"): routine_plan.ActionContract(PIN, PUBLISH, ("title",)),
        }
        with self.assertRaises(routine_plan.PlanError) as refused:
            routine_plan.admit(_document(), contracts)
        self.assertEqual(refused.exception.code, "plan-file-input")

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
            (source("day", {"kind": "run_clock", "format": "time"}), "plan-input-invalid"),
            (source("title", {"kind": "step_text", "step": "publish", "pointer": "/id"}, 1), "plan-input-invalid"),
            (
                source("post_id", {"kind": "step_output", "step": "publish", "pointer": "/a", "where": {}}, 1),
                "plan-input-invalid",
            ),
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
            routine_plan.applicable(chain, {"$ref": "#/$defs/d0"}, 0, None)
        self.assertEqual(indirect.exception.code, "plan-secret-literal")
        self.assertEqual(routine_plan.applicable(nested, None, 0, None), [])

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
        self.assertEqual(routine_plan.member_schemas([extra], "c"), {"allOf": [sealed]})
        self.assertEqual(routine_plan.member_schemas([extra], "a"), {"allOf": [plain]})
        self.assertEqual(routine_plan.member_schemas([extra], "b"), {"allOf": [plain]})
        with self.assertRaises(routine_plan.PlanError) as unmatchable:
            routine_plan.member_schemas([{"patternProperties": {"(?<=a)b": plain}}], "ab")
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
        self.assertEqual(chosen, {("/id", "", ""): "post-1", ("/meta/a~1b/0", "", ""): ["news"]})
        self.assertEqual(routine_plan.selections(self.plan, "share", {"anything": 1}), {})
        self.assertTrue(routine_plan.retained_within({("publish", "/id", "", ""): "x"}))
        large = {("publish", "/id", "", ""): "x" * routine_plan.MAX_RETAINED_BYTES}
        self.assertFalse(routine_plan.retained_within(large))

    def test_resolution_copies_literals_renders_one_run_clock_and_validates_the_whole_input(self) -> None:
        publish, share = self.plan.steps
        resolved = routine_plan.resolve(self.plan, publish, {}, self.started_at, _validator(PUBLISH))
        # 02:30 UTC on 1 January is still 31 December in Sao Paulo.
        self.assertEqual(resolved, {"title": "Weekly report", "day": "2025-12-31"})
        selected = {("publish", "/id", "", ""): "post-1", ("publish", "/meta/a~1b/0", "", ""): ["news"]}
        shared = routine_plan.resolve(self.plan, share, selected, self.started_at, _validator(SHARE))
        self.assertEqual(shared, {"post_id": "post-1", "tags": ["news"]})
        shared["tags"].append("mutated")
        self.assertEqual(selected[("publish", "/meta/a~1b/0", "", "")], ["news"])
        only = {("publish", "/id", "", ""): "post-1"}
        with self.assertRaises(routine_plan.PlanError) as missing:
            routine_plan.resolve(self.plan, share, only, self.started_at, _validator(SHARE))
        self.assertEqual(missing.exception.code, "plan-reference-missing")
        for wrong in (None, 7, [""]):
            with self.subTest(wrong=wrong), self.assertRaises(routine_plan.PlanError) as typed:
                routine_plan.resolve(
                    self.plan,
                    share,
                    {("publish", "/id", "", ""): "post-1", ("publish", "/meta/a~1b/0", "", ""): wrong},
                    self.started_at,
                    _validator(SHARE),
                )
            self.assertEqual(typed.exception.code, "plan-input-type")

    def test_clock_tokens_and_commitments_are_deterministic(self) -> None:
        instant = datetime.datetime(2026, 7, 4, 15, 5, 9, tzinfo=datetime.UTC)
        self.assertEqual(routine_plan.clock_value("date", instant, "Asia/Tokyo"), "2026-07-05")
        self.assertEqual(routine_plan.clock_value("date", instant, "UTC"), "2026-07-04")
        self.assertEqual(routine_plan.commitment({"b": 1, "a": "é"}), routine_plan.commitment({"a": "é", "b": 1}))
        self.assertNotEqual(routine_plan.commitment({"a": 1}), routine_plan.commitment({"a": "1"}))
        with (
            mock.patch.object(routine_plan.schedule, "zone", side_effect=routine_plan.schedule.ScheduleError("x")),
            self.assertRaises(routine_plan.PlanError),
        ):
            routine_plan.admit(_document(), CONTRACTS)


class ScaleTests(unittest.TestCase):
    """One Action many times with its own inputs, plans of hundreds of steps (ADR-0092 amendment, 2026-10-05, scale)."""

    @staticmethod
    def _publish(step_id: str, **inputs: dict[str, object]) -> dict[str, object]:
        return {"id": step_id, "assistant": "shimpz-blog", "action": "publish-post", "pin": PIN, "input": inputs}

    def test_one_action_runs_many_times_each_with_its_own_required_and_optional_inputs(self) -> None:
        steps = [
            self._publish("first", title={"kind": "literal", "value": "One"}, count={"kind": "literal", "value": 1}),
            self._publish("second", title={"kind": "literal", "value": "Two"}, note={"kind": "literal", "value": None}),
            self._publish("third", title={"kind": "step_output", "step": "first", "pointer": "/id"}),
        ]
        document = {**_document(steps=steps), "output": {"mode": "show", "step": "third", "when": None}}
        plan = routine_plan.admit(document, CONTRACTS)
        self.assertEqual([step.action for step in plan.steps], ["publish-post"] * 3)
        # An optional member is present only where the plan sets it; nothing fills one in.
        members = [sorted(step.inputs) for step in plan.steps]
        self.assertEqual(members, [["count", "title"], ["note", "title"], ["title"]])
        self.assertEqual([plan.position(step.step_id) for step in plan.steps], [1, 2, 3])
        # A required member is never defaulted: a step without it is refused.
        missing = {**document, "steps": [*steps, self._publish("fourth", count={"kind": "literal", "value": 4})]}
        with self.assertRaises(routine_plan.PlanError) as caught:
            routine_plan.admit(missing, CONTRACTS)
        self.assertEqual(caught.exception.code, "plan-input-mismatch")

    def test_a_plan_of_the_most_steps_is_admitted_and_one_more_is_refused(self) -> None:
        steps = [
            self._publish(f"s{index}", title={"kind": "literal", "value": f"Post {index}"})
            for index in range(routine_plan.MAX_STEPS)
        ]
        document = {**_document(steps=steps), "output": {"mode": "changes", "step": "s255", "when": None}}
        plan = routine_plan.admit(document, CONTRACTS)
        self.assertEqual((len(plan.steps), plan.position("s255"), plan.shown().step_id), (256, 256, "s255"))
        self.assertTrue(routine_plan.well_formed(document))
        over = {**document, "steps": [*steps, self._publish("s256", title={"kind": "literal", "value": "x"})]}
        self.assertFalse(routine_plan.well_formed(over))

    def test_a_resolved_input_over_its_bound_is_refused_before_any_dispatch(self) -> None:
        step = self._publish("only", title={"kind": "step_output", "step": "earlier", "pointer": ""})
        plan = routine_plan.admit(
            {
                **_document(steps=[self._publish("earlier", title={"kind": "literal", "value": "x"}), step]),
                "output": {"mode": "none", "step": None, "when": None},
            },
            {
                ("shimpz-blog", "publish-post"): routine_plan.ActionContract(
                    PIN, {"type": "object", "properties": {"title": {"type": "string"}}}
                )
            },
        )
        validated: list[object] = []
        large = "x" * routine_plan.MAX_RESOLVED_INPUT_BYTES
        with self.assertRaises(routine_plan.PlanError) as caught:
            routine_plan.resolve(plan, plan.steps[1], {("earlier", "", "", ""): large}, 0, validated.append)
        self.assertEqual((caught.exception.code, validated), ("plan-input-too-large", []))
        fits = "x" * (routine_plan.MAX_RESOLVED_INPUT_BYTES - 64)
        resolved = routine_plan.resolve(plan, plan.steps[1], {("earlier", "", "", ""): fits}, 0, validated.append)
        self.assertEqual(resolved, {"title": fits})


# The output schema of the step an input copies its value from: one member secret by name, one inside an object.
SOURCE = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "token": {"type": "string"},
        "meta": {"type": "object", "properties": {"api_key": {"type": "string"}, "label": {"type": "string"}}},
    },
}


class InputPreviewTests(unittest.TestCase):
    """What a run's step record shows of the inputs one attempt was given: redacted before any cut (scale)."""

    def plan(self, inputs: dict[str, object], source: dict[str, object] = SOURCE, share=SHARE) -> routine_plan.Plan:
        contracts = {
            ("shimpz-blog", "publish-post"): routine_plan.ActionContract(PIN, PUBLISH, (), source),
            ("shimpz-blog", "share-post"): routine_plan.ActionContract(OTHER_PIN, share),
        }
        steps = [
            {
                "id": "publish",
                "assistant": "shimpz-blog",
                "action": "publish-post",
                "pin": PIN,
                "input": {
                    "title": {"kind": "literal", "value": "Weekly report"},
                },
            },
            {"id": "share", "assistant": "shimpz-blog", "action": "share-post", "pin": OTHER_PIN, "input": inputs},
        ]
        output = {"mode": "none", "step": None, "when": None}
        return routine_plan.admit({**_document(steps=steps), "output": output}, contracts)

    def preview(self, inputs, resolved, selected, protected=(), source=SOURCE, share=SHARE) -> list[dict[str, object]]:
        plan = self.plan(inputs, source, share)
        return routine_plan.input_preview(plan, plan.steps[1], resolved, selected, protected)

    def test_a_value_copied_from_a_secret_source_position_is_withheld_whole(self) -> None:
        for pointer in ("/token", "/meta/api_key"):
            inputs = {"post_id": {"kind": "step_output", "step": "publish", "pointer": pointer}}
            with self.subTest(pointer=pointer):
                shown = self.preview(inputs, {"post_id": "s3cr3t"}, {("publish", pointer, "", ""): "s3cr3t"})
                self.assertEqual(shown, [{"member": "post_id", "source": "step_output", "value": None}])

    def test_a_secret_root_or_a_dependent_schema_on_the_way_withholds_the_copied_value(self) -> None:
        note = {"type": "object", "properties": {"note": {"type": "string"}}}
        sources = (
            {**SOURCE, "writeOnly": True},
            {
                "type": "object",
                "properties": {
                    "meta": {**note, "dependentSchemas": {"flag": {"properties": {"note": {"writeOnly": True}}}}}
                },
            },
            {"type": "object", "dependentSchemas": {"flag": {"properties": {"meta": {"format": "password"}}}}},
        )
        for source in sources:
            inputs = {"post_id": {"kind": "step_output", "step": "publish", "pointer": "/meta/note"}}
            with self.subTest(source=source):
                selected = {("publish", "/meta/note", "", ""): "private"}
                shown = self.preview(inputs, {"post_id": "private"}, selected, source=source)
                self.assertEqual(shown, [{"member": "post_id", "source": "step_output", "value": None}])

    def test_a_schema_secret_is_redacted_by_its_original_key_before_any_key_is_renamed(self) -> None:
        meta = {
            "type": "object",
            "properties": {"entry": {"type": "string", "writeOnly": True}, "flag": {"type": "string"}},
            "dependentSchemas": {"flag": {"properties": {"other": {"format": "password"}}}},
        }
        source = {"type": "object", "properties": {"meta": meta}}
        value = {"entry": "private-ordinary-value", "flag": "on", "other": "second-private-value"}
        inputs = {"post_id": {"kind": "step_output", "step": "publish", "pointer": "/meta"}}
        for protected in ((), ("entry",), ("other", "flag")):
            with self.subTest(protected=protected):
                shown = self.preview(
                    inputs, {"post_id": value}, {("publish", "/meta", "", ""): value}, protected, source
                )
                self.assertNotIn("private", shown[0]["value"])

    def test_no_key_is_renamed_before_the_destination_schema_is_walked(self) -> None:
        meta = {
            "type": "object",
            "properties": {"api_key": {"type": "string"}, "other": {"type": "string"}},
            "dependentSchemas": {"api_key": {"properties": {"other": {"writeOnly": True}}}},
        }
        share = {**SHARE, "properties": {**SHARE["properties"], "meta": meta}}
        value = {"api_key": "k-1", "other": "private-other-value"}
        inputs = {
            "meta": {"kind": "step_output", "step": "publish", "pointer": "/meta"},
            "post_id": {"kind": "literal", "value": "p-1"},
        }
        for protected in ((), ("api_key",)):
            with self.subTest(protected=protected):
                selected = {("publish", "/meta", "", ""): value}
                shown = self.preview(inputs, {"meta": value, "post_id": "p-1"}, selected, protected, share=share)
                self.assertNotIn("private", shown[0]["value"])
                self.assertNotIn("k-1", shown[0]["value"])

    def test_a_key_holding_an_injected_value_is_renamed_without_colliding(self) -> None:
        injected = 'quote"and\ncontrol'
        value = {f"k {injected}": "v", "[redacted] 0": "kept", "plain": "ok"}
        inputs = {"post_id": {"kind": "step_output", "step": "publish", "pointer": "/id"}}
        shown = self.preview(inputs, {"post_id": value}, {("publish", "/id", "", ""): value}, (injected,))
        self.assertEqual(shown[0]["value"], '{"[redacted] 0":"kept","[redacted] 1":"v","plain":"ok"}')

    def test_secret_positions_inside_a_copied_value_and_at_its_destination_are_redacted(self) -> None:
        meta = {"api_key": "k-123", "label": "public"}
        inputs = {
            "post_id": {"kind": "step_output", "step": "publish", "pointer": "/meta"},
            "tags": {"kind": "literal", "value": ["a"]},
        }
        shown = self.preview(inputs, {"post_id": meta, "tags": ["a"]}, {("publish", "/meta", "", ""): meta})
        self.assertEqual(shown[0]["value"], '{"api_key":"[redacted]","label":"public"}')
        self.assertEqual(shown[1], {"member": "tags", "source": "literal", "value": '["a"]'})
        destination = {
            "post_id": {"kind": "literal", "value": "p-1"},
            "secret_note": {"kind": "step_output", "step": "publish", "pointer": "/id"},
        }
        shown = self.preview(
            destination, {"post_id": "p-1", "secret_note": "plain"}, {("publish", "/id", "", ""): "plain"}
        )
        self.assertEqual(shown[1], {"member": "secret_note", "source": "step_output", "value": '"[redacted]"'})

    def test_credentials_and_injected_values_are_redacted_before_any_cut(self) -> None:
        credential = "sk-" + "a" * 40
        injected = 'quote"and\ncontrol'
        value = {"label": f"see {injected} here", "nested": [credential], credential: "x", "ok": "fine"}
        inputs = {"post_id": {"kind": "step_output", "step": "publish", "pointer": "/id"}}
        shown = self.preview(inputs, {"post_id": value}, {("publish", "/id", "", ""): value}, (injected,))
        text = shown[0]["value"]
        self.assertNotIn(credential[:12], text)
        self.assertNotIn("quote", text)
        self.assertIn('"ok":"fine"', text)
        self.assertLessEqual(len(text), 120)

    def test_an_unreadable_value_is_withheld_and_a_clock_shows_its_rendered_value(self) -> None:
        inputs = {
            "post_id": {"kind": "step_output", "step": "publish", "pointer": "/id"},
            "title": {"kind": "run_clock", "format": "date"},
        }
        share = {**SHARE, "properties": {**SHARE["properties"], "title": {"type": "string"}}}
        shown = self.preview(inputs, {"post_id": "x", "title": "2026-10-05"}, {}, share=share)
        self.assertEqual(
            shown,
            [
                {"member": "post_id", "source": "step_output", "value": None},
                {"member": "title", "source": "run_clock", "value": '"2026-10-05"'},
            ],
        )

    def test_a_value_nested_past_the_depth_bound_is_redacted_whole(self) -> None:
        deep: object = "leaf"
        for _depth in range(routine_plan.MAX_SAFE_OUTPUT_DEPTH + 2):
            deep = [deep]
        inputs = {"post_id": {"kind": "step_output", "step": "publish", "pointer": "/id"}}
        shown = self.preview(inputs, {"post_id": deep}, {("publish", "/id", "", ""): deep})
        self.assertIn("[redacted]", shown[0]["value"])
        self.assertNotIn("leaf", shown[0]["value"])


ZONES_OUT = {
    "type": "object",
    "properties": {
        "result": {
            "type": "array",
            "prefixItems": [{"type": "object"}],
            "items": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
        }
    },
}


class SelectorAndDispositionTests(unittest.TestCase):
    """A reference through one array item's named member, and what a recorded plan does with its result (ADR-0101)."""

    def plan(self, source: dict[str, object], output: dict[str, object] | None = None, out=ZONES_OUT):
        contracts = {
            ("shimpz-blog", "publish-post"): routine_plan.ActionContract(PIN, PUBLISH, (), out),
            ("shimpz-blog", "share-post"): routine_plan.ActionContract(OTHER_PIN, SHARE),
        }
        steps = [
            {
                "id": "publish",
                "assistant": "shimpz-blog",
                "action": "publish-post",
                "pin": PIN,
                "input": {"title": {"kind": "literal", "value": "x"}},
            },
            {"id": "share", "assistant": "shimpz-blog", "action": "share-post", "pin": OTHER_PIN, "input": source},
        ]
        output = output or {"mode": "show", "step": "share", "when": None}
        return routine_plan.admit({**_document(steps=steps), "output": output}, contracts)

    def selector(self, **changes: object) -> dict[str, object]:
        return {
            "post_id": {
                "kind": "step_output",
                "step": "publish",
                "pointer": "/result",
                "where": {"name": "shimpz.com"},
                "item": "/id",
                **changes,
            }
        }

    def test_a_selector_is_admitted_keyed_whole_and_resolved_through_its_one_item(self) -> None:
        plan = self.plan(self.selector())
        key = ("/result", '{"name":"shimpz.com"}', "/id")
        self.assertEqual(plan.references("publish"), (key,))
        result = {"result": [{"name": "other.org", "id": "z1"}, {"name": "shimpz.com", "id": "z2"}]}
        chosen = routine_plan.selections(plan, "publish", result)
        self.assertEqual(chosen, {key: "z2"})
        resolved = routine_plan.resolve(plan, plan.steps[1], {("publish", *key): "z2"}, 0, _validator(SHARE))
        self.assertEqual(resolved, {"post_id": "z2"})
        with self.assertRaises(routine_plan.PlanError) as ambiguous:
            routine_plan.selections(plan, "publish", {"result": [{"name": "shimpz.com", "id": "a"}] * 2})
        self.assertEqual(ambiguous.exception.code, "plan-reference-ambiguous")

    def test_a_selector_out_of_its_closed_shape_is_refused(self) -> None:
        for changes in (
            {"where": {"name": True}},
            {"where": {"a": "x", "b": "y"}},
            {"where": {1: "x"}},
            {"where": {"name": 1.5}},
            {"item": "id"},
        ):
            with self.subTest(changes=changes), self.assertRaises(routine_plan.PlanError) as caught:
                self.plan(self.selector(**changes))
            self.assertEqual(caught.exception.code, "plan-reference-invalid")
        without_item = dict(self.selector()["post_id"])
        del without_item["item"]
        with self.assertRaises(routine_plan.PlanError) as caught:
            self.plan({"post_id": without_item})
        self.assertEqual(caught.exception.code, "plan-input-invalid")

    def test_a_selected_value_is_previewed_under_every_schema_any_item_may_have(self) -> None:
        plan = self.plan(self.selector())
        key = ("publish", "/result", '{"name":"shimpz.com"}', "/id")
        shown = routine_plan.input_preview(plan, plan.steps[1], {"post_id": "z2"}, {key: "z2"}, ())
        self.assertEqual(shown, [{"member": "post_id", "source": "step_output", "value": '"z2"'}])
        secret = {
            "type": "object",
            "properties": {"result": {"type": "array", "items": {"type": "object", "writeOnly": True}}},
        }
        withheld = self.plan(self.selector(), out=secret)
        shown = routine_plan.input_preview(withheld, withheld.steps[1], {"post_id": "z2"}, {key: "z2"}, ())
        self.assertEqual(shown, [{"member": "post_id", "source": "step_output", "value": None}])

    def test_only_a_decision_has_a_condition_and_may_run_no_step(self) -> None:
        decide = {"mode": "decide", "step": None, "when": "changes"}
        self.assertIsNone(self.plan(self.selector(), decide).shown())
        empty = {**_document(steps=[]), "output": {"mode": "decide", "step": None, "when": "always"}}
        self.assertEqual(routine_plan.admit(empty, CONTRACTS).steps, ())
        self.assertTrue(routine_plan.well_formed(empty))
        for output in (
            {"mode": "decide", "step": None, "when": None},
            {"mode": "decide", "step": "share", "when": "always"},
            {"mode": "show", "step": "share", "when": "always"},
            {"mode": "none", "step": None, "when": "changes"},
            {"mode": "chain", "step": None, "when": None},
            {"mode": "show", "step": "share"},
        ):
            with self.subTest(output=output), self.assertRaises(routine_plan.PlanError) as caught:
                self.plan(self.selector(), output)
            self.assertEqual(caught.exception.code, "plan-output-invalid")
        self.assertFalse(routine_plan.well_formed({**empty, "output": {"mode": "none", "step": None, "when": None}}))

    def test_a_shown_result_redacts_every_value_and_key_the_run_protects(self) -> None:
        node = routine_plan.output_safe(
            {"note": "has tok-1 inside", "tok-1": "x", "ok": "fine"}, {}, frozenset({"tok-1"})
        )
        fields = dict(node["fields"])
        self.assertEqual(fields["note"], routine_plan.OUTPUT_REDACTED)
        self.assertEqual(fields["[redacted]"], routine_plan.OUTPUT_REDACTED)
        self.assertEqual(fields["ok"], {"kind": "text", "value": "fine", "cut": False})
        self.assertEqual(routine_plan.output_safe("plain", {}, frozenset({""}))["kind"], "text")


class IndexedPreviewTests(unittest.TestCase):
    def test_a_pointer_through_an_index_and_scalars_beside_injected_values_preview_safely(self) -> None:
        source = {"type": "object", "properties": {"zones": {"type": "array", "prefixItems": [{"type": "object"}]}}}
        contracts = {
            ("shimpz-blog", "publish-post"): routine_plan.ActionContract(PIN, PUBLISH, (), source),
            ("shimpz-blog", "share-post"): routine_plan.ActionContract(OTHER_PIN, SHARE),
        }
        steps = [
            {
                "id": "publish",
                "assistant": "shimpz-blog",
                "action": "publish-post",
                "pin": PIN,
                "input": {"title": {"kind": "literal", "value": "x"}},
            },
            {
                "id": "share",
                "assistant": "shimpz-blog",
                "action": "share-post",
                "pin": OTHER_PIN,
                "input": {"post_id": {"kind": "step_output", "step": "publish", "pointer": "/zones/0"}},
            },
        ]
        output = {"mode": "none", "step": None, "when": None}
        plan = routine_plan.admit({**_document(steps=steps), "output": output}, contracts)
        value = {"id": 7, "flag": True, "note": None}
        key = ("publish", "/zones/0", "", "")
        shown = routine_plan.input_preview(plan, plan.steps[1], {"post_id": value}, {key: value}, ("secret",))
        self.assertEqual(shown[0]["value"], '{"flag":true,"id":7,"note":null}')


if __name__ == "__main__":
    unittest.main()
