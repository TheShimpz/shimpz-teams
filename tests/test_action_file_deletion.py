"""A Team file's deletion linearized with its delivery, pending use, and Brain references (ADR-0093)."""

import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from assistant.spec import ActionSpec
from inference import client as brain_runtime_client
from local import app as local_app
from local.chat import attachments as local_attachments
from local.chat import segment as local_segment
from storage import files as team_storage

FILE_ID = "0123456789abcdef0123456789abcdef"
TURN_TOKEN = "-".join(("turn", "token"))
OTHER_ID = "fedcba9876543210fedcba9876543210"
NETWORK = types.SimpleNamespace(id="a" * 64)
UPLOAD = ActionSpec(
    "Upload a document.",
    {"type": "object"},
    {"type": "object"},
    human_requests=("approval",),
    input_files=("document",),
)


def _challenges(*file_ids: str) -> mock.Mock:
    store = mock.Mock()
    store.current.return_value = (
        types.SimpleNamespace(payload=types.SimpleNamespace(file_ids=file_ids)) if file_ids else None
    )
    return store


def _storage(referenced: frozenset[str] = frozenset()) -> mock.Mock:
    """The Team's file references as storage records them; only what forget_file reads and writes."""
    return mock.Mock(referenced=mock.Mock(return_value=referenced))


def _service(pending: tuple[str, ...] = (), referenced: frozenset[str] = frozenset()) -> types.SimpleNamespace:
    service = types.SimpleNamespace(
        space_id="local-space",
        _active_chat_guard=threading.Lock(),
        storage=_storage(referenced),
        _raise_storage_problem=local_app.LocalController._raise_storage_problem,
        _routine_holders={},
        human_challenges=_challenges(*pending),
        integration_challenges=_challenges(),
        oauth_pkce=mock.Mock(),
        chat_continuations=mock.Mock(current=mock.Mock(return_value=None)),
        _delete_chat_continuation=mock.Mock(),
        action_state=mock.Mock(),
        brain_runtime=mock.Mock(),
    )
    locks: dict[str, threading.Lock] = {}
    service._chat_lock = lambda team_id: locks.setdefault(team_id, threading.Lock())
    return service


