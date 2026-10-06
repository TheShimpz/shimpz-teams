"""The run cursor: binding, durable prefix, stable operations, budgets, bounds, and decision phases (ADR-0101)."""

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
BOOT = "f" * 32
NONE = {"mode": "none", "step": None, "when": None}


class CursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = routine_plan.admit(_document(output=NONE), CONTRACTS)
        self.cursor = routine_cursor.start(self.plan, BINDING, 1_800_000_000, BOOT)

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
        self.assertEqual(
            advanced.selections(),
            {("publish", "/id", "", ""): "post-1", ("publish", "/meta/a~1b/0", "", ""): ["news"]},
        )
        self.assertNotIn("x" * 1000, routine_cursor.encode(advanced).decode())
        last = routine_cursor.complete(
            routine_cursor.dispatch(advanced, self.plan, "7a2d3c9f-4b5e-4d6f-8a70-829304b5c6d7", COMMITMENT),
            self.plan,
            {"shared": True},
        )
        self.assertTrue(last.done(self.plan) and last.replayed(self.plan))
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
            routine_plan.canonical({**document, "version": 1}),
            routine_plan.canonical({**document, "version": 4.0}),
            routine_plan.canonical({key: value for key, value in document.items() if key != "shown"}),
            routine_plan.canonical({**document, "shown": {"step": "publish"}}),
            routine_plan.canonical({**document, "shown": []}),
            routine_plan.canonical({key: value for key, value in document.items() if key != "budgets"}),
            routine_plan.canonical({**document, "selected": {}}),
            routine_plan.canonical({**document, "selected": [["publish", "/id"]]}),
            routine_plan.canonical({**document, "selected": [["publish", "/id", "", "/x", 1]]}),
            routine_plan.canonical({**document, "selected": [["publish", "/id", 1, "", 1]]}),
            routine_plan.canonical({**document, "reservation": [0, 0]}),
            routine_plan.canonical({**document, "calls": {}}),
            routine_plan.canonical({**document, "calls": [{"operation_id": OPERATION}]}),
            routine_plan.canonical({**document, "boot": "x"}),
            routine_plan.canonical({**document, "budgets": [4]}),
            routine_plan.canonical({**document, "budgets": {**document["budgets"], "retries": 2}}),
            routine_plan.canonical({**document, "attempts": 0}),
            routine_plan.canonical({**document, "commitment": None}),
            routine_plan.canonical({**document, "step": routine_plan.MAX_STEPS + 1}),
            routine_plan.canonical({**document, "plan": "sha256:x"}),
            routine_plan.canonical(
                {**document, "selected": [["publish", "/id", "", "", 1], ["publish", "/id", "", "", 2]]}
            ),
            routine_plan.canonical({**document, "selected": [["Publish", "/id", "", "", 1]]}),
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
                routine_cursor.start(self.plan, binding, 0, BOOT)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
            routine_cursor.start(self.plan, BINDING, -1, BOOT)
        dispatched = routine_cursor.dispatch(self.cursor, self.plan, OPERATION, COMMITMENT)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
            routine_cursor.complete(
                dispatched, self.plan, {"id": "x" * routine_plan.MAX_RETAINED_BYTES, "meta": {"a/b": [1]}}
            )
        oversized = dataclasses.replace(
            self.cursor, selected=tuple((f"s{index}", "/v", "", "", "x" * 60_000) for index in range(5))
        )
        with (
            mock.patch.object(routine_plan, "MAX_RETAINED_BYTES", 10**9),
            self.assertRaisesRegex(routine_cursor.CursorError, "cursor-too-large"),
        ):
            routine_cursor.encode(oversized)


class FaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = routine_plan.admit(_document(output=NONE), CONTRACTS)
        self.dispatched = routine_cursor.dispatch(
            routine_cursor.start(self.plan, BINDING, 1_800_000_000, BOOT), self.plan, OPERATION, COMMITMENT
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
            (routine_cursor.start(self.plan, BINDING, 0, BOOT), "handled"),
        ):
            with self.subTest(fault=fault), self.assertRaisesRegex(routine_cursor.CursorError, "cursor-not-dispatched"):
                routine_cursor.failed(cursor, fault)
        undispatched = routine_cursor.start(self.plan, BINDING, 0, BOOT)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
            routine_cursor.encode(dataclasses.replace(undispatched, fault="handled"))

    def test_an_attempt_keeps_its_workload_and_instant(self) -> None:
        start = routine_cursor.start(self.plan, BINDING, 1_800_000_000, BOOT)
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


