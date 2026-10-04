from __future__ import annotations

import hashlib
import multiprocessing
import os
import sqlite3
import stat
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from action import execution as action_execution
from action import journal as action_journal
from action import result as action_result


def operation(interrupt_id: str, value: str) -> action_journal.Operation:
    return action_journal.Operation(interrupt_id, hashlib.sha256(value.encode()).hexdigest())


def _crash_after_acknowledged_transition(path: str, phase: str) -> None:
    journal = action_journal.ActionJournal(Path(path))
    selected = operation("interrupt-1", "validated-input-1")
    batch = journal.prepare_batch("generation-1", "thread-1", [selected])
    if phase in {"executing", "completed", "delivered"}:
        journal.begin(batch, selected)
    if phase in {"completed", "delivered"}:
        journal.complete(batch, selected, {"answer": 1})
    if phase == "delivered":
        journal.delivered(batch)
    os._exit(0)


class _ConnectionProxy:
    def __init__(self, connection: sqlite3.Connection, match: str, *, result: object = None) -> None:
        self.connection = connection
        self.match = match
        self.result = result
        self.used = False

    def execute(self, statement: str, parameters: object = ()) -> object:
        if not self.used and self.match in statement:
            self.used = True
            if self.result is None:
                raise sqlite3.Error("synthetic storage failure")
            return mock.Mock(fetchone=lambda: self.result)
        return self.connection.execute(statement, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self.connection, name)


class ActionJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "journal.sqlite3"
        self.first = operation("interrupt-1", "validated-input-1")
        self.second = operation("interrupt-2", "validated-input-2")

    def journal(self, **limits: int) -> action_journal.ActionJournal:
        journal = action_journal.ActionJournal(self.path, **limits)
        self.addCleanup(journal.close)
        return journal

    def assert_sql_failure(
        self,
        journal: action_journal.ActionJournal,
        match: str,
        operation: Callable[[], object],
        message: str,
    ) -> None:
        connection = journal._connection
        proxy = _ConnectionProxy(connection, match)
        journal._connection = proxy
        try:
            with self.assertRaisesRegex(action_journal.ActionJournalError, message):
                operation()
            self.assertTrue(proxy.used)
        finally:
            journal._connection = connection
            if connection.in_transaction:
                connection.execute("ROLLBACK")

    def assert_change_conflict(
        self,
        journal: action_journal.ActionJournal,
        operation: Callable[[], object],
    ) -> None:
        connection = journal._connection
        proxy = _ConnectionProxy(connection, "SELECT changes()", result=(0,))
        journal._connection = proxy
        try:
            with self.assertRaises(action_journal.ActionJournalConflictError):
                operation()
            self.assertTrue(proxy.used)
        finally:
            journal._connection = connection
            if connection.in_transaction:
                connection.execute("ROLLBACK")

    def test_reopen_returns_canonical_cached_result_without_reexecution(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])
        started = journal.begin(batch, self.first)
        self.assertEqual(started, action_journal.Execution(True, None, started.operation_id))
        self.assertTrue(action_journal.valid_operation_id(started.operation_id))
        journal.complete(batch, self.first, {"z": [2, 1], "a": "ok"})
        journal.close()

        reopened = self.journal()
        same = reopened.prepare_batch("generation-1", "thread-1", [self.first])

        self.assertEqual(same, batch)
        self.assertEqual(
            reopened.begin(same, self.first),
            action_journal.Execution(False, {"a": "ok", "z": [2, 1]}, started.operation_id),
        )
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_executing_operation_is_uncertain_after_reopen(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])
        self.assertTrue(journal.begin(batch, self.first).execute)
        journal.close()

        reopened = self.journal()
        same = reopened.prepare_batch("generation-1", "thread-1", [self.first])
        with self.assertRaises(action_journal.ActionJournalUncertainError):
            reopened.begin(same, self.first)

    def test_proven_suspension_returns_only_the_executing_operation_to_prepared(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first, self.second])
        journal.begin(batch, self.first)
        journal.complete(batch, self.first, {"answer": 1})
        journal.begin(batch, self.second)

        journal.suspend(batch, self.second)

        self.assertEqual(journal.begin(batch, self.first).result, {"answer": 1})
        self.assertTrue(journal.begin(batch, self.second).execute)
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.suspend(batch, self.first)

    def test_process_crash_loses_no_acknowledged_transition(self) -> None:
        expected = {
            "prepared": ("prepared", None),
            "executing": ("executing", None),
            "completed": ("completed", b'{"answer":1}'),
            "delivered": None,
        }
        context = multiprocessing.get_context("spawn")
        for phase, persisted in expected.items():
            with self.subTest(phase=phase):
                path = Path(self.temporary.name) / phase / "journal.sqlite3"
                process = context.Process(
                    target=_crash_after_acknowledged_transition,
                    args=(str(path), phase),
                )
                process.start()
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

                reopened = action_journal.ActionJournal(path)
                self.addCleanup(reopened.close)
                row = reopened._connection.execute(
                    "SELECT state, result FROM operations WHERE generation = 'generation-1'"
                ).fetchone()
                self.assertEqual(row, persisted)

    def test_uses_bounded_wal_full_durability_policy(self) -> None:
        journal = self.journal()

        self.assertEqual(journal._connection.execute("PRAGMA journal_mode").fetchone(), ("wal",))
        self.assertEqual(journal._connection.execute("PRAGMA synchronous").fetchone(), (2,))
        self.assertEqual(
            journal._connection.execute("PRAGMA wal_autocheckpoint").fetchone(),
            (action_journal.WAL_AUTOCHECKPOINT_PAGES,),
        )

    def test_changed_pending_batch_and_changed_completed_result_fail_closed(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])

        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.prepare_batch("generation-1", "thread-1", [self.second])
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.prepare_batch("generation-1", "other-thread", [self.first])

        journal.begin(batch, self.first)
        journal.complete(batch, self.first, {"answer": 1})
        journal.complete(batch, self.first, {"answer": 1})
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.complete(batch, self.first, {"answer": 2})

    def test_batch_identity_is_scanned_once_before_point_transitions(self) -> None:
        journal = self.journal()
        operations = (
            self.first,
            self.second,
            operation("interrupt-3", "validated-input-3"),
        )
        batch = journal.prepare_batch("generation-1", "thread-1", operations)

        with mock.patch.object(journal, "_load_batch", wraps=journal._load_batch) as full_scan:
            for selected in operations:
                journal.begin(batch, selected)
                journal.complete(batch, selected, {"interrupt": selected.interrupt_id})
            self.assertEqual(full_scan.call_count, 1)
            journal.delivered(batch)

        self.assertEqual(full_scan.call_count, 2)
        self.assertEqual(journal._validated_batches, {})

    def test_point_transition_revalidates_persisted_batch_header(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])
        journal.begin(batch, self.first)
        journal._connection.execute(
            "UPDATE batches SET fingerprint = ? WHERE generation = ?",
            ("f" * 64, batch.generation),
        )

        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.complete(batch, self.first, {"answer": 1})

    def test_delivery_requires_all_results_then_allows_the_next_batch(self) -> None:
        journal = self.journal(max_generations=1)
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first, self.second])
        journal.begin(batch, self.first)
        journal.complete(batch, self.first, {"first": True})
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.delivered(batch)

        journal.begin(batch, self.second)
        journal.complete(batch, self.second, {"second": True})
        journal.delivered(batch)
        journal.delivered(batch)

        next_batch = journal.prepare_batch("generation-1", "thread-1", [operation("interrupt-3", "input-3")])
        self.assertNotEqual(next_batch.fingerprint, batch.fingerprint)
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.delivered(batch)
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.begin(batch, self.first)

        next_operation = next_batch.operations[0]
        journal.begin(next_batch, next_operation)
        journal.complete(next_batch, next_operation, {"third": True})
        journal.delivered(next_batch)
        journal.prepare_batch("generation-2", "thread-2", [self.first])

    def test_delivery_reuses_confirmed_canonical_result_digests(self) -> None:
        journal = self.journal()
        operations = (self.first, self.second)
        batch = journal.prepare_batch("generation-1", "thread-1", operations)
        for selected in operations:
            journal.begin(batch, selected)
            journal.complete(batch, selected, {"interrupt": selected.interrupt_id})

        with mock.patch.object(journal, "_decode_result", wraps=journal._decode_result) as decode:
            journal.delivered(batch)

        decode.assert_not_called()
        self.assertEqual(journal._validated_results, {})

    def test_reopen_canonically_validates_results_before_delivery(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])
        journal.begin(batch, self.first)
        journal.complete(batch, self.first, {"answer": 1})
        journal.close()

        reopened = self.journal()
        with mock.patch.object(reopened, "_decode_result", wraps=reopened._decode_result) as decode:
            reopened.delivered(batch)

        decode.assert_called_once_with(b'{"answer":1}')

    def test_corrupt_database_and_noncanonical_cache_fail_closed(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])
        journal.begin(batch, self.first)
        journal.complete(batch, self.first, {"answer": 1})
        journal.close()

        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE operations SET result = ? WHERE interrupt_id = ?",
            (b'{"answer": 1}', self.first.interrupt_id),
        )
        connection.commit()
        connection.close()

        reopened = self.journal()
        with self.assertRaises(action_journal.ActionJournalCorruptionError):
            reopened.begin(batch, self.first)
        reopened.close()

        self.path.write_bytes(b"not-a-sqlite-database")
        self.path.chmod(0o600)
        with self.assertRaises(action_journal.ActionJournalCorruptionError):
            action_journal.ActionJournal(self.path)

    def test_capacity_and_json_limits_are_enforced_without_raw_inputs(self) -> None:
        journal = self.journal(max_generations=1, max_operations=1, max_result_bytes=16)
        batch = journal.prepare_batch("generation-1", "thread-secret", [self.first])
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.prepare_batch("generation-2", "thread-2", [self.second])
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.prepare_batch("generation-1", "thread-secret", [self.first, self.second])

        journal.begin(batch, self.first)
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.complete(batch, self.first, {"secret": "raw-input-must-not-fit"})

        raw = self.path.read_bytes()
        self.assertNotIn(b"thread-secret", raw)
        self.assertNotIn(b"validated-input-1", raw)

    def test_purge_removes_even_uncertain_generation_and_frees_capacity(self) -> None:
        journal = self.journal(max_generations=1)
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])
        journal.begin(batch, self.first)

        journal.purge("generation-1")
        journal.purge("generation-1")
        replacement = journal.prepare_batch("generation-2", "thread-2", [self.second])

        self.assertTrue(journal.begin(replacement, self.second).execute)
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.begin(batch, self.first)

    def test_exact_batch_purge_keeps_a_newer_batch_of_the_same_generation(self) -> None:
        journal = self.journal(max_generations=1)
        paused = journal.prepare_batch("generation-1", "thread-1", [self.first])
        # A new turn ends the paused batch, replaces it, and starts its own Action before the old cleanup purges.
        self.assertTrue(journal.end_settled("generation-1"))
        newer = journal.prepare_batch("generation-1", "thread-1", [self.second])
        journal.begin(newer, self.second)

        journal.purge_batch("generation-1", paused.fingerprint)

        self.assertEqual(journal.current_batch("generation-1"), (newer.fingerprint, "open"))
        self.assertEqual(journal.uncertain_fingerprint("generation-1"), newer.fingerprint)
        # The exact batch goes in any state, uncertain included, and an absent one is already done.
        journal.purge_batch("generation-1", newer.fingerprint)
        journal.purge_batch("generation-1", newer.fingerprint)
        self.assertIsNone(journal.current_batch("generation-1"))
        self.assertTrue(
            journal.begin(journal.prepare_batch("generation-2", "thread-2", [self.first]), self.first).execute
        )

    def test_exact_batch_purge_refuses_an_invalid_identity(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])
        for generation, fingerprint in (
            ("generation-1", None),
            ("generation-1", batch.fingerprint.upper()),
            ("generation-1", batch.fingerprint[:-1]),
            ("bad generation", batch.fingerprint),
        ):
            with (
                self.subTest(generation=generation, fingerprint=fingerprint),
                self.assertRaises(action_journal.ActionJournalConflictError),
            ):
                journal.purge_batch(generation, fingerprint)
        self.assertEqual(journal.current_batch("generation-1"), (batch.fingerprint, "open"))

    def test_terminal_abandonment_removes_only_the_exact_uncertain_batch(self) -> None:
        journal = self.journal(max_generations=1)
        uncertain = journal.prepare_batch("generation-1", "thread-1", [self.first])
        journal.begin(uncertain, self.first)

        self.assertTrue(journal.abandon_uncertain(uncertain))
        self.assertFalse(journal.abandon_uncertain(uncertain))
        replacement = journal.prepare_batch("generation-1", "thread-2", [self.second])
        with self.assertRaises(action_journal.ActionJournalConflictError):
            journal.abandon_uncertain(uncertain)

        self.assertTrue(journal.begin(replacement, self.second).execute)

    def test_terminal_abandonment_preserves_a_completed_batch_for_replay(self) -> None:
        journal = self.journal()
        completed = journal.prepare_batch("generation-1", "thread-1", [self.first])
        journal.begin(completed, self.first)
        journal.complete(completed, self.first, {"answer": 1})

        self.assertFalse(journal.abandon_uncertain(completed))
        replay = journal.begin(completed, self.first)

        self.assertFalse(replay.execute)
        self.assertEqual(replay.result, {"answer": 1})

    def test_fresh_turn_ends_settled_work_but_keeps_uncertain_work(self) -> None:
        journal = self.journal()
        prepared = journal.prepare_batch("generation-0", "thread-0", [self.first])
        self.assertTrue(journal.end_settled("generation-0"))
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "ended"):
            journal.begin(prepared, self.first)

        paused = journal.prepare_batch("generation-1", "thread-1", [self.first, self.second])
        journal.begin(paused, self.first)
        journal.complete(paused, self.first, {"answer": 1})
        journal.begin(paused, self.second)
        journal.suspend(paused, self.second)

        self.assertTrue(journal.end_settled("generation-1"))
        self.assertFalse(journal.end_settled("generation-1"))
        self.assertFalse(journal.end_settled("generation-absent"))

        uncertain = journal.prepare_batch("generation-2", "thread-2", [self.second])
        journal.begin(uncertain, self.second)
        self.assertFalse(journal.end_settled("generation-2"))
        with self.assertRaises(action_journal.ActionJournalUncertainError):
            journal.begin(uncertain, self.second)

        completed = journal.prepare_batch("generation-3", "thread-3", [self.first])
        journal.begin(completed, self.first)
        journal.complete(completed, self.first, {"answer": 3})
        self.assertTrue(journal.end_settled("generation-3"))
        fresh = journal.prepare_batch("generation-3", "thread-3", [self.second])
        self.assertTrue(journal.begin(fresh, self.second).execute)

    def test_an_ended_batch_keeps_receipts_refuses_replay_and_admits_a_fresh_batch(self) -> None:
        journal = self.journal(max_generations=1)
        completed = journal.prepare_batch("generation-1", "thread-1", [self.first])
        journal.begin(completed, self.first)
        journal.complete(completed, self.first, {"answer": 1})

        self.assertTrue(journal.end(completed))
        self.assertFalse(journal.end(completed))
        receipts = self.read_rows(
            "SELECT b.state, o.state, o.result FROM batches AS b JOIN operations AS o USING (generation)"
        )
        self.assertEqual(receipts, [("ended", "completed", b'{"answer":1}')])
        for transition in (
            lambda: journal.begin(completed, self.first),
            lambda: journal.complete(completed, self.first, {"answer": 1}),
            lambda: journal.delivered(completed),
            lambda: journal.prepare_batch("generation-1", "thread-2", [self.second, self.first]),
        ):
            with self.subTest(transition=transition), self.assertRaises(action_journal.ActionJournalConflictError):
                transition()
        self.assertIsNone(journal.uncertain_fingerprint("generation-1"))
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "capacity"):
            journal.prepare_batch("generation-2", "thread-2", [self.second])

        self.assertEqual(journal.prepare_batch("generation-1", "thread-1", [self.first]), completed)
        self.assertEqual(journal.begin(completed, self.first).result, {"answer": 1})
        self.assertTrue(journal.end(completed))
        fresh = journal.prepare_batch("generation-1", "thread-1", [self.second])
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "replaced this ending handle"):
            journal.end(completed)
        self.assertTrue(journal.begin(fresh, self.second).execute)
        journal.complete(fresh, self.second, {"answer": 2})
        journal.delivered(fresh)
        self.assertFalse(journal.end(fresh))

    def test_an_uncertain_batch_never_ends(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first, self.second])
        journal.begin(batch, self.first)

        self.assertFalse(journal.end(batch))
        self.assertEqual(journal.uncertain_fingerprint("generation-1"), batch.fingerprint)
        with self.assertRaises(action_journal.ActionJournalUncertainError):
            journal.begin(batch, self.first)

    def test_a_reopened_journal_ends_a_completed_batch_before_a_fresh_turn(self) -> None:
        """A Controller restart loses the in-memory turn, never the generation's next batch."""
        journal = self.journal()
        completed = journal.prepare_batch("generation-1", "thread-1", [self.first])
        journal.begin(completed, self.first)
        journal.complete(completed, self.first, {"answer": 1})
        journal.close()

        with action_journal.ActionJournal(self.path) as reopened:
            with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "pending"):
                reopened.prepare_batch("generation-1", "thread-1", [self.second])
            self.assertTrue(reopened.end_settled("generation-1"))
            fresh = reopened.prepare_batch("generation-1", "thread-1", [self.second])
            self.assertTrue(reopened.begin(fresh, self.second).execute)

    def read_rows(self, statement: str) -> list[tuple[object, ...]]:
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(statement).fetchall()
        finally:
            connection.close()

    def test_unsafe_file_and_symlink_paths_are_rejected(self) -> None:
        self.path.parent.mkdir(mode=0o700)
        self.path.write_bytes(b"")
        self.path.chmod(0o644)
        with self.assertRaises(action_journal.ActionJournalCorruptionError):
            action_journal.ActionJournal(self.path)

        self.path.unlink()
        victim = Path(self.temporary.name) / "victim"
        victim.write_bytes(b"unchanged")
        self.path.symlink_to(victim)
        with self.assertRaises(action_journal.ActionJournalCorruptionError):
            action_journal.ActionJournal(self.path)
        self.assertEqual(victim.read_bytes(), b"unchanged")

    def test_new_database_file_and_parent_entry_are_fsynced(self) -> None:
        synced_modes: list[int] = []
        real_fsync = action_journal.os.fsync

        def observe(descriptor: int) -> None:
            synced_modes.append(action_journal.os.fstat(descriptor).st_mode)
            real_fsync(descriptor)

        with mock.patch.object(action_journal.os, "fsync", side_effect=observe):
            journal = self.journal()

        self.assertIsNotNone(journal)
        self.assertEqual(len(synced_modes), 2)
        self.assertTrue(stat.S_ISREG(synced_modes[0]))
        self.assertTrue(stat.S_ISDIR(synced_modes[1]))

    def test_hardlinked_database_fails_closed(self) -> None:
        journal = self.journal()
        journal.close()
        linked = self.path.parent / "journal-copy.sqlite3"
        linked.hardlink_to(self.path)

        with self.assertRaises(action_journal.ActionJournalCorruptionError):
            action_journal.ActionJournal(self.path)

    def test_foreign_parent_or_database_owner_fails_closed(self) -> None:
        journal = self.journal()
        journal.close()

        effective_uid = action_journal.os.geteuid()
        with (
            mock.patch.object(action_journal.os, "geteuid", return_value=effective_uid + 1),
            self.assertRaises(action_journal.ActionJournalCorruptionError),
        ):
            action_journal.ActionJournal(self.path)

    def test_scalar_operation_and_json_guards_reject_invalid_values(self) -> None:
        for value in (0, True, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                action_journal._positive_limit(value, "limit")
        with self.assertRaises(action_journal.ActionJournalConflictError):
            action_journal._safe_id("../unsafe", "identifier")
        with self.assertRaises(action_journal.ActionJournalConflictError):
            action_journal._operation(object())
        with self.assertRaises(action_journal.ActionJournalConflictError):
            action_journal._operation(action_journal.Operation("interrupt", "invalid"))
        # The journal refuses a result that is not bounded canonical JSON as a conflict.
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "canonical"):
            action_journal._canonical_result("\ud800", 100)

    def test_durable_result_admission_matches_the_rpc_frame_bound(self) -> None:
        self.assertEqual(action_result.MAX_RESULT_BYTES, action_execution.MAX_RPC_RESPONSE_BYTES)
        overhead = len(action_result.canonical({"text": ""}, action_result.MAX_RESULT_BYTES))
        fits = {"text": "x" * (action_result.MAX_RESULT_BYTES - overhead)}
        action_result.require_durable(fits)
        with self.assertRaisesRegex(action_result.ActionResultError, "size"):
            action_result.require_durable({"text": fits["text"] + "x"})
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])
        journal.begin(batch, self.first)
        journal.complete(batch, self.first, fits)
        self.assertEqual(journal.begin(batch, self.first).result, fits)

    def test_file_configuration_schema_and_transaction_failures_are_closed(self) -> None:
        with (
            mock.patch.object(Path, "mkdir", side_effect=OSError("offline")),
            self.assertRaisesRegex(action_journal.ActionJournalCorruptionError, "unavailable"),
        ):
            action_journal.ActionJournal(self.path)

        journal = object.__new__(action_journal.ActionJournal)
        connection = mock.Mock()
        journal._connection = connection
        connection.execute.return_value.fetchone.return_value = ("delete",)
        with self.assertRaisesRegex(action_journal.ActionJournalCorruptionError, "durable mode"):
            journal._configure()

        connection.reset_mock()
        results = [("wal",), (0,), (1,)]
        connection.execute.side_effect = lambda _statement: mock.Mock(fetchone=lambda: results.pop(0))
        with self.assertRaisesRegex(action_journal.ActionJournalCorruptionError, "durability policy"):
            journal._configure()

        connection.execute.side_effect = sqlite3.Error("offline")
        with self.assertRaisesRegex(action_journal.ActionJournalCorruptionError, "integrity"):
            journal._validate_schema()
        with self.assertRaisesRegex(action_journal.ActionJournalError, "transaction"):
            journal._transaction()
        with self.assertRaisesRegex(action_journal.ActionJournalError, "commit"):
            journal._commit()

        journal._closed = True
        with self.assertRaisesRegex(action_journal.ActionJournalError, "closed"):
            journal._ensure_open()

    def test_batch_and_handle_shapes_are_exact(self) -> None:
        journal = self.journal(max_operations=2)
        for operations in ("invalid", object(), (), (self.first, self.second, operation("third", "three"))):
            with self.subTest(operations=operations), self.assertRaises(action_journal.ActionJournalConflictError):
                journal._batch("generation", "thread", operations)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "repeats"):
            journal._batch("generation", "thread", (self.first, self.first))

        valid = journal._batch("generation", "thread", (self.first,))
        invalid_handles = (
            object(),
            action_journal.Batch(valid.generation, "invalid", valid.operations),
            action_journal.Batch(valid.generation, valid.fingerprint, ()),
            action_journal.Batch(valid.generation, valid.fingerprint, (self.first, self.first)),
        )
        for handle in invalid_handles:
            with self.subTest(handle=handle), self.assertRaises(action_journal.ActionJournalConflictError):
                journal._validate_handle(handle)

    def test_internal_read_failures_are_never_mistaken_for_conflicts(self) -> None:
        journal = self.journal()
        batch = journal._batch("generation", "thread", (self.first,))
        connection = journal._connection
        journal._connection = mock.Mock()
        journal._connection.execute.side_effect = sqlite3.Error("offline")
        try:
            with self.assertRaisesRegex(action_journal.ActionJournalCorruptionError, "batch could not be read"):
                journal._load_batch(batch)
            journal._validated_batches[(batch.generation, batch.fingerprint)] = {
                self.first.interrupt_id: (0, self.first.fingerprint)
            }
            with self.assertRaisesRegex(action_journal.ActionJournalCorruptionError, "operation could not be read"):
                journal._load_operation(batch, self.first)
        finally:
            journal._connection = connection

    def test_constructor_schema_and_context_manager_edges_are_closed(self) -> None:
        with (
            mock.patch.object(
                action_journal.sqlite3,
                "connect",
                side_effect=action_journal.ActionJournalCorruptionError("invalid"),
            ),
            self.assertRaises(action_journal.ActionJournalCorruptionError),
        ):
            action_journal.ActionJournal(self.path)
        with (
            mock.patch.object(
                action_journal.ActionJournal,
                "_configure",
                side_effect=action_journal.ActionJournalCorruptionError("invalid"),
            ),
            self.assertRaises(action_journal.ActionJournalCorruptionError),
        ):
            action_journal.ActionJournal(self.path)

        journal = self.journal()
        journal._connection.execute("PRAGMA user_version = 1")
        journal.close()
        with self.assertRaisesRegex(action_journal.ActionJournalCorruptionError, "schema"):
            action_journal.ActionJournal(self.path)

        replacement = Path(self.temporary.name) / "context" / "journal.sqlite3"
        with action_journal.ActionJournal(replacement) as opened:
            self.assertIsInstance(opened, action_journal.ActionJournal)
        with self.assertRaisesRegex(action_journal.ActionJournalError, "closed"):
            opened.__enter__()
        opened.__exit__()

    def test_prepare_begin_complete_and_suspend_reject_state_corruption(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation", "thread", (self.first,))
        outsider = operation("outsider", "value")
        for method, arguments in (
            (journal.begin, (batch, outsider)),
            (journal.complete, (batch, outsider, {"ok": True})),
            (journal.suspend, (batch, outsider)),
        ):
            with self.subTest(method=method.__name__), self.assertRaises(action_journal.ActionJournalConflictError):
                method(*arguments)

        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "not executing"):
            journal.complete(batch, self.first, {"ok": True})
        journal.begin(batch, self.first)
        with (
            mock.patch.object(
                journal,
                "_load_operation",
                return_value=(
                    0,
                    self.first.interrupt_id,
                    self.first.fingerprint,
                    "prepared",
                    b"result",
                    action_journal.new_operation_id(),
                    None,
                    None,
                ),
            ),
            self.assertRaisesRegex(action_journal.ActionJournalCorruptionError, "durable state"),
        ):
            journal.begin(batch, self.first)

    def test_cached_result_guards_reject_invalid_raw_and_cached_bytes(self) -> None:
        journal = self.journal()
        batch = journal._batch("generation", "thread", (self.first,))
        for raw in ("invalid", b"x" * (journal.max_result_bytes + 1)):
            with self.subTest(raw=type(raw)), self.assertRaises(action_journal.ActionJournalCorruptionError):
                journal._decode_result(raw)
            with self.subTest(raw=type(raw)), self.assertRaises(action_journal.ActionJournalCorruptionError):
                journal._validated_result(batch, self.first.interrupt_id, raw)

        invalid = b"not-json"
        journal._validated_results[(batch.generation, self.first.interrupt_id)] = hashlib.sha256(invalid).digest()
        with self.assertRaises(action_journal.ActionJournalCorruptionError):
            journal._validated_result(batch, self.first.interrupt_id, invalid)

    def test_sqlite_write_failures_are_translated_for_every_transition(self) -> None:
        journal = self.journal()
        self.assert_sql_failure(
            journal,
            "SELECT fingerprint, state, archivable FROM batches",
            lambda: journal.prepare_batch("generation", "thread", (self.first,)),
            "prepared",
        )

        batch = journal.prepare_batch("generation", "thread", (self.first,))
        self.assert_sql_failure(
            journal,
            "UPDATE operations SET state = 'executing'",
            lambda: journal.begin(batch, self.first),
            "begin",
        )
        journal.begin(batch, self.first)
        self.assert_sql_failure(
            journal,
            "DELETE FROM batches",
            lambda: journal.abandon_uncertain(batch),
            "abandoned",
        )
        self.assert_sql_failure(
            journal,
            "UPDATE operations SET state = ?, origin = ?",
            lambda: journal.complete(batch, self.first, {"ok": True}),
            "committed",
        )
        self.assert_sql_failure(
            journal,
            "UPDATE operations SET state = 'prepared'",
            lambda: journal.suspend(batch, self.first),
            "suspension",
        )
        journal.complete(batch, self.first, {"ok": True})
        self.assert_sql_failure(
            journal,
            "DELETE FROM batches",
            lambda: journal.delivered(batch),
            "delivery",
        )

    def test_purge_storage_failures_and_invalid_capacity_row_are_closed(self) -> None:
        journal = self.journal()
        connection = journal._connection
        proxy = _ConnectionProxy(connection, "SELECT COUNT(*) FROM batches", result=None)
        proxy.result = (None,)
        journal._connection = proxy
        try:
            with self.assertRaisesRegex(action_journal.ActionJournalCorruptionError, "capacity"):
                journal.prepare_batch("generation", "thread", (self.first,))
        finally:
            journal._connection = connection
            if connection.in_transaction:
                connection.execute("ROLLBACK")

        self.assert_sql_failure(
            journal,
            "DELETE FROM batches",
            lambda: journal.purge("generation"),
            "purged",
        )
        self.assert_sql_failure(
            journal,
            "DELETE FROM batches",
            lambda: journal.purge_batch("generation", "a" * 64),
            "purged",
        )
        self.assert_sql_failure(
            journal,
            "SELECT o.state FROM batches",
            lambda: journal.end_settled("generation"),
            "settled",
        )
        batch = journal.prepare_batch("generation", "thread", (self.first,))
        self.assert_sql_failure(
            journal,
            "UPDATE batches SET state = 'ended'",
            lambda: journal.end(batch),
            "ended",
        )
        self.assert_change_conflict(journal, lambda: journal.end_settled("generation"))
        journal.end(batch)
        self.assert_sql_failure(
            journal,
            "DELETE FROM batches WHERE generation = ? AND state = 'ended'",
            lambda: journal.prepare_batch("generation", "thread", (self.second,)),
            "prepared",
        )
        self.assert_change_conflict(journal, lambda: journal.prepare_batch("generation", "thread", (self.second,)))
        self.assert_sql_failure(
            journal,
            "UPDATE batches SET state = 'open'",
            lambda: journal.prepare_batch("generation", "thread", (self.first,)),
            "prepared",
        )
        self.assert_change_conflict(journal, lambda: journal.prepare_batch("generation", "thread", (self.first,)))

    def test_every_compare_and_swap_rejects_a_lost_update(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation", "thread", (self.first,))
        self.assert_change_conflict(journal, lambda: journal.begin(batch, self.first))

        journal.begin(batch, self.first)
        self.assert_change_conflict(journal, lambda: journal.complete(batch, self.first, {"ok": True}))
        self.assert_change_conflict(journal, lambda: journal.suspend(batch, self.first))
        self.assert_change_conflict(journal, lambda: journal.abandon_uncertain(batch))

        journal.complete(batch, self.first, {"ok": True})
        self.assert_change_conflict(journal, lambda: journal.delivered(batch))

        real_lstat = Path.lstat

        def foreign_database(path: Path) -> action_journal.os.stat_result:
            metadata = real_lstat(path)
            if path == self.path:
                fields = list(metadata)
                fields[4] = metadata.st_uid + 1
                return action_journal.os.stat_result(fields)
            return metadata

        with (
            mock.patch.object(Path, "lstat", autospec=True, side_effect=foreign_database),
            self.assertRaises(action_journal.ActionJournalCorruptionError),
        ):
            action_journal.ActionJournal(self.path)


if __name__ == "__main__":
    unittest.main()
