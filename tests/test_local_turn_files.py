"""The files a Local turn references, and its file delivery's admission outcomes (ADR-0093)."""

import sys
import tempfile
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
from tests import storage_fixtures

FILE_ID = "0123456789abcdef0123456789abcdef"
TURN_TOKEN = "-".join(("turn", "token"))
UPLOAD = ActionSpec(
    "Upload a document.",
    {"type": "object"},
    {"type": "object"},
    human_requests=("approval",),
    input_files=("document",),
)


def _service() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        storage=mock.Mock(), _raise_storage_problem=local_app.LocalController._raise_storage_problem
    )


class LocalTurnFileTests(unittest.TestCase):
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
            self.assertEqual(storage_fixtures.referenced(restarted, "team_1"), frozenset({first, second}))
            local_attachments.turn_completed(service, "team_1", [])
            self.assertEqual(storage_fixtures.referenced(restarted, "team_1"), frozenset())

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
            self.assertEqual(storage_fixtures.referenced(storage, "team_1"), frozenset({earlier}))
            service._turn_completed.assert_not_called()
            clock[0] += team_storage.UNREFERENCED_GRACE_SECONDS
            self.assertEqual([item["id"] for item in storage.list("team_1")["files"]], [earlier])
            # Nothing newly referenced releases nothing, and a release that fails leaves the turn's failure standing.
            service.storage = mock.Mock(release=mock.Mock(side_effect=team_storage.StorageError("unavailable")))
            local_attachments.turn_failed(service, "team_1", ())
            service.storage.release.assert_not_called()
            local_attachments.turn_failed(service, "team_1", (earlier,))
            service.storage.release.assert_called_once_with("team_1", (earlier,))

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
