"""The run cursor: binding, durable prefix, stable operations, its recovery, bounds, and protection (ADR-0101)."""

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
NONE = {"mode": "none", "step": None}


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

    def test_the_recovery_episode_opens_once_and_outlives_encoding_and_steps(self) -> None:
        opened = routine_cursor.open_episode(self.cursor)
        self.assertEqual((self.cursor.episode, opened.episode), (False, True))
        self.assertTrue(routine_cursor.decode(routine_cursor.encode(opened), BINDING).episode)
        advanced = routine_cursor.complete(
            routine_cursor.dispatch(opened, self.plan, OPERATION, COMMITMENT),
            self.plan,
            {"id": "1", "meta": {"a/b": [1]}},
        )
        self.assertTrue(advanced.episode)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-episode-spent"):
            routine_cursor.open_episode(advanced)

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
            routine_plan.canonical({**document, "version": 5.0}),
            routine_plan.canonical({key: value for key, value in document.items() if key != "shown"}),
            routine_plan.canonical({**document, "shown": {"step": "publish"}}),
            routine_plan.canonical({**document, "shown": []}),
            routine_plan.canonical({key: value for key, value in document.items() if key != "episode"}),
            routine_plan.canonical({**document, "selected": {}}),
            routine_plan.canonical({**document, "selected": [["publish", "/id"]]}),
            routine_plan.canonical({**document, "selected": [["publish", "/id", "", "/x", 1]]}),
            routine_plan.canonical({**document, "selected": [["publish", "/id", 1, "", 1]]}),
            routine_plan.canonical({**document, "unexpected": None}),
            routine_plan.canonical({**document, "boot": "x"}),
            routine_plan.canonical({**document, "episode": 1}),
            # Only an episode continues a run, and only a continuation carries an operation.
            routine_plan.canonical({**document, "continued": True}),
            routine_plan.canonical({**document, "carried": True}),
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


class ProtectionTests(unittest.TestCase):
    """A run's protection is bound in its Team boot, and its loss is sealed (ADR-0101 section 6.2)."""

    def setUp(self) -> None:
        self.plan = routine_plan.admit(_document(output=NONE), CONTRACTS)
        self.cursor = routine_cursor.start(self.plan, BINDING, 1_800_000_000, BOOT)

    def test_losing_protection_is_sealed_with_the_boot_it_was_bound_in(self) -> None:
        lost = routine_cursor.lose_protection(self.cursor)
        decoded = routine_cursor.decode(routine_cursor.encode(lost), BINDING)
        self.assertEqual((decoded.boot, decoded.protection_lost), (BOOT, True))
        for boot in ("", "F" * 32, None):
            with self.subTest(boot=boot), self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
                routine_cursor.encode(dataclasses.replace(self.cursor, boot=boot))
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
            routine_cursor.encode(dataclasses.replace(self.cursor, protection_lost=1))


class ContinuationTests(unittest.TestCase):
    """A held run's cursor: proven absence permits the retry, inside the run's one continuation (ADR-0092)."""

    def setUp(self) -> None:
        self.plan = routine_plan.admit(_document(output=NONE), CONTRACTS)
        start = routine_cursor.start(self.plan, BINDING, 1_800_000_000, BOOT)
        self.dispatched = routine_cursor.open_episode(routine_cursor.dispatch(start, self.plan, OPERATION, COMMITMENT))

    def test_a_retry_follows_only_proven_absence_and_spends_it(self) -> None:
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-operation-uncertain"):
            routine_cursor.retry(self.dispatched)
        absent = routine_cursor.proven_absent(routine_cursor.failed(self.dispatched, "transport"))
        self.assertFalse(routine_cursor.retry(absent).absent)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-not-dispatched"):
            routine_cursor.proven_absent(routine_cursor.start(self.plan, BINDING, 0, BOOT))

    def test_the_one_continuation_carries_its_operation_into_its_own_generation(self) -> None:
        continued = routine_cursor.continued(self.dispatched)
        self.assertEqual((continued.continued, continued.carried, continued.generation_suffix), (True, True, "s1"))
        self.assertEqual(self.dispatched.generation_suffix, "")
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-continued"):
            routine_cursor.continued(continued)
        without_episode = dataclasses.replace(self.dispatched, episode=False)
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-invalid"):
            routine_cursor.continued(without_episode)


if __name__ == "__main__":
    unittest.main()