class LocalDeletionTests(unittest.TestCase):
    def test_a_paused_turn_that_selected_the_file_is_cancelled_and_the_brain_thread_purged(self) -> None:
        service = _service((FILE_ID,), frozenset({FILE_ID}))
        local_attachments.forget_file(service, "team_1", FILE_ID, NETWORK)
        service.human_challenges.cancel_team.assert_called_once_with("team_1")
        service.integration_challenges.cancel_team.assert_called_once_with("team_1")
        service._delete_chat_continuation.assert_called_once_with("team_1")
        # Settled state ends; uncertain Action evidence is kept, never purged.
        service.action_state.end_settled.assert_called_once_with(NETWORK.id)
        service.action_state.purge.assert_not_called()
        service.brain_runtime.delete_thread.assert_called_once()
        # The purged thread references nothing more, so every file it referenced starts its grace period.
        service.storage.settle.assert_called_once_with("team_1", ())

    def test_an_unrelated_file_leaves_a_referencing_thread_and_its_paused_turn_alone(self) -> None:
        service = _service((OTHER_ID,), frozenset({OTHER_ID}))
        local_attachments.forget_file(service, "team_1", FILE_ID, NETWORK)
        service.human_challenges.cancel_team.assert_not_called()
        service.brain_runtime.delete_thread.assert_not_called()
        service.storage.settle.assert_not_called()

    def test_a_referenced_file_purges_the_thread_and_any_paused_turn_with_it(self) -> None:
        service = _service((OTHER_ID,), frozenset({FILE_ID}))
        local_attachments.forget_file(service, "team_1", FILE_ID, NETWORK)
        service.human_challenges.cancel_team.assert_called_once_with("team_1")
        service.brain_runtime.delete_thread.assert_called_once()
        unreadable = _service()
        unreadable.chat_continuations.current.return_value = object()
        local_attachments.forget_file(unreadable, "team_1", FILE_ID, NETWORK)
        unreadable._delete_chat_continuation.assert_called_once_with("team_1")

    def test_unavailable_action_state_refuses_the_deletion(self) -> None:
        service = _service((FILE_ID,), frozenset({FILE_ID}))
        service.action_state.end_settled.side_effect = local_attachments.action_journal.ActionJournalError("down")
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_attachments.forget_file(service, "team_1", FILE_ID, NETWORK)
        self.assertEqual(caught.exception.code, "action-state-unavailable")
        service.brain_runtime.delete_thread.assert_not_called()

    def test_a_brain_failure_refuses_the_deletion(self) -> None:
        service = _service(referenced=frozenset({FILE_ID}))
        service.brain_runtime.delete_thread.side_effect = brain_runtime_client.BrainRuntimeError("down")
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_attachments.forget_file(service, "team_1", FILE_ID, NETWORK)
        self.assertEqual(caught.exception.code, "brain-runtime-failed")
        service.storage.settle.assert_not_called()

    def test_turn_references_are_recorded_in_storage_and_survive_a_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "teams"
            service = _service()
            service.storage = team_storage.TeamStorage(root)
            first = service.storage.put("team_1", "a.txt", b"a", "text/plain")["id"]
            second = service.storage.put("team_1", "b.txt", b"b", "text/plain")["id"]
            local_attachments.turn_completed(service, "team_1", [second])
            local_attachments.turn_started(service, "team_1", [first])
            # A restarted controller reads the same references: nothing is unknown after a restart.
            restarted = team_storage.TeamStorage(root)
            self.assertEqual(restarted.referenced("team_1"), frozenset({first, second}))
            local_attachments.turn_completed(service, "team_1", [])
            self.assertEqual(restarted.referenced("team_1"), frozenset())

    def test_a_failed_turn_releases_only_the_files_it_newly_referenced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            clock = [1_000_000.0]
            storage = team_storage.TeamStorage(Path(directory) / "teams", clock=lambda: clock[0])
            earlier = storage.put("team_1", "earlier.txt", b"earlier", "text/plain")["id"]
            failing = storage.put("team_1", "failing.txt", b"failing", "text/plain")["id"]
            storage.reference("team_1", [earlier])
            service = _service()
            service.storage = storage
            service._turn_started = lambda team_id, file_ids: local_attachments.turn_started(service, team_id, file_ids)
            service._turn_failed = lambda team_id, added: local_attachments.turn_failed(service, team_id, added)
            service._turn_completed = mock.Mock()
            service._run_chat_segment_with_metadata = mock.Mock(side_effect=RuntimeError("helper unavailable"))
            request = types.SimpleNamespace(
                team_id="team_1", file_ids=[earlier, failing], routine=None, continuation=None
            )
            with self.assertRaisesRegex(RuntimeError, "helper unavailable"):
                local_segment._run_chat_segment(service, request)
            # The earlier turn's file stays referenced; the failed turn's own file starts its grace and is collected.
            self.assertEqual(storage.referenced("team_1"), frozenset({earlier}))
            service._turn_completed.assert_not_called()
            clock[0] += team_storage.UNREFERENCED_GRACE_SECONDS
            self.assertEqual([item["id"] for item in storage.list("team_1")["files"]], [earlier])
            # Nothing newly referenced, or a release that fails, changes nothing.
            local_attachments.turn_failed(service, "team_1", ())
            service.storage = mock.Mock(release=mock.Mock(side_effect=team_storage.StorageError("unavailable")))
            local_attachments.turn_failed(service, "team_1", (earlier,))

    def test_a_turn_naming_a_collected_file_fails_before_its_brain_start(self) -> None:
        service = _service()
        service.storage.reference.side_effect = team_storage.StorageNotFoundError("file not found")
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_attachments.turn_started(service, "team_1", [FILE_ID])
        self.assertEqual(caught.exception.code, "file-not-found")
        service.storage.reference.side_effect = team_storage.StorageError("unsafe")
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_attachments.turn_started(service, "team_1", [FILE_ID])
        self.assertEqual(caught.exception.code, "storage-safety-failed")

    def test_a_failed_release_keeps_the_completed_turn_and_every_reference(self) -> None:
        service = _service()
        service.storage.settle.side_effect = team_storage.StorageError("unavailable")
        local_attachments.turn_completed(service, "team_1", [])
        service.storage.settle.assert_called_once_with("team_1", [])

    def test_a_turn_holding_the_slot_refuses_the_deletion_until_it_ends(self) -> None:
        service = _service()
        lock = service._chat_lock("team_1")
        lock.acquire()
        with self.assertRaises(local_app.ApiProblem) as caught, local_attachments.deletion_slot(service, "team_1"):
            pass
        self.assertEqual(caught.exception.code, "chat-active")
        service._routine_holders["team_1"] = "routine"
        with self.assertRaises(local_app.ApiProblem) as caught, local_attachments.deletion_slot(service, "team_1"):
            pass
        self.assertEqual(caught.exception.code, "routine-active")
        lock.release()
        with local_attachments.deletion_slot(service, "team_1"):
            self.assertTrue(lock.locked())
        self.assertFalse(lock.locked())

    def test_the_controller_forgets_the_file_before_removing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = team_storage.TeamStorage(Path(directory) / "teams")
            stored = storage.put("team_1", "a.pdf", b"%PDF", "application/pdf")
            order: list[str] = []
            controller = object.__new__(local_app.LocalController)
            controller._locks = tuple(threading.RLock() for _ in range(64))
            controller.storage = storage
            controller.assistant_lifecycle = types.SimpleNamespace(_network=lambda _team_id: NETWORK)
            service = _service()
            service._file_deletion_slot = lambda team_id: local_attachments.deletion_slot(service, team_id)
            service._forget_file = lambda _team_id, file_id, _network: order.append(file_id)
            controller.chat_turn_service = service
            controller._raise_storage_problem = local_app.LocalController._raise_storage_problem
            self.assertTrue(controller.delete_file("team_1", stored["id"])["deleted"])
            # A repeated deletion is the same successful outcome, reported as absent, after the same cleanup.
            again = controller.delete_file("team_1", stored["id"])
            self.assertEqual((again["deleted"], order), (False, [stored["id"], stored["id"]]))
            with self.assertRaises(local_app.ApiProblem) as caught:
                controller.delete_file("team_1", "not-a-file-id")
            self.assertEqual((caught.exception.code, len(order)), ("invalid-file", 2))
            service._forget_file = mock.Mock(
                side_effect=local_app.ApiProblem(503, "unavailable", code="brain-runtime-failed")
            )
            kept = storage.put("team_1", "b.pdf", b"%PDF", "application/pdf")
            with self.assertRaises(local_app.ApiProblem):
                controller.delete_file("team_1", kept["id"])
            self.assertEqual(len(storage.list("team_1")["files"]), 1)


