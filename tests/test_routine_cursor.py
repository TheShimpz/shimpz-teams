"""The compiled run cursor: binding, durable prefix, stable operations, budgets, and bounds (ADR-0092)."""

from __future__ import annotations

import dataclasses
import json
import unittest
from unittest import mock

from routine import cursor as routine_cursor
from routine import plan as routine_plan
from tests.test_routine_plan import CONTRACTS, _document

BINDING = routine_cursor.Binding("a" * 64, "b" * 32, 1, "c" * 32)
OPERATION = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"
COMMITMENT = "d" * 64


class CursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = routine_plan.admit(_document(), CONTRACTS)
        self.cursor = routine_cursor.start(self.plan, BINDING, 1_800_000_000)

    def test_a_completed_prefix_advances_and_keeps_only_selected_values(self) -> None:
        self.assertEqual((self.cursor.step, self.cursor.operation_id, self.cursor.attempts), (0, None, 0))
        dispatched = routine_cursor.dispatch(self.cursor, self.plan, OPERATION, COMMITMENT)
        self.assertEqual(
            (dispatched.operation_id, dispatched.attempts, dispatched.commitment), (OPERATION, 1, COMMITMENT)
        )
        result = {"id": "post-1", "meta": {"a/b": [["news"]]}, "body": "x" * 1000}
        advanced = routine_cursor.complete(dispatched, self.plan, result)
        self.assertEqual(
            (advanced.step, advanced.operation_id, advanced.attempts, advanced.commitment), (1, None, 0, None)
        )
        self.assertEqual(advanced.selections(), {("publish", "/id"): "post-1", ("publish", "/meta/a~1b/0"): ["news"]})
        self.assertNotIn("x" * 1000, routine_cursor.encode(advanced).decode())
        last = routine_cursor.complete(
            routine_cursor.dispatch(advanced, self.plan, "7a2d3c9f-4b5e-4d6f-8a70-829304b5c6d7", COMMITMENT),
            self.plan,
            {"shared": True},
        )
        self.assertTrue(last.done(self.plan))
        self.assertEqual(last.selections(), advanced.selections())
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-dispatch-invalid"):
            routine_cursor.dispatch(last, self.plan, OPERATION, COMMITMENT)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-not-dispatched"):
            routine_cursor.complete(last, self.plan, {})
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-not-dispatched"):
            routine_cursor.complete(advanced, self.plan, {})
        with self.assertRaisesRegex(routine_cursor.CursorError, "plan-reference-missing"):
            routine_cursor.complete(dispatched, self.plan, {"id": "post-1"})

    def test_a_replay_keeps_its_logical_operation_and_exact_payload(self) -> None:
        first = routine_cursor.dispatch(self.cursor, self.plan, OPERATION, COMMITMENT)
        again = routine_cursor.dispatch(first, self.plan, OPERATION, COMMITMENT)
        self.assertEqual((again.operation_id, again.attempts), (OPERATION, 2))
        for operation_id, commitment in (("7a2d3c9f-4b5e-4d6f-8a70-829304b5c6d7", COMMITMENT), (OPERATION, "e" * 64)):
            with self.subTest(operation_id=operation_id), self.assertRaisesRegex(routine_cursor.CursorError, "changed"):
                routine_cursor.dispatch(first, self.plan, operation_id, commitment)
        for operation_id, commitment in (("x", COMMITMENT), (OPERATION, "x"), (OPERATION, None)):
            with self.subTest(operation_id=operation_id), self.assertRaisesRegex(routine_cursor.CursorError, "invalid"):
                routine_cursor.dispatch(self.cursor, self.plan, operation_id, commitment)
        other = routine_plan.admit(_document(timezone="UTC"), CONTRACTS)
        for transition in (
            lambda: routine_cursor.dispatch(self.cursor, other, OPERATION, COMMITMENT),
            lambda: routine_cursor.complete(first, other, {}),
        ):
            with (
                self.subTest(transition=transition),
                self.assertRaisesRegex(routine_cursor.CursorError, "plan-changed"),
            ):
                transition()

    def test_budgets_are_spent_never_replenished_or_borrowed(self) -> None:
        spent = routine_cursor.spend(routine_cursor.spend(self.cursor, "verifications", 2), "model_calls", 4)
        self.assertEqual(dict(spent.budgets)["verifications"], 1)
        self.assertEqual(dict(spent.budgets)["model_calls"], 0)
        decoded = routine_cursor.decode(routine_cursor.encode(spent), BINDING)
        self.assertEqual(decoded.budgets, spent.budgets)
        advanced = routine_cursor.complete(
            routine_cursor.dispatch(spent, self.plan, OPERATION, COMMITMENT),
            self.plan,
            {"id": "1", "meta": {"a/b": [1]}},
        )
        self.assertEqual(advanced.budgets, spent.budgets)
        for budget, amount in (
            ("model_calls", 1),
            ("verifications", 2),
            ("retries", 0),
            ("retries", True),
            ("dollars", 1),
        ):
            with (
                self.subTest(budget=budget, amount=amount),
                self.assertRaisesRegex(routine_cursor.CursorError, "exhausted"),
            ):
                routine_cursor.spend(spent, budget, amount)

    def test_a_cursor_decodes_only_for_its_exact_binding_and_canonical_bytes(self) -> None:
        raw = routine_cursor.encode(routine_cursor.dispatch(self.cursor, self.plan, OPERATION, COMMITMENT))
        self.assertEqual(routine_cursor.decode(raw, BINDING).operation_id, OPERATION)
        for binding in (
            dataclasses.replace(BINDING, incarnation="e" * 64),
            dataclasses.replace(BINDING, routine_id="e" * 32),
            dataclasses.replace(BINDING, revision=2),
            dataclasses.replace(BINDING, run_id="e" * 32),
        ):
            with self.subTest(binding=binding), self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
                routine_cursor.decode(raw, binding)
        document = json.loads(raw)
        tampered = (
            b"not json",
            json.dumps(document).encode(),
            routine_plan.canonical({**document, "version": 2}),
            routine_plan.canonical({key: value for key, value in document.items() if key != "budgets"}),
            routine_plan.canonical({**document, "selected": {}}),
            routine_plan.canonical({**document, "selected": [["publish", "/id"]]}),
            routine_plan.canonical({**document, "budgets": [4]}),
            routine_plan.canonical({**document, "budgets": {**document["budgets"], "retries": 2}}),
            routine_plan.canonical({**document, "attempts": 0}),
            routine_plan.canonical({**document, "commitment": None}),
            routine_plan.canonical({**document, "step": 9}),
            routine_plan.canonical({**document, "plan": "sha256:x"}),
            routine_plan.canonical({**document, "selected": [["publish", "/id", 1], ["publish", "/id", 2]]}),
            routine_plan.canonical({**document, "selected": [["Publish", "/id", 1]]}),
        )
        for value in tampered:
            with self.subTest(value=value[:80]), self.assertRaises(routine_cursor.CursorError):
                routine_cursor.decode(value, BINDING)

    def test_a_cursor_stays_within_its_bounds(self) -> None:
        for binding in (
            dataclasses.replace(BINDING, incarnation="x"),
            dataclasses.replace(BINDING, revision=0),
            dataclasses.replace(BINDING, revision=True),
            dataclasses.replace(BINDING, run_id=None),
        ):
            with self.subTest(binding=binding), self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
                routine_cursor.start(self.plan, binding, 0)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
            routine_cursor.start(self.plan, BINDING, -1)
        dispatched = routine_cursor.dispatch(self.cursor, self.plan, OPERATION, COMMITMENT)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
            routine_cursor.complete(
                dispatched, self.plan, {"id": "x" * routine_plan.MAX_RETAINED_BYTES, "meta": {"a/b": [1]}}
            )
        oversized = dataclasses.replace(
            self.cursor, selected=tuple((f"s{index}", "/v", "x" * 60_000) for index in range(5))
        )
        with (
            mock.patch.object(routine_plan, "MAX_RETAINED_BYTES", 10**9),
            self.assertRaisesRegex(routine_cursor.CursorError, "cursor-too-large"),
        ):
            routine_cursor.encode(oversized)


