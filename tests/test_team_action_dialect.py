"""The Assistant protocol's pattern dialect, schema admission, and payload validation vectors on Team's Python."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jsonschema.exceptions import ValidationError

from assistant import action_schema
from assistant import manifest as assistant_manifest
from protocol.action.v1 import pattern as dialect
from protocol.action.v1 import schema as action_protocol
from protocol.http.v1 import strict_json

VECTORS = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1" / "vectors"


def _cases(name: str) -> list[dict]:
    vectors = json.loads((VECTORS / name).read_bytes())
    assert vectors["version"] == 1
    return vectors["cases"]


def _outcome(schema: dict, payload: object) -> tuple[str, int]:
    """Team's validation of one payload, with the matching work its searches charged."""
    validator = action_schema.payload_validator(schema)
    if not isinstance(payload, dict) or not action_schema.json_depth_within(payload, action_schema.MAX_PAYLOAD_DEPTH):
        with unittest.TestCase().assertRaises(ValueError):
            assistant_manifest.validate_schema_payload(validator, payload)
        return "shape", 0
    with action_protocol.pattern_work_budget():
        try:
            validator.validate(payload)
            verdict = "valid"
        except ValidationError:
            verdict = "invalid"
        except action_protocol.PatternError as error:
            verdict = "arithmetic" if "arithmetic" in str(error) else "exhausted"
        charged = action_protocol.MAX_PATTERN_WORK - action_protocol._pattern_work.get().remaining
    return verdict, charged


class DialectVectorTests(unittest.TestCase):
    def test_every_pattern_program_is_as_published(self) -> None:
        for case in _cases("pattern-program.json"):
            with self.subTest(case=case["name"]):
                self.assertEqual(dialect.pattern_program(case["pattern"]), case["program"])
                self.assertEqual(dialect.pattern_preparation(case["pattern"]), case["preparation"])
                self.assertEqual(
                    dialect.pattern_costs(case["pattern"]),
                    None if case["program"] is None else (case["program"], case["preparation"]),
                )
                self.assertIs(dialect.pattern_admitted(case["pattern"]), case["program"] is not None)

    def test_every_schema_vector_is_admitted_as_published(self) -> None:
        for case in _cases("action-schema.json"):
            schema = strict_json.loads(json.dumps(case["schema"]))
            with self.subTest(case=case["name"]):
                try:
                    action_schema.admitted(schema)
                    admitted = True
                except action_schema.ActionSchemaError:
                    admitted = False
                self.assertIs(admitted, case["valid"])

    def test_every_payload_vector_validates_and_charges_as_published(self) -> None:
        for case in _cases("payload.json"):
            schema = action_schema.admitted(strict_json.loads(json.dumps(case["schema"])))
            payload = strict_json.loads(json.dumps(case["payload"]))
            with self.subTest(case=case["name"]):
                self.assertEqual(_outcome(schema, payload), (case["outcome"], case["charged"]))
                if case["outcome"] == "valid":
                    validator = action_schema.payload_validator(schema)
                    self.assertEqual(assistant_manifest.validate_schema_payload(validator, payload), payload)


class DialectConstructTests(unittest.TestCase):
    def test_constructs_refused_at_each_step_of_the_parse(self) -> None:
        for pattern in (
            "\ud800",
            "(?P<a",
            "(?P<1a>x)",
            "(?P<a-b>x)",
            "(?i",
            "(?ii)a",
            "a{2x}",
            "(?im)a(?s)",
            "a{2,1}",
            "a{4294967296}",
            "a{,2}",
            "a{2,3",
            "\\",
            "\\xG1",
            "\\x4",
            "[a",
            "[^",
            "[a-",
            "[a-\\d]",
            "[\\d-a]",
            "[b-a]",
            "[a-b]{0}{1}",
            "(a))",
            "|*",
            "(?i)*",
            "^*",
            "\\b{start}",
            "\\B{2}",
            "[\\b]",
            "[\\A]",
            "[a&&b]",
            "[a~~b]",
            "[[:alpha:]]",
        ):
            with self.subTest(pattern=pattern):
                self.assertIsNone(dialect.pattern_program(pattern))

    def test_constructs_admitted_with_their_bound(self) -> None:
        for pattern, bound in (
            ("(?i)|a", 16 + 2 + 0 + 8),
            ("(?:)*", 16 + 1 + 2 + 2),
            ("()", 16 + 1 + 2),
            ("[]]", 16 + 36),
            ("[^]a]", 16 + 40),
            ("[a-z-]", 16 + 40),
            ("[\\-a]", 16 + 40),
            ("[!-[]", 16 + 36),
            ("a{1000,}", 16 + 1000 * 9 + 1),
            ("a{0,1000}?", 16 + 1000 * 9 + 1),
            ("\\x00", 16 + 8),
            ("(?P<_a>b)(?P<c1>d)", 16 + 10 + 10),
            ("a|(b|c)", 16 + 2 + 8 + (2 + 2 + 16)),
        ):
            with self.subTest(pattern=pattern):
                self.assertEqual(dialect.pattern_program(pattern), bound)

    def test_a_pattern_outside_the_dialect_is_refused_before_any_charge(self) -> None:
        with action_protocol.pattern_work_budget():
            with self.assertRaisesRegex(action_protocol.PatternError, "linear-time matcher"):
                action_protocol.pattern_matches("\\p{L}", "a")
            self.assertEqual(action_protocol._pattern_work.get().remaining, action_protocol.MAX_PATTERN_WORK)

    def test_a_preparation_beyond_the_budget_is_refused_before_the_pattern_compiles(self) -> None:
        with (
            action_protocol.pattern_work_budget(),
            mock.patch.object(action_protocol.re2, "compile", wraps=action_protocol.re2.compile) as compile_,
        ):
            spent = (action_protocol.MAX_PATTERN_WORK - dialect.pattern_preparation("a")) // 24
            self.assertTrue(action_protocol.pattern_matches("a", "a" * spent))
            with self.assertRaisesRegex(action_protocol.PatternError, "work budget"):
                action_protocol.pattern_matches("b", "b")
            self.assertEqual(compile_.call_count, 1)

    def test_a_pattern_re2_cannot_compile_fails_closed(self) -> None:
        with (
            mock.patch.object(action_protocol.re2, "compile", side_effect=action_protocol.re2.error("refused")),
            self.assertRaisesRegex(action_protocol.PatternError, "linear-time matcher"),
        ):
            action_protocol.pattern_matches("a", "a")

    def test_unique_items_and_multiple_of_refuse_what_they_cannot_decide(self) -> None:
        unique = action_schema.payload_validator({"uniqueItems": True})
        self.assertFalse(unique.is_valid([[1], [True], [1.0]]))
        self.assertTrue(unique.is_valid([{"a": 1}, {"a": True}, "1", None, False]))
        self.assertTrue(action_schema.payload_validator({"uniqueItems": False}).is_valid([1, 1]))
        multiple = action_schema.payload_validator({"multipleOf": 0.5})
        with self.assertRaisesRegex(action_protocol.PatternError, "arithmetic"):
            multiple.is_valid(10**400)
        self.assertTrue(multiple.is_valid(1.5))


if __name__ == "__main__":
    unittest.main()
