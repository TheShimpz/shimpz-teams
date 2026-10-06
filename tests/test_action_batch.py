"""Action batch journal fingerprints, replay, and Stored Input generation binding."""

from __future__ import annotations

import dataclasses
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
sys.path.insert(0, str(Path(__file__).resolve().parent))


from test_action_rpc_exchange import rpc_strategy

from action import dispatch as action_dispatch
from action import execution as action_execution
from action import human as action_human
from action import journal as action_journal
from inference import client as brain_runtime_client
from tests import human_request_fixtures


def _action_batch(journal, binding, execute, preflight, *, generation="generation-1", thread="thread-1", **options):
    """An ActionBatch over the single Assistant ``binding``, identified by its container id and image."""
    return action_execution.ActionBatch(
        journal,
        generation,
        thread,
        {"assistant": binding},
        action_execution.ActionBatchStrategy(
            lambda item: (item.container_id, item.spec.image), execute, preflight, **options
        ),
    )


class ActionBatchTests(unittest.TestCase):
    def test_both_action_batch_adapters_reject_the_same_generation_drift(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "lookup", {"query": "safe"})
        generation = [1]
        execute = mock.Mock(return_value={"ok": True})
        image = "example.invalid/assistant@sha256:" + "a" * 64
        bindings = (
            (
                SimpleNamespace(container=SimpleNamespace(id="container-1"), image=image),
                lambda item: (item.container.id, item.image),
            ),
            (
                SimpleNamespace(container_id="container-1", spec=SimpleNamespace(image=image)),
                lambda item: (item.container_id, item.spec.image),
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            for index, (binding, identity) in enumerate(bindings):
                with self.subTest(adapter=index):
                    journal = action_journal.ActionJournal(Path(directory) / f"journal-{index}.sqlite3")
                    self.addCleanup(journal.close)
                    batch = action_execution.ActionBatch(
                        journal,
                        "generation-1",
                        "thread-1",
                        {"assistant": binding},
                        action_execution.ActionBatchStrategy(
                            identity,
                            execute,
                            lambda _request: None,
                            lambda _request: (("secret", generation[0]),),
                        ),
                    )
                    generation[0] = 1
                    batch.prepare((request,))
                    generation[0] = 2
                    with self.assertRaisesRegex(
                        action_journal.ActionJournalConflictError,
                        "Action credential generation changed",
                    ):
                        batch.invoke(request)
        execute.assert_not_called()

    def test_action_batch_passes_only_the_invoke_time_preflight_evidence(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "lookup", {"query": "safe"})
        binding = SimpleNamespace(container_id="container-1", spec=SimpleNamespace(image="example.invalid/image"))
        evidence: list[dict[str, int]] = []

        def preflight(_request):
            current = {"sequence": len(evidence) + 1}
            evidence.append(current)
            return current

        execute = mock.Mock(return_value={"ok": True})
        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            batch = _action_batch(journal, binding, execute, preflight)

            batch.prepare((request,))
            result = batch.invoke(request)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(evidence, [{"sequence": 1}, {"sequence": 2}])
        (called_request, called_evidence, operation_id), _kwargs = execute.call_args
        self.assertEqual((called_request, called_evidence), (request, evidence[1]))
        self.assertTrue(action_journal.valid_operation_id(operation_id))

    def test_action_batch_excuses_only_a_stored_input_newly_sealed_by_a_sibling(self) -> None:
        first = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "search", {"q": "a"})
        second = brain_runtime_client.ActionRequest("interrupt-2", "assistant", "search", {"q": "b"})
        sibling = action_execution.stored_input_origin(first)
        binding = SimpleNamespace(container_id="container", spec=SimpleNamespace(image="image"))
        store: dict[str, tuple[int, str]] = {}

        def generations(request, origins):
            return tuple(
                (stored_input_id, generation)
                for stored_input_id, (generation, origin) in store.items()
                if origin not in origins
            )

        def run(initial: dict[str, tuple[int, str]], sealed: tuple[int, str] | None) -> object:
            store.clear()
            store.update(initial)
            with tempfile.TemporaryDirectory() as directory:
                journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
                self.addCleanup(journal.close)
                batch = _action_batch(
                    journal,
                    binding,
                    lambda _request, _evidence, _operation_id: {"ok": True},
                    lambda _request: None,
                    generation="generation",
                    thread="thread",
                    stored_input_generations=generations,
                )
                batch.prepare((first, second))
                batch.invoke(first)
                if sealed is None:
                    store.pop("key", None)
                else:
                    store["key"] = sealed
                return batch.invoke(second)

        self.assertEqual(run({}, (1, sibling)), {"ok": True})
        self.assertEqual(run({"key": (1, sibling)}, (1, sibling)), {"ok": True})
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "generation changed"):
            run({"key": (1, sibling)}, (2, sibling))
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "generation changed"):
            run({}, (1, "c" * 64))
        own = action_execution.stored_input_origin(second)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "generation changed"):
            run({"key": (1, own)}, (2, sibling))
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "generation changed"):
            run({"key": (1, own)}, (2, own))
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "generation changed"):
            run({"key": (1, own)}, None)
        self.assertEqual(run({"key": (1, own)}, (1, own)), {"ok": True})

    def test_action_batch_rejects_unprepared_duplicate_and_changed_delivery(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "lookup", {})
        unknown = brain_runtime_client.ActionRequest("interrupt-2", "assistant", "lookup", {})
        binding = SimpleNamespace(container_id="container", spec=SimpleNamespace(image="image"))
        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            batch = _action_batch(
                journal,
                binding,
                lambda _request, _evidence, _operation_id: {"ok": True},
                lambda _request: None,
                generation="generation",
                thread="thread",
            )
            with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "not prepared"):
                batch.invoke(request)
            with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "not prepared"):
                batch.delivered((request,))
            with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "unavailable"):
                batch.prepare((brain_runtime_client.ActionRequest("interrupt", "missing", "lookup", {}),))
            batch.prepare((request,))
            with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "already prepared"):
                batch.prepare((request,))
            with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "operation is not prepared"):
                batch.invoke(unknown)
            with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "delivery batch changed"):
                batch.delivered((unknown,))

    def test_valid_human_suspension_is_the_only_retryable_execution(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "lookup", {"query": "safe"})
        binding = SimpleNamespace(container_id="container-1", spec=SimpleNamespace(image="example.invalid/image"))
        suspension = action_human.HumanRequestSuspensionError(
            human_request_fixtures.request("approval", title="Continue", description="Continue the reviewed operation.")
        )

        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            execute = mock.Mock(side_effect=[suspension, {"ok": True}])
            batch = _action_batch(journal, binding, execute, lambda _request: None)
            batch.prepare((request,))

            with self.assertRaises(action_human.HumanRequestSuspensionError):
                batch.invoke(request)
            self.assertEqual(batch.invoke(request), {"ok": True})

    def test_terminal_abandonment_resets_a_lazy_journal_only_after_exact_deletion(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "lookup", {})
        binding = SimpleNamespace(container_id="container-1", spec=SimpleNamespace(image="example.invalid/image"))
        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            journal_source = mock.Mock(return_value=journal)
            batch = _action_batch(
                journal_source,
                binding,
                lambda _request, _evidence, _operation_id: (_ for _ in ()).throw(RuntimeError("terminal failure")),
                lambda _request: None,
            )
            batch.prepare((request,))
            with self.assertRaisesRegex(RuntimeError, "terminal failure"):
                batch.invoke(request)

            with mock.patch.object(journal, "abandon_uncertain", return_value=False):
                self.assertFalse(batch.terminate())
            self.assertTrue(batch.terminate())
            self.assertFalse(batch.terminate())

        journal_source.assert_called_once_with()

    def test_a_completed_undelivered_batch_ends_and_admits_a_fresh_batch(self) -> None:
        """A Brain failure or Stop after every Action completed must not strand the generation."""
        first = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "lookup", {})
        fresh = brain_runtime_client.ActionRequest("interrupt-2", "assistant", "lookup", {})
        binding = SimpleNamespace(container_id="container-1", spec=SimpleNamespace(image="example.invalid/image"))
        execute = mock.Mock(return_value={"ok": True})
        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)

            def attempt() -> action_execution.ActionBatch:
                return _action_batch(journal, binding, execute, lambda _request: None)

            batch = attempt()
            self.assertFalse(batch.terminate())
            batch.prepare((first,))
            self.assertEqual(batch.invoke(first), {"ok": True})
            with mock.patch.object(journal, "end", return_value=False):
                self.assertFalse(batch.terminate())
            self.assertTrue(batch.terminate())
            self.assertFalse(batch.terminate())

            replay = attempt()
            replay.prepare((first,))
            self.assertEqual(replay.invoke(first), {"ok": True})
            self.assertTrue(replay.terminate())
            repeated = attempt()
            with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "ended Action interrupt"):
                repeated.prepare((fresh, first))
            following = attempt()
            following.prepare((fresh,))
            self.assertEqual(following.invoke(fresh), {"ok": True})
            following.delivered((fresh,))
        self.assertEqual(execute.call_count, 2)


