"""Action schema patterns run on the bounded linear-time matcher on every Team validation path."""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

from assistant import action_schema
from assistant import manifest as assistant_manifest
from assistant import spec as assistant_spec

assistants = harness.hosted_assistants
assistant_registry = assistants.assistant_registry
PATTERN_VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1" / "pattern-vectors.json"
# Python's backtracking `re` needs seconds for 30 characters of this pattern and doubles with each one more.
CATASTROPHIC = "^(a+)+b$"
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


def _closed(properties: dict[str, object] | None = None, **keywords: object) -> dict[str, object]:
    schema: dict[str, object] = {"type": "object", "additionalProperties": False, **keywords}
    if properties is not None:
        schema["properties"] = properties
    return schema


def _admitted(schema: dict[str, object]) -> dict[str, object]:
    return assistant_manifest._machine_schema(schema, kind="input")


def _validate(schema: dict[str, object], payload: object) -> dict[str, object]:
    return assistant_manifest.validate_schema_payload(assistant_manifest.action_schema_validator(schema), payload)


class PatternSemanticsTests(unittest.TestCase):
    def test_matches_every_published_pattern_vector(self) -> None:
        vectors = json.loads(PATTERN_VECTORS.read_bytes())
        self.assertEqual(vectors["version"], 1)
        for case in vectors["cases"]:
            with self.subTest(case=case["name"]):
                self.assertIs(action_schema.pattern_matches(case["pattern"], case["subject"]), case["matches"])
                for schema, payload in (
                    (_closed({"value": {"type": "string", "pattern": case["pattern"]}}), {"value": case["subject"]}),
                    (_closed(patternProperties={case["pattern"]: {"type": "integer"}}), {case["subject"]: 1}),
                ):
                    valid = True
                    try:
                        _validate(_admitted(schema), payload)
                    except ValueError:
                        valid = False
                    self.assertIs(valid, case["matches"])

    def test_refuses_patterns_outside_the_bounded_matcher_at_admission(self) -> None:
        for pattern in ("\\u0041", "(?x)a b", "a{1001}", "(?:a{100}){11}", "a" * 20_000, "\ud800"):
            for schema in (
                _closed({"value": {"type": "string", "pattern": pattern}}),
                _closed(patternProperties={pattern: {"type": "string"}}),
            ):
                with (
                    self.subTest(pattern=pattern[:16], schema=sorted(schema)),
                    self.assertRaisesRegex(assistant_manifest.ManifestError, "linear-time matcher admits"),
                ):
                    _admitted(schema)
        self.assertEqual(_admitted(_closed({"value": {"pattern": "a" * 16_000}}))["type"], "object")

    def test_refuses_unevaluated_properties_whose_evaluation_would_bypass_the_matcher(self) -> None:
        for schema in (
            _closed(unevaluatedProperties=False),
            _closed({"value": {"allOf": [{"unevaluatedProperties": False}]}}),
        ):
            with self.subTest(schema=schema), self.assertRaisesRegex(assistant_manifest.ManifestError, "unevaluated"):
                _admitted(schema)
        with self.assertRaisesRegex(ValueError, "does not match its reviewed schema") as raised:
            _validate(_closed(unevaluatedProperties=False), {})
        self.assertIsInstance(raised.exception.__cause__, action_schema.PatternError)

    def test_a_subject_that_is_not_unicode_fails_closed_even_under_not(self) -> None:
        schema = _admitted(_closed({"value": {"not": {"pattern": "secret"}}}))
        self.assertEqual(_validate(schema, {"value": "public"}), {"value": "public"})
        with self.assertRaisesRegex(ValueError, "does not match its reviewed schema") as raised:
            _validate(schema, {"value": "secret\ud800"})
        self.assertIsInstance(raised.exception.__cause__, action_schema.PatternError)

    def test_a_declared_dialect_never_switches_to_the_backtracking_validator(self) -> None:
        nested = _admitted(
            _closed({"value": {"anyOf": [{"$schema": DRAFT_2020_12, "type": "string", "pattern": "^a$"}]}})
        )
        rooted = _admitted(
            _closed(
                {"value": {"type": "string", "pattern": "^a$"}, "self": {"$ref": "#"}},
                **{"$schema": DRAFT_2020_12, "default": {"$schema": "data"}},
            )
        )
        for schema, payload in ((nested, {"value": "a\n"}), (rooted, {"self": {"value": "a\n"}})):
            # Python `re` would accept the trailing newline; RE2 matches `$` only at the end of the text.
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _validate(schema, payload)
        self.assertEqual(_validate(rooted, {"self": {"value": "a"}}), {"self": {"value": "a"}})
        self.assertEqual(rooted["default"], {"$schema": "data"})

    def test_additional_properties_consult_each_pattern_property_separately(self) -> None:
        schema = _admitted(
            _closed(
                {"id": {"type": "string"}},
                patternProperties={"^x-[a-z]+$": {"type": "string"}, "(?i)^y$": {"type": "integer"}},
            )
        )
        self.assertEqual(_validate(schema, {"id": "1", "x-a": "b", "Y": 2}), {"id": "1", "x-a": "b", "Y": 2})
        for payload in ({"x-a": 1}, {"x-1": "b"}, {"Y": "2"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _validate(schema, payload)

        typed = action_schema.payload_validator(
            {"type": "object", "patternProperties": {"^x": {}}, "additionalProperties": {"type": "integer"}}
        )
        self.assertTrue(typed.is_valid({"x": "any", "other": 1}))
        self.assertFalse(typed.is_valid({"other": "text"}))
        self.assertTrue(action_schema.payload_validator({"additionalProperties": True}).is_valid({"other": 1}))
        self.assertTrue(action_schema.payload_validator({"additionalProperties": False}).is_valid([1]))
        self.assertTrue(action_schema.payload_validator({"patternProperties": {"^x": False}}).is_valid([1]))
        self.assertTrue(action_schema.payload_validator({"pattern": "^x"}).is_valid(1))


class LinearTimeValidationPathTests(unittest.TestCase):
    INPUT = _admitted(
        _closed({"value": {"type": "string", "pattern": CATASTROPHIC}}, patternProperties={CATASTROPHIC: {}})
    )
    OUTPUT = _admitted(_closed({"items": {"type": "array", "items": {"type": "string", "pattern": CATASTROPHIC}}}))
    NEAR_MISS = "a" * 100_000

    def assert_fast_refusal(self, validate, payload: object) -> None:
        started = time.perf_counter()
        with self.assertRaisesRegex(ValueError, "does not match its reviewed schema"):
            validate(payload)
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_local_input_and_output_validation_stay_linear(self) -> None:
        action = assistant_spec.ActionSpec("Run", self.INPUT, self.OUTPUT)
        for direction, payload in (
            ("input", {"value": "a" * 30}),
            ("input", {"value": self.NEAR_MISS}),
            ("input", {"a" * 30: 1}),
            ("output", {"items": ["a" * 30, self.NEAR_MISS]}),
        ):
            with self.subTest(direction=direction):
                self.assert_fast_refusal(
                    lambda value, direction=direction: assistant_spec.validate_action_payload(action, direction, value),
                    payload,
                )
        self.assertEqual(
            assistant_spec.validate_action_payload(action, "output", {"items": ["aab"]}), {"items": ["aab"]}
        )

    def test_hosted_input_and_output_validation_stay_linear(self) -> None:
        contract = assistant_registry.AssistantContract(
            name="Assistant",
            actions={"run": assistant_registry.ActionSpec("Run", self.INPUT, self.OUTPUT)},
            integrations={},
        )
        for output, payload in (
            (False, {"value": "a" * 30}),
            (False, {"a" * 30: 1}),
            (True, {"items": [self.NEAR_MISS]}),
        ):
            with self.subTest(output=output):
                self.assert_fast_refusal(
                    lambda value, output=output: assistants._validate_action_payload(
                        contract, "run", value, output=output
                    ),
                    payload,
                )


if __name__ == "__main__":
    unittest.main()
