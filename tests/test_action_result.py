import math
import unittest

from action import result as action_result


class ActionResultTests(unittest.TestCase):
    def test_only_bounded_json_values_are_admitted(self) -> None:
        for value in (math.inf, {1: "invalid"}, object()):
            with self.subTest(value=value), self.assertRaises(action_result.ActionResultError):
                action_result.walk(value)
        with self.assertRaisesRegex(action_result.ActionResultError, "structure"):
            action_result.walk(None, budget=[0])
        with self.assertRaisesRegex(action_result.ActionResultError, "structure"):
            action_result.walk(_nested(action_result.MAX_JSON_DEPTH + 1))
        action_result.walk({"list": [1, 1.5, True, None, "text"]})

    def test_canonical_bytes_are_sorted_compact_utf8_and_bounded(self) -> None:
        self.assertEqual(action_result.canonical({"b": "ã", "a": [1]}, 100), '{"a":[1],"b":"ã"}'.encode())
        with self.assertRaisesRegex(action_result.ActionResultError, "canonical"):
            action_result.canonical("\ud800", 100)
        with self.assertRaisesRegex(action_result.ActionResultError, "size"):
            action_result.canonical("x" * 10, 5)
        # The refusal is a ValueError, which every RPC result boundary already treats as an invalid result.
        self.assertTrue(issubclass(action_result.ActionResultError, ValueError))


def _nested(depth: int) -> object:
    value: object = []
    for _ in range(depth):
        value = [value]
    return value


if __name__ == "__main__":
    unittest.main()