if __name__ == "__main__":
    unittest.main()


class FaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = routine_plan.admit(_document(), CONTRACTS)
        self.dispatched = routine_cursor.dispatch(
            routine_cursor.start(self.plan, BINDING, 1_800_000_000), self.plan, OPERATION, COMMITMENT
        )

    def test_a_classified_failure_is_sealed_and_a_policy_fault_never_admits_absence(self) -> None:
        for fault in routine_cursor.FAULTS[1:]:
            with self.subTest(fault=fault):
                classified = routine_cursor.failed(self.dispatched, fault)
                self.assertEqual(routine_cursor.decode(routine_cursor.encode(classified), BINDING).fault, fault)
        policy = routine_cursor.failed(self.dispatched, "policy")
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-policy-hold"):
            routine_cursor.proven_absent(policy)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
            routine_cursor.encode(dataclasses.replace(policy, absent=True))
        # A new attempt of the operation starts unclassified.
        self.assertEqual(routine_cursor.dispatch(policy, self.plan, OPERATION, COMMITMENT).fault, "")
        for cursor, fault in (
            (self.dispatched, "unknown"),
            (self.dispatched, ""),
            (routine_cursor.start(self.plan, BINDING, 0), "handled"),
        ):
            with self.subTest(fault=fault), self.assertRaisesRegex(routine_cursor.CursorError, "cursor-not-dispatched"):
                routine_cursor.failed(cursor, fault)
        undispatched = routine_cursor.start(self.plan, BINDING, 0)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
            routine_cursor.encode(dataclasses.replace(undispatched, fault="handled"))

    def test_an_attempt_keeps_its_workload_and_instant(self) -> None:
        start = routine_cursor.start(self.plan, BINDING, 1_800_000_000)
        attempt = routine_cursor.dispatch(
            start, self.plan, OPERATION, COMMITMENT, workload="assistant-container", dispatched_at=1_800_000_100
        )
        decoded = routine_cursor.decode(routine_cursor.encode(attempt), BINDING)
        self.assertEqual((decoded.workload, decoded.dispatched_at), ("assistant-container", 1_800_000_100))
        for changed in (
            dataclasses.replace(attempt, workload="-bad"),
            dataclasses.replace(attempt, dispatched_at=-1),
            dataclasses.replace(start, workload="assistant-container"),
            dataclasses.replace(start, dispatched_at=5),
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
                routine_cursor.encode(changed)
