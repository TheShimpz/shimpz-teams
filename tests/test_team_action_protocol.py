"""Integrity gate for the producer-owned Team Action protocol that the Brain mirrors."""

import json
import unittest
from pathlib import Path
from unittest import mock

from protocol_manifest import assert_manifest_holds

from assistant import action_schema
from protocol.action.v1 import schema as action_protocol

PROTOCOL = Path(__file__).resolve().parents[1] / "protocol" / "action" / "v1"
ADMISSION_VECTORS = PROTOCOL.parents[1] / "assistant" / "v1" / "vectors" / "action-schema.json"
CLOSED = {"type": "object", "additionalProperties": False}


class TeamActionProtocolTests(unittest.TestCase):
    def test_manifest_covers_and_digests_every_produced_artifact(self) -> None:
        assert_manifest_holds(self, PROTOCOL)

    def test_team_admission_applies_the_protocol_bounds_and_matcher(self) -> None:
        self.assertEqual((action_schema.MAX_NODES, action_schema.MAX_BYTES), (4096, 128 * 1024))
        self.assertEqual(
            (action_schema.MAX_NODES, action_schema.MAX_BYTES), (action_protocol.MAX_NODES, action_protocol.MAX_BYTES)
        )
        self.assertIs(action_schema.pattern_matches, action_protocol.pattern_matches)

    def test_the_admission_predicate_decides_every_developers_vector(self) -> None:
        # The Brain applies this predicate itself, so it must decide every case Developers publication pins.
        for case in json.loads(ADMISSION_VECTORS.read_bytes())["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(action_protocol.admission_problem(case["schema"]) is None, case["valid"])

    def test_the_admission_predicate_names_the_first_rule_broken(self) -> None:
        for schema, problem in (
            ([], "must describe an object"),
            ({"type": "string"}, "must describe an object"),
            ({"type": "object"}, "must close every object"),
            ({**CLOSED, "properties": {"v": {"type": "object"}}}, "must close every object"),
            ({**CLOSED, "properties": {"v": {"required": ["x"]}}}, "must close every object"),
            ({**CLOSED, "properties": {"v": {"type": ["string", "object"]}}}, "must close every object"),
            ({**CLOSED, "properties": {"v": True}}, "must not use a boolean subschema"),
            ({**CLOSED, "properties": {"v": {"type": "string", "pattern": "(?=a)"}}}, "must use only patterns"),
            ({**CLOSED, "required": "v"}, "is invalid"),
            ({**CLOSED, "const": float("nan")}, "is invalid"),
            ({**CLOSED, "const": object()}, "is invalid"),
            ({**CLOSED, "description": "x" * action_protocol.MAX_BYTES}, "is too large"),
            ({**CLOSED, "enum": list(range(action_protocol.MAX_NODES))}, "is too large"),
            ({**CLOSED, "$ref": "#"}, "must resolve every reference without a cycle"),
        ):
            with self.subTest(schema=str(schema)[:80]):
                self.assertTrue(action_protocol.admission_problem(schema).startswith(problem))
        closed = {**CLOSED, "properties": {"v": {"type": ["string", "null"]}}}
        self.assertIsNone(action_protocol.admission_problem(closed))

    def test_the_metaschema_verdict_is_kept_only_for_exact_plain_json(self) -> None:
        action_protocol._CHECKED.clear()
        check = action_protocol.Draft202012Validator.check_schema
        try:
            with mock.patch.object(action_protocol.Draft202012Validator, "check_schema", side_effect=check) as checked:
                schemas = [{**CLOSED, "title": f"s{index}"} for index in range(action_protocol.MAX_CHECKED_SCHEMAS)]
                for schema in schemas:
                    self.assertIsNone(action_protocol.admission_problem(schema))
                for schema in schemas:
                    self.assertIsNone(action_protocol.admission_problem(json.loads(json.dumps(schema))))
                self.assertEqual(checked.call_count, action_protocol.MAX_CHECKED_SCHEMAS)
                # One more schema drops the oldest verdict, which is then checked again.
                self.assertIsNone(action_protocol.admission_problem({**CLOSED, "title": "one more"}))
                self.assertIsNone(action_protocol.admission_problem(schemas[0]))
                self.assertEqual(checked.call_count, action_protocol.MAX_CHECKED_SCHEMAS + 2)
                # A tuple has no exact JSON form: it is checked as it is, every time, and refused.
                for _ in range(2):
                    self.assertEqual(action_protocol.admission_problem({**CLOSED, "required": ("v",)}), "is invalid")
                self.assertEqual(checked.call_count, action_protocol.MAX_CHECKED_SCHEMAS + 4)
        finally:
            action_protocol._CHECKED.clear()


if __name__ == "__main__":
    unittest.main()
