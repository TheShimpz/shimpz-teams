"""A Routine's sealed creation source admits only its own exact canonical record (ADR-0092 amendment, 2026-10-02)."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from local import app as local_app
from local.routine import source as routine_source

ROUTINE_ID = "a" * 32
SOURCE = routine_source.Source(
    ROUTINE_ID, "network-1", (("said", "Every day at 9, list my zones"),), (("schedule",), {"k": 1})
)


def _record(**changes: object) -> bytes:
    value = json.loads(SOURCE.encode())
    value.update(changes)
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


class SourceDecodeTests(unittest.TestCase):
    def assert_unavailable(self, payload: bytes) -> None:
        with self.assertRaises(local_app.ApiProblem) as caught:
            routine_source.decode(payload, ROUTINE_ID)
        self.assertEqual(caught.exception.code, "routine-state-unavailable")

    def test_the_exact_canonical_record_round_trips(self) -> None:
        self.assertEqual(routine_source.decode(SOURCE.encode(), ROUTINE_ID), SOURCE)
        bare = routine_source.Source(ROUTINE_ID, "network-1", (("said", "x"),))
        self.assertEqual(routine_source.decode(bare.encode(), ROUTINE_ID), bare)
        # A source keeps every kinded part exactly, oldest first: earlier sends, a draft's words, and an answer.
        words = (("cited", "list my zones"), ("said", "do this every 30 seconds"), ("said", "Up to 100 a day"))
        kept = routine_source.Source(ROUTINE_ID, "network-1", words)
        self.assertEqual(routine_source.decode(kept.encode(), ROUTINE_ID), kept)
        self.assertNotEqual(kept.commitment, routine_source.Source(ROUTINE_ID, "network-1", words[1:]).commitment)
        # The longest words a Routine can hold still seal.
        longest = (*(("said", "x" * 4_000),) * 8, *(("cited", "y" * 2_000),) * 3, ("said", "z" * 16_000))
        self.assertEqual(
            routine_source.decode(routine_source.Source(ROUTINE_ID, "n", longest).encode(), ROUTINE_ID).parts, longest
        )

    def test_unreadable_foreign_or_malformed_records_fail_closed(self) -> None:
        for payload in (
            b"\xff",
            b"{",
            b"[]",
            _record(version=1),
            _record(extra=1),
            _record(routine_id="b" * 32),
            # The words are 1 to 13 kinded parts, the last said, each a bounded text without NUL, within the bound.
            _record(parts=[]),
            _record(parts="x"),
            _record(parts=[{"kind": "said", "text": ""}]),
            _record(parts=[{"kind": "said", "text": "a\u0000b"}]),
            _record(parts=[{"kind": "said", "text": "x" * 16_001}]),
            _record(parts=[{"kind": "cited", "text": "x"}]),
            _record(parts=[{"kind": "quoted", "text": "x"}]),
            _record(parts=[{"kind": "said"}]),
            _record(parts=[{"kind": "said", "text": 1}]),
            _record(parts=[{"kind": "said", "text": "x"}] * 14),
            _record(parts=[{"kind": "said", "text": "x" * 16_000}] * 4),
            _record(selected=["schedule"]),
            _record(selected={"field": ["schedule"]}),
            _record(selected={"field": ["weekday"], "value": 1}),
            _record(selected={"field": ["input", "zones", ""], "value": 1}),
            _record(selected={"field": "schedule", "value": 1}),
        ):
            with self.subTest(payload=payload[:60]):
                self.assert_unavailable(payload)

    def test_a_record_that_is_not_byte_for_byte_canonical_is_refused(self) -> None:
        value = json.loads(SOURCE.encode())
        for payload in (json.dumps(value, indent=1).encode(), SOURCE.encode() + b"\n"):
            with self.subTest(payload=payload[:20]):
                self.assert_unavailable(payload)


class FieldValueTests(unittest.TestCase):
    def test_each_question_field_reads_its_value_and_an_absent_step_reads_none(self) -> None:
        routine = SimpleNamespace(
            schedule={"kind": "daily", "time": "09:00"},
            timezone="UTC",
            plan={"steps": [{"id": "zones", "input": {"page": {"kind": "literal", "value": 1}}}]},
        )
        self.assertEqual(routine_source.field_value(routine, ("schedule",)), {"kind": "daily", "time": "09:00"})
        self.assertEqual(routine_source.field_value(routine, ("timezone",)), "UTC")
        self.assertEqual(
            routine_source.field_value(routine, ("input", "zones", "page")), {"kind": "literal", "value": 1}
        )
        self.assertIsNone(routine_source.field_value(routine, ("input", "zones", "per_page")))
        self.assertIsNone(routine_source.field_value(routine, ("input", "gone", "page")))


if __name__ == "__main__":
    unittest.main()
