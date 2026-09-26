"""Action batch journal fingerprints, replay, and Stored Input generation binding."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
sys.path.insert(0, str(Path(__file__).resolve().parent))


from action import execution as action_execution
from action import human as action_human
from action import journal as action_journal
from inference import client as brain_runtime_client


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
            batch = action_execution.ActionBatch(
                journal,
                "generation-1",
                "thread-1",
                {"assistant": binding},
                action_execution.ActionBatchStrategy(
                    lambda item: (item.container_id, item.spec.image),
                    execute,
                    preflight,
                ),
            )

            batch.prepare((request,))
            result = batch.invoke(request)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(evidence, [{"sequence": 1}, {"sequence": 2}])
        execute.assert_called_once_with(request, evidence[1])

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
                batch = action_execution.ActionBatch(
                    journal,
                    "generation",
                    "thread",
                    {"assistant": binding},
                    action_execution.ActionBatchStrategy(
                        lambda item: (item.container_id, item.spec.image),
                        lambda _request, _evidence: {"ok": True},
                        lambda _request: None,
                        stored_input_generations=generations,
                    ),
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
            batch = action_execution.ActionBatch(
                journal,
                "generation",
                "thread",
                {"assistant": binding},
                action_execution.ActionBatchStrategy(
                    lambda item: (item.container_id, item.spec.image),
                    lambda _request, _evidence: {"ok": True},
                    lambda _request: None,
                ),
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
        descriptor = {
            "kind": "approval",
            "ordinal": 0,
            "title": "Continue",
            "description": "Continue the reviewed operation.",
        }
        descriptor["fingerprint"] = action_human._fingerprint(descriptor)
        suspension = action_human.HumanRequestSuspensionError(
            action_human.validate_request(descriptor, ("approval",)),
        )

        with tempfile.TemporaryDirectory() as directory:
            journal = action_journal.ActionJournal(Path(directory) / "journal.sqlite3")
            self.addCleanup(journal.close)
            execute = mock.Mock(side_effect=[suspension, {"ok": True}])
            batch = action_execution.ActionBatch(
                journal,
                "generation-1",
                "thread-1",
                {"assistant": binding},
                action_execution.ActionBatchStrategy(
                    lambda item: (item.container_id, item.spec.image),
                    execute,
                    lambda _request: None,
                ),
            )
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
            batch = action_execution.ActionBatch(
                journal_source,
                "generation-1",
                "thread-1",
                {"assistant": binding},
                action_execution.ActionBatchStrategy(
                    lambda item: (item.container_id, item.spec.image),
                    lambda _request, _evidence: (_ for _ in ()).throw(RuntimeError("terminal failure")),
                    lambda _request: None,
                ),
            )
            batch.prepare((request,))
            with self.assertRaisesRegex(RuntimeError, "terminal failure"):
                batch.invoke(request)

            with mock.patch.object(journal, "abandon_uncertain", return_value=False):
                self.assertFalse(batch.abandon_uncertain())
            self.assertTrue(batch.abandon_uncertain())
            self.assertFalse(batch.abandon_uncertain())

        journal_source.assert_called_once_with()