class HeldActionBatchTests(unittest.TestCase):
    """A Routine run's batch holds an uncertain outcome for recovery; it never abandons it (ADR-0092)."""

    def test_an_uncertain_batch_is_held_and_named_never_abandoned(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "write", {"value": "x"})
        binding = SimpleNamespace(container_id="container-1", spec=SimpleNamespace(image="example.invalid/image"))

        def failing(_request, _evidence, _operation_id):
            raise RuntimeError("the Assistant failed mid-write")

        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            batch = action_execution.HeldActionBatch(
                journal,
                "net:routine:" + "f" * 32,
                "thread-1",
                {"assistant": binding},
                action_execution.ActionBatchStrategy(
                    lambda item: (item.container_id, item.spec.image), failing, lambda _request: None
                ),
            )
            self.assertFalse(batch.terminate())
            self.assertEqual(batch.held, "")
            batch.prepare((request,))
            with self.assertRaises(RuntimeError):
                batch.invoke(request)
            self.assertFalse(batch.terminate())
            self.assertRegex(batch.held, r"\A[0-9a-f]{64}\Z")
            # The journal still holds the uncertain batch: fresh-turn cleanup refuses to end it.
            self.assertFalse(journal.end_settled("net:routine:" + "f" * 32))
            with closing(sqlite3.connect(journal.path)) as connection:
                rows = connection.execute("SELECT generation FROM batches").fetchall()
            self.assertEqual(rows, [("net:routine:" + "f" * 32,)])


