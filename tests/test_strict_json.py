"""Strict JSON decoder contracts."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol.http.v1 import strict_json


class StrictJsonTests(unittest.TestCase):
    def test_rejects_non_finite_numbers_and_duplicate_fields(self):
        for payload in (
            "NaN",
            '{"a": Infinity}',
            '{"a": -Infinity}',
            '{"a": 1e999}',
            '{"a": -1e999}',
            '{"a": 1, "a": 2}',
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                strict_json.loads(payload)

    def test_rejects_framing_that_the_rust_reader_refuses(self):
        for payload in (
            b"\xef\xbb\xbf{}",
            b"\xff",
            '"\\ud800"',
            '{"\\uDC00": 1}',
            '["\ud800"]',
            "[" * 513 + "]" * 513,
        ):
            with self.subTest(payload=payload[:16]), self.assertRaises(ValueError):
                strict_json.loads(payload)

    def test_accepts_paired_surrogates_and_counts_only_brackets_outside_strings(self):
        self.assertEqual(strict_json.loads(b'"\\ud83d\\ude00"'), "\U0001f600")
        self.assertEqual(strict_json.loads('["\\"[", "]]"]'), ['"[', "]]"])
        self.assertIsInstance(strict_json.loads("[" * 512 + "]" * 512), list)
        quoted = strict_json.loads("[" * 300 + '"' + "]" * 600 + '"' + "]" * 300)
        self.assertIsInstance(quoted, list)
        self.assertEqual(len(strict_json.loads("[" + "[]," * 600 + "[]]")), 601)
        self.assertEqual(strict_json.loads('["' + "[" * 600 + '"]'), ["[" * 600])

    def test_accepts_finite_unique_object(self):
        self.assertEqual(strict_json.loads('{"a": 1}'), {"a": 1})
        self.assertEqual(strict_json.loads('{"a": 1.5e3}'), {"a": 1500.0})
