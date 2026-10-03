"""A Routine's sealed creation source admits only its own exact canonical record (ADR-0092 amendment, 2026-10-02)."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from local import app as local_app
from local.routine import source as routine_source

ROUTINE_ID = "a" * 32
SOURCE = routine_source.Source(ROUTINE_ID, "network-1", "Every day at 9, list my zones", (("schedule",), {"k": 1}))


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
        bare = routine_source.Source(ROUTINE_ID, "network-1", "x")
        self.assertEqual(routine_source.decode(bare.encode(), ROUTINE_ID), bare)

    def test_unreadable_foreign_or_malformed_records_fail_closed(self) -> None:
        for payload in (
            b"\xff",
            b"{",
            b"[]",
            _record(version=2),
            _record(extra=1),
            _record(routine_id="b" * 32),
            _record(message=""),
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