def _rpc_strategy(api: object) -> action_execution.RpcExchangeStrategy:
    return rpc_strategy(api, workdir="/srv", timeout=0.2, cancelled=lambda _exc: None)


class NeverDispatchedTests(unittest.TestCase):
    """Team's own pre-dispatch refusal settles only that attempt as never run; ambiguity stays uncertain."""

    def _batch(self, journal: action_journal.ActionJournal, execute) -> action_execution.ActionBatch:
        binding = SimpleNamespace(container_id="container-1", spec=SimpleNamespace(image="example.invalid/image"))
        return action_execution.ActionBatch(
            journal,
            "generation-1",
            "thread-1",
            {"assistant": binding},
            action_execution.ActionBatchStrategy(
                lambda item: (item.container_id, item.spec.image), execute, lambda _request: None
            ),
        )

    def test_a_saturated_docker_pool_leaves_the_attempt_prepared_not_uncertain(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "write", {"value": "x"})
        api = mock.Mock()
        saturated = mock.Mock(acquire=mock.Mock(return_value=False))

        def wrapped(_request, _evidence, _operation_id):
            try:
                action_execution.rpc_exchange("container", ["command"], b"request", _rpc_strategy(api))
            except action_execution.RpcExchangeError as exc:
                # Each profile raises its own public problem from the exchange error.
                raise RuntimeError("Assistant Action timed out") from exc

        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            batch = self._batch(journal, wrapped)
            batch.prepare((request,))
            with (
                mock.patch.object(action_dispatch, "_DOCKER_CALL_SLOTS", saturated),
                self.assertRaises(RuntimeError) as refused,
            ):
                batch.invoke(request)
            self.assertTrue(action_dispatch.never_dispatched(refused.exception))
            api.exec_create.assert_not_called()
            # Nothing ran: no uncertain outcome remains, the turn ends the batch, and the same interrupt may run.
            self.assertIsNone(journal.uncertain_fingerprint("generation-1"))
            self.assertTrue(batch.terminate())

    def test_an_exit_inspection_refused_after_the_exchange_stays_uncertain(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "write", {"value": "x"})
        api = SimpleNamespace(
            exec_create=lambda *_a, **_k: {"Id": "exec"},
            exec_start=lambda *_a, **_k: SimpleNamespace(_sock=object()),
            exec_inspect=mock.Mock(),
        )
        real = action_dispatch.bounded_call
        calls = []

        def second_refused(call, deadline, *stopped):
            calls.append(call)
            if len(calls) > 1:
                raise action_dispatch.DispatchRefusedError("Docker capacity stayed saturated")
            return real(call, deadline, *stopped)

        def execute(_request, _evidence, _operation_id):
            try:
                action_execution.rpc_exchange("container", ["command"], b"request", _rpc_strategy(api))
            except action_execution.RpcExchangeError as exc:
                raise RuntimeError("Assistant Action timed out") from exc

        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            batch = self._batch(journal, execute)
            batch.prepare((request,))
            with (
                mock.patch.object(action_dispatch, "bounded_call", side_effect=second_refused),
                mock.patch.object(action_execution, "exchange_rpc_frames", return_value=(b"{}", b"")),
                self.assertRaises(RuntimeError) as uncertain,
            ):
                batch.invoke(request)
            self.assertFalse(action_dispatch.never_dispatched(uncertain.exception))
            self.assertEqual(
                (uncertain.exception.__cause__.kind, uncertain.exception.__cause__.condition),
                ("timeout", "exit-unavailable"),
            )
            api.exec_inspect.assert_not_called()
            self.assertIsNotNone(journal.uncertain_fingerprint("generation-1"))

    def test_a_turn_stopped_while_waiting_for_capacity_never_ran_its_attempt(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "assistant", "write", {"value": "x"})
        api = mock.Mock()
        saturated = mock.Mock(acquire=mock.Mock(side_effect=lambda timeout: time.sleep(timeout) or False))
        stop = threading.Event()

        def hosted_cancelled(exc):
            # The hosted profile raises its stopped problem from the refusal it observed.
            raise RuntimeError("brain turn stopped") from exc

        def execute(_request, _evidence, _operation_id):
            strategy = dataclasses.replace(_rpc_strategy(api), timeout=30, cancelled=hosted_cancelled)
            threading.Timer(0.2, stop.set).start()
            return action_execution.rpc_exchange("container", ["command"], b"request", strategy)

        binding = SimpleNamespace(container_id="container-1", spec=SimpleNamespace(image="example.invalid/image"))
        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            batch = _action_batch(journal, binding, execute, lambda _request: None, stopped=stop.is_set)
            batch.prepare((request,))
            started = time.monotonic()
            with (
                mock.patch.object(action_dispatch, "_DOCKER_CALL_SLOTS", saturated),
                self.assertRaisesRegex(RuntimeError, "brain turn stopped"),
            ):
                batch.invoke(request)
            self.assertLess(time.monotonic() - started, 2.0)
            api.exec_create.assert_not_called()
            self.assertIsNone(journal.uncertain_fingerprint("generation-1"))
            self.assertTrue(batch.terminate())

    def test_a_refusal_buried_beyond_the_bounded_cause_chain_is_not_trusted(self) -> None:
        current: BaseException = action_dispatch.DispatchRefusedError("refused")
        for depth in range(8):
            wrapper = RuntimeError(f"layer {depth}")
            wrapper.__cause__ = current
            current = wrapper
        self.assertFalse(action_dispatch.never_dispatched(current))
        self.assertTrue(action_dispatch.never_dispatched(current.__cause__))

    def test_only_an_executing_attempt_returns_to_prepared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            operation = action_journal.Operation("action-1", "b" * 64)
            batch = journal.prepare_batch("generation", "thread", (operation,))
            with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "was not executing"):
                journal.not_dispatched(batch, operation)
            journal.begin(batch, operation)
            journal.not_dispatched(batch, operation)
            self.assertIsNone(journal.uncertain_fingerprint("generation"))
            self.assertTrue(journal.begin(batch, operation).execute)


class UncertainFingerprintTests(unittest.TestCase):
    def test_only_a_batch_with_an_executing_operation_is_uncertain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            first = action_journal.Operation("action-1", "b" * 64)
            second = action_journal.Operation("action-2", "c" * 64)
            batch = journal.prepare_batch("generation", "thread", (first, second))
            self.assertIsNone(journal.uncertain_fingerprint("generation"))
            self.assertIsNone(journal.uncertain_fingerprint("absent"))
            journal.begin(batch, first)
            journal.complete(batch, first, {"ok": True})
            self.assertIsNone(journal.uncertain_fingerprint("generation"))
            journal.begin(batch, second)
            self.assertEqual(journal.uncertain_fingerprint("generation"), batch.fingerprint)
            with (
                mock.patch.object(journal, "_connection", mock.Mock(execute=mock.Mock(side_effect=sqlite3.Error))),
                self.assertRaises(action_journal.ActionJournalError),
            ):
                journal.uncertain_fingerprint("generation")