class DeliveryAdmissionMappingTests(unittest.TestCase):
    """A refused or stopped slot wait reaches Local as its own public outcome, before anything is journaled."""

    def _admit(self, admit, error: BaseException) -> None:
        refused = mock.MagicMock()
        refused.__enter__.side_effect = error
        with mock.patch.object(local_segment.action_files, "admitted", return_value=refused), admit():
            pass

    def test_local_maps_a_stop_and_a_busy_slot(self) -> None:
        bindings = {"docs": types.SimpleNamespace(spec=types.SimpleNamespace(actions={"upload": UPLOAD}))}
        request = types.SimpleNamespace(transcripts=(), token=TURN_TOKEN)
        action = brain_runtime_client.ActionRequest("i", "docs", "upload", {"document": FILE_ID})
        subject = types.SimpleNamespace(_chat_cancelled=lambda _token: False)

        def admit():
            return local_segment._admitted_delivery(subject, request, bindings, action, object())

        with self.assertRaises(local_segment.chat_orchestrator.ChatStoppedError):
            self._admit(admit, local_segment.action_files.FileRpcCancelledError("stopped"))
        with self.assertRaises(local_app.ApiProblem) as caught:
            self._admit(admit, local_segment.action_files.FileRpcBusyError("busy"))
        self.assertEqual(caught.exception.code, "assistant-file-busy")


if __name__ == "__main__":
    unittest.main()
