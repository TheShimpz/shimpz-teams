"""Logical operation ids, outcome origins, and archive markers in the Action journal (ADR-0092 sections 4 and 5)."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from action import execution as action_execution
from action import failure as action_failure
from action import human as action_human
from action import journal as action_journal
from inference import client as brain_runtime_client

EVIDENCE = "e" * 64
RETRY_ID = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"


def operation(interrupt_id: str, value: str, operation_id: str | None = None) -> action_journal.Operation:
    return action_journal.Operation(interrupt_id, hashlib.sha256(value.encode()).hexdigest(), operation_id)


def _crash_after(path: str, phase: str) -> None:
    """Acknowledge one durable transition, then die without closing anything."""
    journal = action_journal.ActionJournal(Path(path))
    selected = operation("interrupt-1", "input-1")
    batch = journal.prepare_batch("routine-gen", "thread", [selected], archivable=True)
    if phase in {"executing", "snapshot"}:
        journal.begin(batch, selected)
    os._exit(0)


class JournalOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "private" / "journal.sqlite3"
        self.first = operation("interrupt-1", "input-1")
        self.second = operation("interrupt-2", "input-2")

    def journal(self, path: Path | None = None, **limits: int) -> action_journal.ActionJournal:
        journal = action_journal.ActionJournal(path or self.path, **limits)
        self.addCleanup(journal.close)
        return journal

    def ids(self, journal: action_journal.ActionJournal, generation: str) -> list[tuple[str, str, int]]:
        return journal._connection.execute(
            "SELECT interrupt_id, operation_id, attempts FROM operations WHERE generation = ? ORDER BY ordinal",
            (generation,),
        ).fetchall()

    def test_each_operation_id_is_durable_before_its_first_dispatch_and_survives_restart(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation", "thread", [self.first, self.second])
        prepared = self.ids(journal, "generation")
        self.assertEqual([row[2] for row in prepared], [0, 0])
        self.assertEqual(len({row[1] for row in prepared}), 2)
        self.assertTrue(all(action_journal.valid_operation_id(row[1]) for row in prepared))
        journal.close()

        reopened = self.journal()
        same = reopened.prepare_batch("generation", "thread", [self.first, self.second])
        self.assertEqual(same, batch)
        started = reopened.begin(same, self.first)
        self.assertEqual((started.execute, started.operation_id), (True, prepared[0][1]))
        reopened.complete(same, self.first, {"ok": 1})
        self.assertEqual(reopened.begin(same, self.first).operation_id, prepared[0][1])
        self.assertEqual(self.ids(reopened, "generation")[0][2], 1)

    def test_a_human_suspension_replays_the_same_logical_operation_as_a_new_attempt(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation", "thread", [self.first])
        first = journal.begin(batch, self.first)
        journal.suspend(batch, self.first)
        journal.close()
        reopened = self.journal()
        same = reopened.prepare_batch("generation", "thread", [self.first])
        second = reopened.begin(same, self.first)
        self.assertEqual(second.operation_id, first.operation_id)
        self.assertEqual(self.ids(reopened, "generation"), [("interrupt-1", first.operation_id, 2)])

    def test_a_permitted_retry_keeps_its_logical_operation_id_in_a_fresh_generation(self) -> None:
        journal = self.journal()
        retry = operation("interrupt-1", "input-1", RETRY_ID)
        batch = journal.prepare_batch("attempt-2", "thread", [retry, self.second])
        self.assertEqual(journal.begin(batch, retry).operation_id, RETRY_ID)
        self.assertNotEqual(journal.begin(batch, self.second).operation_id, RETRY_ID)
        self.assertNotEqual(batch, journal._batch("attempt-2", "thread", [self.first, self.second]))
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "changed or is corrupt"):
            journal._load_batch(journal._batch("attempt-2", "thread", [self.first, self.second]))
        for invalid in (
            [operation("interrupt-1", "input-1", RETRY_ID.upper())],
            [operation("interrupt-1", "input-1", RETRY_ID), operation("interrupt-2", "input-2", RETRY_ID)],
        ):
            with self.subTest(invalid=invalid), self.assertRaises(action_journal.ActionJournalConflictError):
                journal.prepare_batch("attempt-3", "thread", invalid)
        self.assertTrue(action_journal.valid_operation_id(action_journal.new_operation_id()))
        self.assertFalse(action_journal.valid_operation_id(None))

    def test_a_read_only_failure_ends_without_effect_and_is_never_dispatched_again(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation", "thread", [self.first, self.second])
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "not executing"):
            journal.fail_without_effect(batch, self.first)
        journal.begin(batch, self.first)
        journal.fail_without_effect(batch, self.first)
        journal.fail_without_effect(batch, self.first)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "without effect"):
            journal.begin(batch, self.first)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "every result"):
            journal.delivered(batch)
        self.assertIsNone(journal.uncertain_fingerprint("generation"))
        self.assertTrue(journal.end(batch))
        row = journal._connection.execute(
            "SELECT state, origin, evidence, result FROM operations WHERE interrupt_id = 'interrupt-1'"
        ).fetchone()
        self.assertEqual(row, ("no_effect", "execution", None, None))

    def test_only_bound_verifier_evidence_resolves_an_uncertain_operation(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation", "thread", [self.first, self.second])
        for invalid in ("", "E" * 64, "e" * 63, None):
            with (
                self.subTest(evidence=invalid),
                self.assertRaisesRegex(action_journal.ActionJournalConflictError, "evidence"),
            ):
                journal.resolve_verified(batch, self.first, invalid, {"id": "1"})
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "not executing"):
            journal.resolve_verified(batch, self.first, EVIDENCE, {"id": "1"})
        journal.begin(batch, self.first)
        journal.begin(batch, self.second)
        journal.resolve_verified(batch, self.first, EVIDENCE, {"id": "1"})
        journal.resolve_verified(batch, self.first, EVIDENCE, {"id": "1"})
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "changed after completion"):
            journal.resolve_verified(batch, self.first, "f" * 64, {"id": "1"})
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "not executing"):
            journal.complete(batch, self.first, {"id": "1"})
        journal.resolve_verified(batch, self.second, EVIDENCE, None)
        self.assertEqual(journal.begin(batch, self.first).result, {"id": "1"})
        rows = journal._connection.execute(
            "SELECT interrupt_id, state, origin, evidence FROM operations ORDER BY ordinal"
        ).fetchall()
        self.assertEqual(
            rows,
            [
                ("interrupt-1", "completed", "verification", EVIDENCE),
                ("interrupt-2", "no_effect", "verification", EVIDENCE),
            ],
        )

    def test_the_schema_refuses_an_outcome_without_its_origin_or_unbound_evidence(self) -> None:
        journal = self.journal()
        journal.prepare_batch("generation", "thread", [self.first])
        for statement in (
            "UPDATE operations SET state = 'completed', result = x'7b7d'",
            "UPDATE operations SET state = 'no_effect', origin = 'verification'",
            "UPDATE operations SET state = 'no_effect', origin = 'execution', evidence = 'e'",
            "UPDATE operations SET state = 'prepared', origin = 'execution'",
            "UPDATE operations SET attempts = -1",
            "UPDATE batches SET state = 'archived'",
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                journal._connection.execute(statement)

    def test_an_archived_generation_refuses_every_replay_and_frees_its_active_capacity(self) -> None:
        journal = self.journal(max_generations=1)
        batch = journal.prepare_batch("routine-gen", "thread", [self.first, self.second], archivable=True)
        journal.begin(batch, self.first)
        journal.complete(batch, self.first, {"ok": 1})
        journal.begin(batch, self.second)
        records = journal.snapshot("routine-gen", batch.fingerprint)
        self.assertEqual(
            [(item.interrupt_id, item.state, item.attempts) for item in records],
            [
                ("interrupt-1", "completed", 1),
                ("interrupt-2", "executing", 1),
            ],
        )
        self.assertEqual(records[1].origin, None)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "no longer current"):
            journal.snapshot("routine-gen", "0" * 64)

        journal.archive("routine-gen", batch.fingerprint)
        journal.archive("routine-gen", batch.fingerprint)
        self.assertEqual(self.ids(journal, "routine-gen"), [])
        self.assertIsNone(journal.uncertain_fingerprint("routine-gen"))
        for transition in (
            lambda: journal.prepare_batch("routine-gen", "thread", [self.first, self.second], archivable=True),
            lambda: journal.prepare_batch("routine-gen", "thread", [operation("fresh", "input")], archivable=True),
            lambda: journal.prepare_batch("routine-gen", "thread", [operation("fresh", "input")]),
            lambda: journal.snapshot("routine-gen", batch.fingerprint),
        ):
            with self.subTest(transition=transition), self.assertRaises(action_journal.ActionJournalArchivedError):
                transition()
        for transition in (
            lambda: journal.begin(batch, self.second),
            lambda: journal.end(batch),
            lambda: journal.abandon_uncertain(batch),
        ):
            with self.subTest(transition=transition), self.assertRaises(action_journal.ActionJournalConflictError):
                transition()
        self.assertFalse(journal.end_settled("routine-gen"))
        # The marker is not active: a chat generation is admitted even at a one-generation bound.
        chat = journal.prepare_batch("chat-gen", "thread", [self.first])
        journal.discard("routine-gen")
        with self.assertRaises(action_journal.ActionJournalArchivedError):
            journal.prepare_batch("routine-gen", "thread", [self.first])
        journal.begin(chat, self.first)
        journal.complete(chat, self.first, {})
        journal.delivered(chat)
        self.assertTrue(journal.release_archive("routine-gen", batch.fingerprint))
        self.assertFalse(journal.release_archive("routine-gen", batch.fingerprint))
        journal.prepare_batch("routine-gen", "thread", [self.first])

    def test_archive_markers_are_reserved_before_any_action_runs_and_never_evicted(self) -> None:
        journal = self.journal(max_archived=2)
        held = journal.prepare_batch("held", "thread", [self.first], archivable=True)
        live = journal.prepare_batch("live", "thread", [self.first], archivable=True)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "archive capacity"):
            journal.prepare_batch("third", "thread", [self.first], archivable=True)
        # Chat batches reserve no marker, and the reservation never blocks them.
        journal.prepare_batch("chat", "thread", [self.first])
        journal.archive("held", held.fingerprint)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "archive capacity"):
            journal.prepare_batch("third", "thread", [self.first], archivable=True)
        journal.discard("live")
        journal.prepare_batch("third", "thread", [self.first], archivable=True)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "archive reservation"):
            journal.prepare_batch("chat", "thread", [self.first], archivable=True)
        chat = journal.prepare_batch("chat", "thread", [self.first])
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "no archive reservation"):
            journal.archive("chat", chat.fingerprint)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "no longer current"):
            journal.archive("chat", live.fingerprint)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "no such archived"):
            journal.release_archive("chat", chat.fingerprint)
        journal.purge("held")
        self.assertEqual(
            journal._connection.execute("SELECT COUNT(*) FROM batches WHERE generation = 'held'").fetchone(), (0,)
        )

    def test_an_unreadable_snapshot_is_a_journal_failure_not_a_conflict(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("routine-gen", "thread", [self.first], archivable=True)
        connection = journal._connection
        journal._connection = mock.Mock()
        journal._connection.execute.side_effect = sqlite3.Error("offline")
        try:
            for read in (
                lambda: journal.snapshot("routine-gen", batch.fingerprint),
                lambda: journal.current_batch("routine-gen"),
            ):
                with self.assertRaisesRegex(action_journal.ActionJournalError, "could not be read") as caught:
                    read()
                self.assertNotIsInstance(caught.exception, action_journal.ActionJournalConflictError)
        finally:
            journal._connection = connection

    def test_every_crash_window_recovers_without_dispatching(self) -> None:
        context = multiprocessing.get_context("spawn")
        for phase in ("prepared", "executing", "snapshot"):
            with self.subTest(phase=phase):
                path = self.root / phase / "journal.sqlite3"
                process = context.Process(target=_crash_after, args=(str(path), phase))
                process.start()
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
                reopened = self.journal(path)
                persisted = self.ids(reopened, "routine-gen")
                self.assertTrue(action_journal.valid_operation_id(persisted[0][1]))
                batch = reopened.prepare_batch("routine-gen", "thread", [self.first], archivable=True)
                if phase == "prepared":
                    self.assertEqual(reopened.begin(batch, self.first).operation_id, persisted[0][1])
                    continue
                # The interrupted dispatch stays uncertain; reconciliation archives it without dispatching again.
                with self.assertRaises(action_journal.ActionJournalUncertainError):
                    reopened.begin(batch, self.first)
                fingerprint = reopened.uncertain_fingerprint("routine-gen")
                self.assertEqual(fingerprint, batch.fingerprint)
                reopened.archive("routine-gen", fingerprint)
                reopened.archive("routine-gen", fingerprint)
                self.assertIsNone(reopened.uncertain_fingerprint("routine-gen"))


class BatchOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.journal = action_journal.ActionJournal(Path(temporary.name) / "journal.sqlite3")
        self.addCleanup(self.journal.close)
        self.request = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "lookup", {"q": "a"})
        self.binding = SimpleNamespace(container_id="container", spec=SimpleNamespace(image="image"))

    def batch(self, execute, effect: str, kind=action_execution.ActionBatch) -> action_execution.ActionBatch:
        return kind(
            self.journal,
            "generation" if kind is action_execution.ActionBatch else "routine-gen",
            "thread",
            {"assistant": self.binding},
            action_execution.ActionBatchStrategy(
                lambda item: (item.container_id, item.spec.image),
                execute,
                lambda _request: None,
                effect=lambda _request: effect,
            ),
        )

    def test_the_same_logical_operation_reaches_every_replay_and_a_crash_never_dispatches_twice(self) -> None:
        seen: list[str] = []
        request = action_human.HumanRequest

        def suspend_once(_request, _evidence, operation_id):
            seen.append(operation_id)
            if len(seen) == 1:
                raise action_human.HumanRequestSuspensionError(object.__new__(request))
            return {"ok": True}

        batch = self.batch(suspend_once, "mutating")
        batch.prepare((self.request,))
        with self.assertRaises(action_human.HumanRequestSuspensionError):
            batch.invoke(self.request)
        replay = self.batch(suspend_once, "mutating")
        replay.prepare((self.request,))
        self.assertEqual(replay.invoke(self.request), {"ok": True})
        self.assertEqual(len(set(seen)), 1)
        self.assertTrue(action_journal.valid_operation_id(seen[0]))

        dispatched: list[str] = []

        def crash(_request, _evidence, operation_id):
            dispatched.append(operation_id)
            raise SystemExit("process died mid-dispatch")

        held = self.batch(crash, "mutating", action_execution.HeldActionBatch)
        held.prepare((self.request,))
        with self.assertRaises(SystemExit):
            held.invoke(self.request)
        restarted = self.batch(crash, "mutating", action_execution.HeldActionBatch)
        restarted.prepare((self.request,))
        with self.assertRaises(action_journal.ActionJournalUncertainError):
            restarted.invoke(self.request)
        self.assertEqual(len(dispatched), 1)
        archivable = self.journal._connection.execute(
            "SELECT archivable FROM batches WHERE generation = 'routine-gen'"
        ).fetchone()
        self.assertEqual(archivable, (1,))

    def test_only_a_handled_read_only_failure_is_recorded_as_no_effect(self) -> None:
        failure = action_failure.ActionFailure("ValueError", "", None, None, None, False, False)

        def handled(_request, _evidence, _operation_id):
            try:
                raise action_failure.ActionFailedError(failure)
            except action_failure.ActionFailedError as exc:
                raise RuntimeError("Controller problem") from exc

        for effect, raised, expected in (
            ("read_only", handled, "no_effect"),
            ("mutating", handled, "executing"),
            ("read_only", lambda *_args: (_ for _ in ()).throw(RuntimeError("transport fault")), "executing"),
        ):
            with self.subTest(effect=effect, expected=expected):
                self.journal.purge("generation")
                batch = self.batch(raised, effect)
                batch.prepare((self.request,))
                with self.assertRaises(RuntimeError):
                    batch.invoke(self.request)
                state = self.journal._connection.execute("SELECT state FROM operations").fetchone()
                self.assertEqual(state, (expected,))
                self.assertEqual(bool(batch._executing_here), expected == "executing")

    def test_a_direct_invocation_is_one_fresh_logical_operation(self) -> None:
        resolved = [action_execution.resolve_invocation_evidence(None, dict, dict) for _ in range(2)]
        self.assertNotEqual(resolved[0].operation_id, resolved[1].operation_id)
        self.assertTrue(all(action_journal.valid_operation_id(item.operation_id) for item in resolved))
        frozen = action_execution.ActionInvocationEvidence(
            action_execution.RpcPrivateInputs({}, {}), action_human.ActionTranscript(""), "a" * 64, RETRY_ID
        )
        self.assertEqual(action_execution.resolve_invocation_evidence(frozen, dict, dict).operation_id, RETRY_ID)


if __name__ == "__main__":
    unittest.main()
