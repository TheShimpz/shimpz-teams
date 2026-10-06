"""Exact equality, the ``where`` item selector, and the whole-input secret check of a recorded plan (ADR-0101)."""

from __future__ import annotations

import unittest

from routine import plan as routine_plan

ZONES = {
    "result": [
        {"id": "a" * 32, "name": "example.com", "paused": False},
        {"id": "b" * 32, "name": "shimpz.com", "paused": True},
        {"id": "c" * 32, "name": "other.org", "rank": 7},
        "not-an-object",
        {"id": "d" * 32},
    ]
}


class SameTests(unittest.TestCase):
    def test_equality_is_type_sensitive_and_exact(self) -> None:
        same = routine_plan.same
        cases = [
            (True, 1, False),
            (1, True, False),
            (False, 0, False),
            (True, True, True),
            (1, 1, True),
            (12345678901234567890, 12345678901234567890, True),
            (12345678901234567890, 12345678901234567891, False),
            (2.0, 2, True),
            (2, 2.0, True),
            (2.5, 2, False),
            (float(2**60), 2**60 + 1, False),
            (1.5, 1.5, True),
            ("1", 1, False),
            (None, None, True),
            (None, False, False),
            ([1, "a"], [1, "a"], True),
            ([1, "a"], [1, "a", None], False),
            ([True], [1], False),
            ({"a": 1, "b": [2]}, {"b": [2.0], "a": 1}, True),
            ({"a": 1}, {"a": 1, "b": 2}, False),
            ({"a": 1}, {"b": 1}, False),
            ({"a": 1}, [1], False),
            ("x", "x", True),
        ]
        for left, right, expected in cases:
            with self.subTest(left=left, right=right):
                self.assertIs(same(left, right), expected)


class SelectWhereTests(unittest.TestCase):
    def test_exactly_one_matching_item_is_selected(self) -> None:
        chosen = routine_plan.select_where(ZONES, "/result", {"name": "shimpz.com"}, "/id")
        self.assertEqual(chosen, "b" * 32)
        self.assertEqual(routine_plan.select_where(ZONES, "/result", {"rank": 7}, ""), ZONES["result"][2])

    def test_two_selectors_on_one_array_pick_different_items(self) -> None:
        first = routine_plan.select_where(ZONES, "/result", {"name": "example.com"}, "/id")
        second = routine_plan.select_where(ZONES, "/result", {"name": "shimpz.com"}, "/id")
        self.assertEqual((first, second), ("a" * 32, "b" * 32))

    def test_zero_matches_and_non_matches_fail_closed(self) -> None:
        for where in ({"name": "absent.com"}, {"missing": "x"}, {"rank": "7"}, {"paused": 1}):
            with self.subTest(where=where):
                with self.assertRaises(routine_plan.PlanError) as raised:
                    routine_plan.select_where(ZONES, "/result", where, "/id")
                self.assertEqual(raised.exception.code, "plan-reference-missing")

    def test_several_matches_are_ambiguous(self) -> None:
        value = {"items": [{"name": "x", "id": 1}, {"name": "x", "id": 2}]}
        with self.assertRaises(routine_plan.PlanError) as raised:
            routine_plan.select_where(value, "/items", {"name": "x"}, "/id")
        self.assertEqual(raised.exception.code, "plan-reference-ambiguous")

    def test_pointer_must_select_an_array_and_item_must_resolve(self) -> None:
        for pointer, item in (("/result/0", "/id"), ("/absent", "/id"), ("", "/id"), ("/result", "/absent")):
            with self.subTest(pointer=pointer, item=item):
                with self.assertRaises(routine_plan.PlanError) as raised:
                    routine_plan.select_where(ZONES, pointer, {"name": "shimpz.com"}, item)
                self.assertEqual(raised.exception.code, "plan-reference-missing")

    def test_malformed_selectors_are_invalid(self) -> None:
        for pointer, where, item in (
            ("/result", {}, "/id"),
            ("/result", {"name": "a", "id": "b"}, "/id"),
            ("/result", {"paused": True}, "/id"),
            ("/result", {"rank": 7.0}, "/id"),
            ("/result", {"name": None}, "/id"),
            ("/result", ["name"], "/id"),
            ("/result", {"name": "shimpz.com"}, "id"),
            ("result", {"name": "shimpz.com"}, "/id"),
        ):
            with self.subTest(pointer=pointer, where=where, item=item):
                with self.assertRaises(routine_plan.PlanError) as raised:
                    routine_plan.select_where(ZONES, pointer, where, item)
                self.assertEqual(raised.exception.code, "plan-reference-invalid")

    def test_selected_value_is_a_copy(self) -> None:
        chosen = routine_plan.select_where(ZONES, "/result", {"rank": 7}, "")
        chosen["id"] = "changed"
        self.assertEqual(ZONES["result"][2]["id"], "c" * 32)


class SecretLiteralTests(unittest.TestCase):
    def test_whole_input_context_activates_dependent_schemas(self) -> None:
        schema = {
            "type": "object",
            "properties": {"mode": {"type": "string"}, "value": {"type": "string"}},
            "dependentSchemas": {"mode": {"properties": {"value": {"writeOnly": True}}}},
        }
        self.assertTrue(routine_plan.secret_literal(schema, {"mode": "x", "value": "y"}))
        self.assertFalse(routine_plan.secret_literal(schema, {"value": "y"}))
        self.assertTrue(routine_plan.secret_literal({"type": "object"}, {"api_token": "y"}))
        self.assertFalse(routine_plan.secret_literal({"type": "object"}, {"zone_id": "y"}))


if __name__ == "__main__":
    unittest.main()
