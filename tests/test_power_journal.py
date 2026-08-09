from __future__ import annotations

import hashlib
import math
import multiprocessing
import os
import shutil
import sqlite3
import stat
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from power import journal as power_journal


def operation(interrupt_id: str, value: str) -> power_journal.Operation:
    return power_journal.Operation(interrupt_id, hashlib.sha256(value.encode()).hexdigest())


def _crash_after_acknowledged_transition(path: str, phase: str) -> None:
    journal = power_journal.PowerJournal(Path(path))
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


class PowerJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private" / "journal.sqlite3"
        self.first = operation("interrupt-1", "validated-input-1")
        self.second = operation("interrupt-2", "validated-input-2")

    def journal(self, **limits: int) -> power_journal.PowerJournal:
        journal = power_journal.PowerJournal(self.path, **limits)
        self.addCleanup(journal.close)
        return journal

    def assert_sql_failure(
        self,
        journal: power_journal.PowerJournal,
        match: str,
        operation: Callable[[], object],
        message: str,
    ) -> None:
        connection = journal._connection
        proxy = _ConnectionProxy(connection, match)
        journal._connection = proxy
        try:
            with self.assertRaisesRegex(power_journal.PowerJournalError, message):
                operation()
            self.assertTrue(proxy.used)
        finally:
            journal._connection = connection
            if connection.in_transaction:
                connection.execute("ROLLBACK")

    def assert_change_conflict(
        self,
        journal: power_journal.PowerJournal,
        operation: Callable[[], object],
    ) -> None:
        connection = journal._connection
        proxy = _ConnectionProxy(connection, "SELECT changes()", result=(0,))
        journal._connection = proxy
        try:
            with self.assertRaises(power_journal.PowerJournalConflictError):
                operation()
            self.assertTrue(proxy.used)
        finally:
            journal._connection = connection
            if connection.in_transaction:
                connection.execute("ROLLBACK")

    def test_reopen_returns_canonical_cached_result_without_reexecution(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])
        self.assertEqual(journal.begin(batch, self.first), power_journal.Execution(True, None))
        journal.complete(batch, self.first, {"z": [2, 1], "a": "ok"})
        journal.close()

        reopened = self.journal()
        same = reopened.prepare_batch("generation-1", "thread-1", [self.first])

        self.assertEqual(same, batch)
        self.assertEqual(
            reopened.begin(same, self.first),
            power_journal.Execution(False, {"a": "ok", "z": [2, 1]}),
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
        with self.assertRaises(power_journal.PowerJournalUncertainError):
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
        with self.assertRaises(power_journal.PowerJournalConflictError):
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

                reopened = power_journal.PowerJournal(path)
                self.addCleanup(reopened.close)
                row = reopened._connection.execute(
                    "SELECT state, result FROM operations WHERE generation = 'generation-1'"
                ).fetchone()
                self.assertEqual(row, persisted)

    def test_uses_bounded_wal_normal_durability_policy(self) -> None:
        journal = self.journal()

        self.assertEqual(journal._connection.execute("PRAGMA journal_mode").fetchone(), ("wal",))
        self.assertEqual(journal._connection.execute("PRAGMA synchronous").fetchone(), (1,))
        self.assertEqual(
            journal._connection.execute("PRAGMA wal_autocheckpoint").fetchone(),
            (power_journal.WAL_AUTOCHECKPOINT_PAGES,),
        )

    def test_power_loss_model_bounds_acknowledged_state_loss(self) -> None:
        journal = self.journal()
        operations = tuple(operation(f"interrupt-{index}", f"validated-input-{index}") for index in range(16))
        batch = journal.prepare_batch("generation-1", "thread-1", operations)
        self.assertEqual(
            journal._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone(),
            (0, 0, 0),
        )
        stable_checkpoint = Path(self.temporary.name) / "stable-checkpoint.sqlite3"
        shutil.copy2(self.path, stable_checkpoint)

        transitions = 0
        for selected in operations[:-1]:
            journal.begin(batch, selected)
            journal.complete(batch, selected, {"interrupt": selected.interrupt_id})
            transitions += 2
        final = operations[-1]
        journal.begin(batch, final)
        transitions += 1

        page_size = journal._connection.execute("PRAGMA page_size").fetchone()[0]
        wal_size = self.path.with_name(f"{self.path.name}-wal").stat().st_size
        frames = (wal_size - 32) // (page_size + 24)
        self.assertEqual(transitions, power_journal.MAX_ACKNOWLEDGED_TRANSITIONS_AT_RISK)
        self.assertEqual(frames, transitions)

        simulated_loss = Path(self.temporary.name) / "loss" / "journal.sqlite3"
        simulated_loss.parent.mkdir(mode=0o700)
        shutil.copy2(stable_checkpoint, simulated_loss)
        recovered_before_checkpoint = power_journal.PowerJournal(simulated_loss)
        self.addCleanup(recovered_before_checkpoint.close)
        states = recovered_before_checkpoint._connection.execute(
            "SELECT state FROM operations ORDER BY ordinal"
        ).fetchall()
        self.assertEqual(states, [("prepared",)] * len(operations))

        journal.complete(batch, final, {"interrupt": final.interrupt_id})
        after_checkpoint = Path(self.temporary.name) / "checkpointed" / "journal.sqlite3"
        after_checkpoint.parent.mkdir(mode=0o700)
        shutil.copy2(self.path, after_checkpoint)
        recovered_after_checkpoint = power_journal.PowerJournal(after_checkpoint)
        self.addCleanup(recovered_after_checkpoint.close)
        states = recovered_after_checkpoint._connection.execute(
            "SELECT state FROM operations ORDER BY ordinal"
        ).fetchall()
        self.assertEqual(states, [("completed",)] * len(operations))

    def test_changed_pending_batch_and_changed_completed_result_fail_closed(self) -> None:
        journal = self.journal()
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first])

        with self.assertRaises(power_journal.PowerJournalConflictError):
            journal.prepare_batch("generation-1", "thread-1", [self.second])
        with self.assertRaises(power_journal.PowerJournalConflictError):
            journal.prepare_batch("generation-1", "other-thread", [self.first])

        journal.begin(batch, self.first)
        journal.complete(batch, self.first, {"answer": 1})
        journal.complete(batch, self.first, {"answer": 1})
        with self.assertRaises(power_journal.PowerJournalConflictError):
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

        with self.assertRaises(power_journal.PowerJournalConflictError):
            journal.complete(batch, self.first, {"answer": 1})

    def test_delivery_requires_all_results_then_allows_the_next_batch(self) -> None:
        journal = self.journal(max_generations=1)
        batch = journal.prepare_batch("generation-1", "thread-1", [self.first, self.second])
        journal.begin(batch, self.first)
        journal.complete(batch, self.first, {"first": True})
        with self.assertRaises(power_journal.PowerJournalConflictError):
            journal.delivered(batch)

        journal.begin(batch, self.second)
        journal.complete(batch, self.second, {"second": True})
        journal.delivered(batch)
        journal.delivered(batch)

        next_batch = journal.prepare_batch("generation-1", "thread-1", [operation("interrupt-3", "input-3")])
        self.assertNotEqual(next_batch.fingerprint, batch.fingerprint)
        with self.assertRaises(power_journal.PowerJournalConflictError):
            journal.delivered(batch)
        with self.assertRaises(power_journal.PowerJournalConflictError):
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
        with self.assertRaises(power_journal.PowerJournalCorruptionError):
            reopened.begin(batch, self.first)
        reopened.close()

        self.path.write_bytes(b"not-a-sqlite-database")
        self.path.chmod(0o600)
        with self.assertRaises(power_journal.PowerJournalCorruptionError):
            power_journal.PowerJournal(self.path)

    def test_capacity_and_json_limits_are_enforced_without_raw_inputs(self) -> None:
        journal = self.journal(max_generations=1, max_operations=1, max_result_bytes=16)
        batch = journal.prepare_batch("generation-1", "thread-secret", [self.first])
        with self.assertRaises(power_journal.PowerJournalConflictError):
            journal.prepare_batch("generation-2", "thread-2", [self.second])
        with self.assertRaises(power_journal.PowerJournalConflictError):
            journal.prepare_batch("generation-1", "thread-secret", [self.first, self.second])

        journal.begin(batch, self.first)
        with self.assertRaises(power_journal.PowerJournalConflictError):
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
        with self.assertRaises(power_journal.PowerJournalConflictError):
            journal.begin(batch, self.first)

    def test_terminal_abandonment_removes_only_the_exact_uncertain_batch(self) -> None:
        journal = self.journal(max_generations=1)
        uncertain = journal.prepare_batch("generation-1", "thread-1", [self.first])
        journal.begin(uncertain, self.first)

        self.assertTrue(journal.abandon_uncertain(uncertain))
        replacement = journal.prepare_batch("generation-1", "thread-2", [self.second])
        with self.assertRaises(power_journal.PowerJournalConflictError):
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

    def test_replayable_purge_abandons_paused_work_but_keeps_uncertain_work(self) -> None:
        journal = self.journal()
        prepared = journal.prepare_batch("generation-0", "thread-0", [self.first])
        self.assertTrue(journal.purge_replayable("generation-0"))
        with self.assertRaises(power_journal.PowerJournalConflictError):
            journal.begin(prepared, self.first)

        paused = journal.prepare_batch("generation-1", "thread-1", [self.first, self.second])
        journal.begin(paused, self.first)
        journal.complete(paused, self.first, {"answer": 1})
        journal.begin(paused, self.second)
        journal.suspend(paused, self.second)

        self.assertTrue(journal.purge_replayable("generation-1"))
        self.assertFalse(journal.purge_replayable("generation-1"))

        uncertain = journal.prepare_batch("generation-2", "thread-2", [self.second])
        journal.begin(uncertain, self.second)
        self.assertFalse(journal.purge_replayable("generation-2"))
        with self.assertRaises(power_journal.PowerJournalUncertainError):
            journal.begin(uncertain, self.second)

        completed = journal.prepare_batch("generation-3", "thread-3", [self.first])
        journal.begin(completed, self.first)
        journal.complete(completed, self.first, {"answer": 3})
        self.assertFalse(journal.purge_replayable("generation-3"))
        replay = journal.begin(completed, self.first)
        self.assertFalse(replay.execute)
        self.assertEqual(replay.result, {"answer": 3})

    def test_unsafe_file_and_symlink_paths_are_rejected(self) -> None:
        self.path.parent.mkdir(mode=0o700)
        self.path.write_bytes(b"")
        self.path.chmod(0o644)
        with self.assertRaises(power_journal.PowerJournalCorruptionError):
            power_journal.PowerJournal(self.path)

        self.path.unlink()
        victim = Path(self.temporary.name) / "victim"
        victim.write_bytes(b"unchanged")
        self.path.symlink_to(victim)
        with self.assertRaises(power_journal.PowerJournalCorruptionError):
            power_journal.PowerJournal(self.path)
        self.assertEqual(victim.read_bytes(), b"unchanged")

    def test_new_database_file_and_parent_entry_are_fsynced(self) -> None:
        synced_modes: list[int] = []
        real_fsync = power_journal.os.fsync

        def observe(descriptor: int) -> None:
            synced_modes.append(power_journal.os.fstat(descriptor).st_mode)
            real_fsync(descriptor)

        with mock.patch.object(power_journal.os, "fsync", side_effect=observe):
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

        with self.assertRaises(power_journal.PowerJournalCorruptionError):
            power_journal.PowerJournal(self.path)

    def test_foreign_parent_or_database_owner_fails_closed(self) -> None:
        journal = self.journal()
        journal.close()

        effective_uid = power_journal.os.geteuid()
        with (
            mock.patch.object(power_journal.os, "geteuid", return_value=effective_uid + 1),
            self.assertRaises(power_journal.PowerJournalCorruptionError),
        ):
            power_journal.PowerJournal(self.path)

    def test_scalar_operation_and_json_guards_reject_invalid_values(self) -> None:
        for value in (0, True, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                power_journal._positive_limit(value, "limit")
        with self.assertRaises(power_journal.PowerJournalConflictError):
            power_journal._safe_id("../unsafe", "identifier")
        with self.assertRaises(power_journal.PowerJournalConflictError):
            power_journal._operation(object())
        with self.assertRaises(power_journal.PowerJournalConflictError):
            power_journal._operation(power_journal.Operation("interrupt", "invalid"))

        for value in (
            math.inf,
            {1: "invalid"},
            object(),
        ):
            with self.subTest(value=value), self.assertRaises(power_journal.PowerJournalConflictError):
                power_journal._walk_json(value)
        with self.assertRaisesRegex(power_journal.PowerJournalConflictError, "structure"):
            power_journal._walk_json(None, budget=[0])
        with self.assertRaisesRegex(power_journal.PowerJournalConflictError, "canonical"):
            power_journal._canonical_result("\ud800", 100)
        power_journal._walk_json(1.5)

    def test_file_configuration_schema_and_transaction_failures_are_closed(self) -> None:
        with (
            mock.patch.object(Path, "mkdir", side_effect=OSError("offline")),
            self.assertRaisesRegex(power_journal.PowerJournalCorruptionError, "unavailable"),
        ):
            power_journal.PowerJournal(self.path)

        journal = object.__new__(power_journal.PowerJournal)
        connection = mock.Mock()
        journal._connection = connection
        connection.execute.return_value.fetchone.return_value = ("delete",)
        with self.assertRaisesRegex(power_journal.PowerJournalCorruptionError, "durable mode"):
            journal._configure()

        connection.reset_mock()
        results = [("wal",), (0,), (1,)]
        connection.execute.side_effect = lambda _statement: mock.Mock(fetchone=lambda: results.pop(0))
        with self.assertRaisesRegex(power_journal.PowerJournalCorruptionError, "durability policy"):
            journal._configure()

        connection.execute.side_effect = sqlite3.Error("offline")
        with self.assertRaisesRegex(power_journal.PowerJournalCorruptionError, "integrity"):
            journal._validate_schema()
        with self.assertRaisesRegex(power_journal.PowerJournalError, "transaction"):
            journal._transaction()
        with self.assertRaisesRegex(power_journal.PowerJournalError, "commit"):
            journal._commit()

        journal._closed = True
        with self.assertRaisesRegex(power_journal.PowerJournalError, "closed"):
            journal._ensure_open()

    def test_batch_and_handle_shapes_are_exact(self) -> None:
        journal = self.journal(max_operations=2)
        for operations in ("invalid", object(), (), (self.first, self.second, operation("third", "three"))):
            with self.subTest(operations=operations), self.assertRaises(power_journal.PowerJournalConflictError):
                journal._batch("generation", "thread", operations)
        with self.assertRaisesRegex(power_journal.PowerJournalConflictError, "repeats"):
            journal._batch("generation", "thread", (self.first, self.first))

        valid = journal._batch("generation", "thread", (self.first,))
        invalid_handles = (
            object(),
            power_journal.Batch(valid.generation, "invalid", valid.operations),
            power_journal.Batch(valid.generation, valid.fingerprint, ()),
            power_journal.Batch(valid.generation, valid.fingerprint, (self.first, self.first)),
        )
        for handle in invalid_handles:
            with self.subTest(handle=handle), self.assertRaises(power_journal.PowerJournalConflictError):
                journal._validate_handle(handle)

    def test_internal_read_failures_are_never_mistaken_for_conflicts(self) -> None:
        journal = self.journal()
        batch = journal._batch("generation", "thread", (self.first,))
        connection = journal._connection
        journal._connection = mock.Mock()
        journal._connection.execute.side_effect = sqlite3.Error("offline")
        try:
            with self.assertRaisesRegex(power_journal.PowerJournalCorruptionError, "batch could not be read"):
                journal._load_batch(batch)
            journal._validated_batches[(batch.generation, batch.fingerprint)] = {
                self.first.interrupt_id: (0, self.first.fingerprint)
            }
            with self.assertRaisesRegex(power_journal.PowerJournalCorruptionError, "operation could not be read"):
                journal._load_operation(batch, self.first)
        finally:
            journal._connection = connection

    def test_constructor_schema_and_context_manager_edges_are_closed(self) -> None:
        with (
            mock.patch.object(
                power_journal.sqlite3,
                "connect",
                side_effect=power_journal.PowerJournalCorruptionError("invalid"),
            ),
            self.assertRaises(power_journal.PowerJournalCorruptionError),
        ):
            power_journal.PowerJournal(self.path)
        with (
            mock.patch.object(
                power_journal.PowerJournal,
                "_configure",
                side_effect=power_journal.PowerJournalCorruptionError("invalid"),
            ),
            self.assertRaises(power_journal.PowerJournalCorruptionError),
        ):
            power_journal.PowerJournal(self.path)

        journal = self.journal()
        journal._connection.execute("PRAGMA user_version = 2")
        journal.close()
        with self.assertRaisesRegex(power_journal.PowerJournalCorruptionError, "schema"):
            power_journal.PowerJournal(self.path)

        replacement = Path(self.temporary.name) / "context" / "journal.sqlite3"
        with power_journal.PowerJournal(replacement) as opened:
            self.assertIsInstance(opened, power_journal.PowerJournal)
        with self.assertRaisesRegex(power_journal.PowerJournalError, "closed"):
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
            with self.subTest(method=method.__name__), self.assertRaises(power_journal.PowerJournalConflictError):
                method(*arguments)

        with self.assertRaisesRegex(power_journal.PowerJournalConflictError, "not executing"):
            journal.complete(batch, self.first, {"ok": True})
        journal.begin(batch, self.first)
        with (
            mock.patch.object(
                journal,
                "_load_operation",
                return_value=(0, self.first.interrupt_id, self.first.fingerprint, "prepared", b"result"),
            ),
            self.assertRaisesRegex(power_journal.PowerJournalCorruptionError, "durable state"),
        ):
            journal.begin(batch, self.first)

    def test_cached_result_guards_reject_invalid_raw_and_cached_bytes(self) -> None:
        journal = self.journal()
        batch = journal._batch("generation", "thread", (self.first,))
        for raw in ("invalid", b"x" * (journal.max_result_bytes + 1)):
            with self.subTest(raw=type(raw)), self.assertRaises(power_journal.PowerJournalCorruptionError):
                journal._decode_result(raw)
            with self.subTest(raw=type(raw)), self.assertRaises(power_journal.PowerJournalCorruptionError):
                journal._validated_result(batch, self.first.interrupt_id, raw)

        invalid = b"not-json"
        journal._validated_results[(batch.generation, self.first.interrupt_id)] = hashlib.sha256(invalid).digest()
        with self.assertRaises(power_journal.PowerJournalCorruptionError):
            journal._validated_result(batch, self.first.interrupt_id, invalid)

    def test_sqlite_write_failures_are_translated_for_every_transition(self) -> None:
        journal = self.journal()
        self.assert_sql_failure(
            journal,
            "SELECT fingerprint FROM batches",
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
            "UPDATE operations SET state = 'completed'",
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
            with self.assertRaisesRegex(power_journal.PowerJournalCorruptionError, "capacity"):
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
            "SELECT state FROM operations",
            lambda: journal.purge_replayable("generation"),
            "replayable",
        )

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

        def foreign_database(path: Path) -> power_journal.os.stat_result:
            metadata = real_lstat(path)
            if path == self.path:
                fields = list(metadata)
                fields[4] = metadata.st_uid + 1
                return power_journal.os.stat_result(fields)
            return metadata

        with (
            mock.patch.object(Path, "lstat", autospec=True, side_effect=foreign_database),
            self.assertRaises(power_journal.PowerJournalCorruptionError),
        ):
            power_journal.PowerJournal(self.path)


if __name__ == "__main__":
    unittest.main()
