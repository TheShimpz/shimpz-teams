"""Hosted Action file delivery: the same withheld-first contract, Owner disclosure, and fail-closed refusals."""

from __future__ import annotations

import base64
import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness
from test_action_files import DATA, OPERATION_ID, UPLOAD

from tests import human_request_fixtures

assistants = harness.hosted_assistants
segment = harness.hosted_chat_segment
state = harness.runtime_state
action_files = assistants.action_files
action_human = assistants.action_human
action_execution = assistants.action_execution
# The harness loads the Hosted modules on their own, so storage errors must come from the module they use.
team_storage = assistants.team_storage

TEAM_ID = "team_1"
ASSISTANT_ID = "shimpz-cloudflare"
TURN_TOKEN = "-".join(("turn", "token"))
CONTRACT = dataclasses.replace(harness.HOSTED_SPEC.contract, actions={"upload": UPLOAD})


def _approved() -> action_human.ActionTranscript:
    return action_human.ActionTranscript("interrupt-1").append(human_request_fixtures.request("approval"), True)


class HostedFileDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = team_storage.TeamStorage(Path(temporary.name) / "teams")
        stored = self.storage.put(TEAM_ID, "Relatório de março.pdf", DATA, "application/pdf")
        self.file_id = stored["id"]
        self.file = action_files.selected([stored])[self.file_id]
        self.container = SimpleNamespace(id="c" * 64)
        self.active = assistants._ActiveAssistant(
            ASSISTANT_ID, CONTRACT, self.container, harness.HOSTED_SPEC.image, "0.4.1", harness.HOSTED_SPEC.summary
        )
        for patcher in (
            mock.patch.object(state, "_storage", return_value=self.storage),
            mock.patch.object(assistants.audit, "log"),
        ):
            started = patcher.start()
            self.addCleanup(patcher.stop)
        self.audit = started

    def request(self, transcript, file="selected"):
        evidence = action_execution.ActionInvocationEvidence(
            action_execution.RpcPrivateInputs({}, {}, self.file if file == "selected" else file),
            transcript,
            OPERATION_ID,
        )
        return assistants.ActionInvocationRequest(
            team_id=TEAM_ID,
            token=TURN_TOKEN,
            assistant_id=ASSISTANT_ID,
            contract=CONTRACT,
            container=self.container,
            action="upload",
            payload={"document": self.file_id},
            validated_assistant=self.active,
            evidence=evidence,
        )

    def invoke(self, transcript, file="selected", rpc_result=None):
        request = self.request(transcript, file)
        rpc_result = rpc_result or {"type": "result", "result": {"stored": True}}
        with mock.patch.object(assistants, "_assistant_rpc", return_value=rpc_result) as rpc:
            try:
                return assistants._invoke_assistant_action(request), rpc
            except state.ApiError as exc:
                return exc, rpc

    def test_metadata_first_then_verified_bytes_only_after_approval(self) -> None:
        refused, rpc = self.invoke(action_human.ActionTranscript("interrupt-1"))
        self.assertIsInstance(refused, state.ApiError)
        self.assertEqual(rpc.call_args.args[4]["files"][self.file_id]["content"], {"type": "withheld"})
        result, rpc = self.invoke(_approved())
        self.assertEqual(result["result"], {"stored": True})
        content = rpc.call_args.args[4]["files"][self.file_id]["content"]
        self.assertEqual(base64.b64decode(content["base64"]), DATA)
        self.audit.assert_any_call(
            "assistant_action",
            TEAM_ID,
            result="ok",
            phase="file-delivered",
            assistant=ASSISTANT_ID,
            action="upload",
            file=self.file_id,
            size=len(DATA),
        )

    def test_a_failed_exchange_audits_the_delivery_as_unconfirmed_never_as_delivered(self) -> None:
        with (
            mock.patch.object(assistants, "_assistant_rpc", side_effect=state.ApiError(504, "timed out")),
            self.assertRaises(state.ApiError),
        ):
            assistants._invoke_assistant_action(self.request(_approved()))
        phases = [call.kwargs.get("phase") for call in self.audit.call_args_list]
        self.assertIn("file-delivery-unconfirmed", phases)
        self.assertNotIn("file-delivered", phases)

    def test_a_deleted_file_or_a_turn_without_it_never_reaches_the_workload(self) -> None:
        self.storage.delete(TEAM_ID, self.file_id)
        for file in ("selected", None):
            with self.subTest(file=file):
                refused, rpc = self.invoke(_approved(), file)
                self.assertIsInstance(refused, state.ApiError)
                self.assertIn("attach it again", refused.message)
                rpc.assert_not_called()

    def test_the_preflight_binds_only_a_selected_file(self) -> None:
        request = assistants.brain_runtime_client.ActionRequest(
            "interrupt-1", ASSISTANT_ID, "upload", {"document": self.file_id}
        )
        bindings = {ASSISTANT_ID: self.active}
        selected = {self.file_id: self.file}
        with mock.patch.object(assistants, "_resolve_action_integrations", return_value={}):
            private = assistants._require_hosted_action_rpc_envelope(TEAM_ID, bindings, request, selected)
            self.assertIs(private.file, self.file)
            with self.assertRaises(state.ApiError):
                assistants._require_hosted_action_rpc_envelope(TEAM_ID, bindings, request, {})

    def test_an_owner_authorization_discloses_the_selected_file(self) -> None:
        request = human_request_fixtures.request("approval")
        action = assistants.brain_runtime_client.ActionRequest(
            "interrupt-1", ASSISTANT_ID, "upload", {"document": self.file_id}
        )
        pack = human_request_fixtures.pack_for(request.messages())
        with mock.patch.object(segment.assistant_lifecycle, "_assistant_language", return_value=pack):
            requirement = segment._hosted_human_requirement(
                {ASSISTANT_ID: self.active}, action, request, "en", {self.file_id: self.file}
            )
            self.assertEqual(requirement.file, self.file.metadata())
            with self.assertRaises(segment.chat_orchestrator.ChatOrchestrationError):
                segment._hosted_human_requirement({ASSISTANT_ID: self.active}, action, request, "en", {})


if __name__ == "__main__":
    unittest.main()
