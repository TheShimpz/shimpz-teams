"""Local Action file delivery: withheld first, bytes only on the approved replay, and fail-closed refusals."""

import base64
import tempfile
import threading
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from test_action_files import DATA, OPERATION_ID, UPLOAD

from action import files as action_files
from action import human as action_human
from chat import orchestrator as chat_orchestrator
from local import app as local_app
from local.chat import continuation as local_continuation
from local.chat import segment as local_segment
from storage import files as team_storage
from tests import human_request_fixtures


def _approved() -> action_human.ActionTranscript:
    return action_human.ActionTranscript("interrupt-1").append(human_request_fixtures.request("approval"), True)


class LocalFileDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = team_storage.TeamStorage(Path(temporary.name) / "teams")
        stored = self.storage.put("team_1", "Relatório de março.pdf", DATA, "application/pdf")
        self.file_id = stored["id"]
        self.file = action_files.selected([{**stored, "media_type": "application/pdf"}])[self.file_id]
        controller = object.__new__(local_app.LocalController)
        controller._locks = tuple(threading.RLock() for _ in range(64))
        controller.storage = self.storage
        spec = types.SimpleNamespace(actions={"upload": UPLOAD}, stored_inputs={}, machine_contract={"messages": []})
        self.container = types.SimpleNamespace(id="container", status="running", reload=mock.Mock())
        self.rpc = mock.Mock(return_value={"type": "result", "result": {"stored": True}})
        controller.assistant_lifecycle = types.SimpleNamespace(
            _resolve=lambda *_args: spec,
            _network=lambda _team_id: types.SimpleNamespace(name="network"),
            _assistant_container=lambda *_args: self.container,
            _validate_container=mock.Mock(),
            _blocked_action_workloads=set(),
            _rpc=self.rpc,
        )
        controller.chat_turn_service = types.SimpleNamespace(
            _active_chat_guard=nullcontext(),
            _active_action_containers={},
            _resolve_action_integrations=lambda *_args: {},
        )
        controller.assistant_stored_inputs = mock.Mock()
        self.controller = controller
        audit = mock.patch.object(local_app.local_audit, "record_request")
        self.audit = audit.start()
        self.addCleanup(audit.stop)

    def invoke(self, transcript: action_human.ActionTranscript, file=None) -> dict[str, object]:
        evidence = local_app.action_execution.ActionInvocationEvidence(
            local_app.action_execution.RpcPrivateInputs({}, {}, self.file if file is None else file),
            transcript,
            OPERATION_ID,
        )
        return self.controller.invoke("team_1", "docs", "upload", {"document": self.file_id}, evidence)

    def sent(self) -> dict[str, object]:
        return self.rpc.call_args.args[2]["files"][self.file_id]

    def test_the_first_invocation_carries_metadata_only_and_cannot_succeed(self) -> None:
        with self.assertRaises(local_app.ApiProblem) as caught:
            self.invoke(action_human.ActionTranscript("interrupt-1"))
        self.assertEqual(caught.exception.code, "invalid-action-output")
        self.assertEqual(self.sent()["content"], {"type": "withheld"})
        self.assertEqual((self.sent()["size"], self.sent()["media_type"]), (len(DATA), "application/pdf"))

    def test_the_approved_replay_receives_the_original_bytes_and_audits_only_id_and_size(self) -> None:
        result = self.invoke(_approved())
        self.assertEqual(result["result"], {"stored": True})
        self.assertEqual(base64.b64decode(self.sent()["content"]["base64"]), DATA)
        details = [call.kwargs.get("detail") for call in self.audit.call_args_list]
        self.assertIn(f"file-delivered:upload:{self.file_id}:{len(DATA)}", details)
        self.assertFalse(any("Relatório" in str(detail) for detail in details))

    def test_a_failed_exchange_audits_the_delivery_as_unconfirmed_never_as_delivered(self) -> None:
        self.rpc.side_effect = local_app.ApiProblem(504, "Assistant Action timed out", code="assistant-timeout")
        with self.assertRaises(local_app.ApiProblem):
            self.invoke(_approved())
        details = [call.kwargs.get("detail") for call in self.audit.call_args_list]
        self.assertIn(f"file-delivery-unconfirmed:upload:{self.file_id}:{len(DATA)}", details)
        self.assertFalse(any(str(detail).startswith("file-delivered") for detail in details))

    def test_a_deleted_file_or_a_direct_invocation_never_reaches_the_workload(self) -> None:
        self.storage.delete("team_1", self.file_id)
        with self.assertRaises(local_app.ApiProblem) as caught:
            self.invoke(_approved())
        self.assertEqual(caught.exception.code, "action-file-unavailable")
        with self.assertRaises(local_app.ApiProblem) as caught:
            self.controller.invoke("team_1", "docs", "upload", {"document": self.file_id})
        self.assertEqual(caught.exception.code, "action-file-unavailable")
        self.rpc.assert_not_called()


class LocalFileChallengeTests(unittest.TestCase):
    def test_an_authorization_requirement_discloses_the_selected_file(self) -> None:
        active = types.SimpleNamespace(
            spec=types.SimpleNamespace(
                assistant_id="docs",
                name="Docs",
                version="1.0.0",
                actions={"upload": UPLOAD},
                stored_inputs={},
            )
        )
        request = human_request_fixtures.request("approval")
        subject = types.SimpleNamespace(
            _assistant_language=lambda _active: human_request_fixtures.pack_for(request.messages())
        )
        file = {
            "id": "0" * 32,
            "name": "a.pdf",
            "media_type": "application/pdf",
            "size": 3,
            "sha256": "a" * 64,
        }
        selected = action_files.selected([file])
        action = local_app.brain_runtime_client.ActionRequest("interrupt-1", "docs", "upload", {"document": "0" * 32})
        requirement = local_segment._human_requirement(subject, {"docs": active}, action, request, "en", selected)
        self.assertEqual(requirement.file, file)
        with self.assertRaises(chat_orchestrator.ChatOrchestrationError):
            local_segment._human_requirement(subject, {"docs": active}, action, request, "en", {})
        restored = local_continuation._human_requirement(
            local_continuation._requirements_payload("human", (requirement,))[0]
        )
        self.assertEqual(restored, requirement)


if __name__ == "__main__":
    unittest.main()