DECIDE = {"mode": "decide", "step": None, "when": "always"}
MODEL = {"provider": "openai", "model": "gpt-6-luna", "effort": "low"}
KEPT = {"value": {"id": "post-1"}, "withheld": ["/token"]}


class DecisionPhaseTests(unittest.TestCase):
    """A decide plan's phases, accumulator, candidate, allowance, calls, and model (ADR-0101 section 6.6)."""

    def setUp(self) -> None:
        self.plan = routine_plan.admit(_document(output=DECIDE), CONTRACTS)
        self.cursor = routine_cursor.start(self.plan, BINDING, 1_800_000_000, BOOT)

    def decided(self, **changes: object) -> routine_cursor.Cursor:
        call = routine_cursor.Call(OPERATION, "shimpz-blog", "share-post", False, COMMITMENT)
        values = {
            "step": 2,
            "phase": "decision",
            "accumulator": {"results": [["publish", KEPT]], "over": False},
            "candidate": "e" * 32,
            "reservation": (16, 1),
            "calls": (call,),
            "model": dict(MODEL),
        }
        return dataclasses.replace(self.cursor, **{**values, **changes})

    def test_a_decide_plan_is_done_only_once_its_decision_closes(self) -> None:
        replayed = dataclasses.replace(self.cursor, step=2)
        self.assertTrue(replayed.replayed(self.plan))
        self.assertFalse(replayed.done(self.plan))
        deciding = self.decided()
        self.assertFalse(deciding.done(self.plan))
        self.assertTrue(dataclasses.replace(deciding, phase="closed").done(self.plan))
        decoded = routine_cursor.decode(routine_cursor.encode(deciding), BINDING)
        self.assertEqual(decoded, deciding)
        self.assertEqual(decoded.calls[0].state, "reserved")

    def test_losing_protection_is_sealed_with_the_boot_it_was_bound_in(self) -> None:
        lost = routine_cursor.lose_protection(self.cursor)
        decoded = routine_cursor.decode(routine_cursor.encode(lost), BINDING)
        self.assertEqual((decoded.boot, decoded.protection_lost), (BOOT, True))
        for boot in ("", "F" * 32, None):
            with self.subTest(boot=boot), self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
                routine_cursor.encode(dataclasses.replace(self.cursor, boot=boot))
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
            routine_cursor.encode(dataclasses.replace(self.cursor, protection_lost=1))

    def test_each_call_carries_its_classified_outcome(self) -> None:
        call = routine_cursor.Call(OPERATION, "shimpz-blog", "share-post", False, COMMITMENT)
        dispatched = dataclasses.replace(call, state="dispatched", workload="assistant-1", dispatched_at=5)
        failed = dataclasses.replace(dispatched, state="failed", fault="handled", absent=True)
        for valid in (
            call,
            dispatched,
            dataclasses.replace(dispatched, state="succeeded"),
            dataclasses.replace(dispatched, state="stopped"),
            failed,
        ):
            with self.subTest(valid=valid):
                routine_cursor.encode(self.decided(calls=(valid,)))
        invalid = (
            dataclasses.replace(call, operation_id="x"),
            dataclasses.replace(call, assistant="Bad"),
            dataclasses.replace(call, action="Bad"),
            dataclasses.replace(call, read_only=1),
            dataclasses.replace(call, commitment="x"),
            dataclasses.replace(call, commitment=None),
            dataclasses.replace(call, attempts=0),
            dataclasses.replace(call, state="unknown"),
            dataclasses.replace(call, fault="nope"),
            dataclasses.replace(call, state="failed"),
            dataclasses.replace(dispatched, fault="handled"),
            dataclasses.replace(call, workload="assistant-1"),
            dataclasses.replace(dispatched, dispatched_at=0, workload=""),
            dataclasses.replace(dispatched, workload="-bad"),
            dataclasses.replace(dispatched, workload=None),
            dataclasses.replace(dispatched, dispatched_at=-1),
            dataclasses.replace(dispatched, absent=True),
            dataclasses.replace(failed, fault="policy"),
            dataclasses.replace(call, absent=1),
            "not a call",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
                routine_cursor.encode(self.decided(calls=(value,)))

    def test_phase_parts_are_consistent(self) -> None:
        call = self.decided().calls[0]
        invalid = (
            self.decided(phase="unknown"),
            self.decided(accumulator=None),
            dataclasses.replace(self.cursor, candidate="e" * 32),
            dataclasses.replace(self.cursor, calls=(call,), reservation=(1, 1)),
            dataclasses.replace(self.cursor, model=dict(MODEL)),
            self.decided(candidate="x"),
            self.decided(reservation=(1, 2)),
            self.decided(reservation=(65, 1)),
            self.decided(reservation=(True, 1)),
            self.decided(reservation=None),
            self.decided(reservation=(16, 0)),
            self.decided(calls=(call, call), reservation=(16, 2)),
            self.decided(calls=[call]),
            self.decided(model={"provider": "openai"}),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
                routine_cursor.encode(value)
        routine_cursor.encode(self.decided(candidate=None, model=None, calls=(), reservation=(0, 0)))

    def test_the_accumulator_holds_kept_results_within_its_bound(self) -> None:
        over = {"results": [], "over": True}
        self.assertEqual(
            routine_cursor.decode(routine_cursor.encode(self.decided(accumulator=over)), BINDING).accumulator, over
        )
        large = {"value": "x" * routine_cursor.MAX_ACCUMULATOR_BYTES, "withheld": []}
        invalid = (
            [],
            {"results": [], "over": 1},
            {"results": {}, "over": False},
            {"results": [["publish"]], "over": False},
            {"results": [["Publish", KEPT]], "over": False},
            {"results": [[1, KEPT]], "over": False},
            {"results": [["publish", KEPT], ["publish", KEPT]], "over": False},
            {"results": [["publish", {"value": 1}]], "over": False},
            {"results": [["publish", {"value": 1, "withheld": {}}]], "over": False},
            {"results": [["publish", {"value": 1, "withheld": ["x"]}]], "over": False},
            {"results": [["publish", {"value": 1, "withheld": ["/b", "/a"]}]], "over": False},
            {"results": [["publish", large]], "over": False},
            {"results": [["publish", KEPT]], "over": True},
        )
        for value in invalid:
            with (
                self.subTest(value=str(value)[:60]),
                self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"),
            ):
                routine_cursor.encode(self.decided(accumulator=value))


class ContinuationTests(unittest.TestCase):
    """A held run's cursor: proven absence permits one retry, and each continuation is a new segment (ADR-0092)."""

    def setUp(self) -> None:
        self.plan = routine_plan.admit(_document(output=NONE), CONTRACTS)
        start = routine_cursor.start(self.plan, BINDING, 1_800_000_000, BOOT)
        self.dispatched = routine_cursor.dispatch(start, self.plan, OPERATION, COMMITMENT)

    def test_one_retry_follows_only_proven_absence_and_spends_its_budget(self) -> None:
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-operation-uncertain"):
            routine_cursor.retry(self.dispatched)
        absent = routine_cursor.proven_absent(routine_cursor.failed(self.dispatched, "transport"))
        retried = routine_cursor.retry(absent)
        self.assertEqual((retried.absent, retried.remaining("retries")), (False, 0))
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-not-dispatched"):
            routine_cursor.proven_absent(routine_cursor.start(self.plan, BINDING, 0, BOOT))

    def test_a_continuation_carries_its_operation_into_its_own_segment(self) -> None:
        continued = routine_cursor.continued(self.dispatched)
        self.assertEqual((continued.segment, continued.carried, continued.generation_suffix), (1, True, "s1"))
        self.assertEqual(self.dispatched.generation_suffix, "")
        last = dataclasses.replace(self.dispatched, segment=routine_cursor.MAX_SEGMENTS)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-segments-exhausted"):
            routine_cursor.continued(last)

    def test_a_refund_returns_only_part_of_a_real_reservation(self) -> None:
        spent = routine_cursor.spend(self.dispatched, "verifications", 2)
        self.assertEqual(routine_cursor.refund(spent, "verifications", 1).remaining("verifications"), 2)
        for budget, amount in (("dollars", 1), ("verifications", 0), ("verifications", True)):
            with self.subTest(budget=budget), self.assertRaisesRegex(routine_cursor.CursorError, "budget-invalid"):
                routine_cursor.refund(spent, budget, amount)


if __name__ == "__main__":
    unittest.main()
