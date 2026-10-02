"""Team-brokered Action file delivery (ADR-0093): selection, withheld metadata, authorized bytes, and bounds."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hosted_assistant_fixture import hosted_assistants
from hosted_assistant_fixture import runtime_state as hosted_state

from action import challenges as action_challenges
from action import execution as action_execution
from action import files as action_files
from action import human as action_human
from assistant.spec import ActionSpec
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from local import app as local_app
from local.assistant import rpc as local_assistant_rpc
from local.chat import private as local_chat_private
from protocol.http.v1 import payload as http_payload
from storage import files as team_storage
from tests import human_request_fixtures

FILE_ID = "0123456789abcdef0123456789abcdef"
DATA = b"%PDF-1.4 quarterly report"
FILE_SCHEMA = {
    "type": "object",
    "properties": {"document": {"type": "string", "minLength": 32, "maxLength": 32, "pattern": "^[0-9a-f]{32}$"}},
    "required": ["document"],
    "additionalProperties": False,
}
UPLOAD = ActionSpec(
    "Upload a document.", FILE_SCHEMA, {"type": "object"}, human_requests=("approval",), input_files=("document",)
)
ORDINARY = ActionSpec("List zones.", {"type": "object"}, {"type": "object"})
OPERATION_ID = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"


def _wire(file_id: str = FILE_ID, *, data: bytes = DATA, **changes: object) -> dict[str, object]:
    return {
        "id": file_id,
        "name": "Relatório de março.pdf",
        "media_type": "application/pdf",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content": {"type": "opaque", "reason": "unsupported"},
        **changes,
    }


def _approved(interrupt_id: str = "interrupt-1") -> action_human.ActionTranscript:
    return action_human.ActionTranscript(interrupt_id).append(human_request_fixtures.request("approval"), True)


class SelectionTests(unittest.TestCase):
    def test_only_a_selected_deliverable_file_reaches_a_file_taking_action(self) -> None:
        selected = action_files.selected([_wire()])
        file = action_files.action_file(UPLOAD.input_files, {"document": FILE_ID}, selected)
        self.assertEqual(file.metadata()["sha256"], hashlib.sha256(DATA).hexdigest())
        self.assertIsNone(action_files.action_file((), {"document": FILE_ID}, selected))
        oversized = action_files.selected([_wire(size=8 * 1024 * 1024 + 1)])
        unnamed = action_files.selected([_wire(name="a/b.pdf")])
        for files, value in (
            (selected, "f" * 32),
            (selected, ["not", "an", "id"]),
            ({}, FILE_ID),
            (oversized, FILE_ID),
            (unnamed, FILE_ID),
        ):
            with self.subTest(value=value), self.assertRaises(action_files.FileDeliveryError):
                action_files.action_file(UPLOAD.input_files, {"document": value}, files)

    def test_file_commitments_are_always_in_the_operation_fingerprint(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "docs", "upload", {"document": FILE_ID})
        file = action_files.action_file(UPLOAD.input_files, request.input, action_files.selected([_wire()]))
        changed = dataclasses.replace(file, sha256="b" * 64)
        self.assertEqual(action_files.commitments(None), [])
        fingerprints = {
            action_execution.action_operation(request, "container", "image", files=commitments).fingerprint
            for commitments in ([], action_files.commitments(file), action_files.commitments(changed))
        }
        self.assertEqual(len(fingerprints), 3)
        self.assertEqual(
            action_execution.action_operation(request, "container", "image").fingerprint,
            action_execution.action_operation(request, "container", "image", files=[]).fingerprint,
        )

    def test_only_an_authorization_request_discloses_the_file(self) -> None:
        selected = action_files.selected([_wire()])
        disclosed = action_files.disclosure(UPLOAD.input_files, {"document": FILE_ID}, selected, "approval")
        self.assertEqual(set(disclosed), {"id", "name", "media_type", "size", "sha256"})
        self.assertIsNone(action_files.disclosure(UPLOAD.input_files, {"document": FILE_ID}, selected, "input:text"))
        self.assertIsNone(action_files.disclosure((), {}, selected, "approval"))
        with self.assertRaises(action_files.FileDeliveryError):
            action_files.disclosure(UPLOAD.input_files, {"document": "f" * 32}, selected, "auth:totp")


class DeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = team_storage.TeamStorage(Path(temporary.name) / "teams")
        stored = self.storage.put("team_1", "Relatório de março.pdf", DATA, "application/pdf")
        self.file = action_files.action_file(
            UPLOAD.input_files,
            {"document": stored["id"]},
            action_files.selected([_wire(stored["id"])]),
        )
        self.input = {"document": stored["id"]}

    def read(self, file_id: str) -> tuple[dict[str, object], bytes]:
        return self.storage.get("team_1", file_id)

    def test_the_first_invocation_receives_metadata_only_and_never_reads_the_bytes(self) -> None:
        read = mock.Mock()
        files = action_files.deliver(UPLOAD, self.file, action_human.ActionTranscript("interrupt-1"), self.input, read)
        self.assertEqual(files[self.file.id]["content"], {"type": "withheld"})
        self.assertEqual(files[self.file.id]["size"], len(DATA))
        read.assert_not_called()
        self.assertIsNone(action_files.delivered(files))

    def test_only_the_approved_replay_receives_the_verified_original_bytes(self) -> None:
        files = action_files.deliver(UPLOAD, self.file, _approved(), self.input, self.read)
        content = files[self.file.id]["content"]
        self.assertEqual((content["type"], base64.b64decode(content["base64"])), ("delivered", DATA))
        self.assertEqual(action_files.delivered(files), self.file)

    def test_changed_deleted_or_unselected_bytes_are_never_delivered(self) -> None:
        def changed(_file_id: str) -> tuple[dict[str, object], bytes]:
            return {"sha256": self.file.sha256, "size": self.file.size}, b"x" * self.file.size

        def resized(_file_id: str) -> tuple[dict[str, object], bytes]:
            return {"sha256": self.file.sha256, "size": self.file.size + 1}, DATA

        for read in (changed, resized):
            with self.subTest(read=read.__name__), self.assertRaises(action_files.FileDeliveryError):
                action_files.deliver(UPLOAD, self.file, _approved(), self.input, read)
        self.storage.delete("team_1", self.file.id)
        with self.assertRaises(team_storage.StorageNotFoundError):
            action_files.deliver(UPLOAD, self.file, _approved(), self.input, self.read)
        # A file-taking Action outside a turn that selected its file never runs, even before authorization.
        with self.assertRaises(action_files.FileDeliveryError):
            action_files.deliver(UPLOAD, None, action_human.ActionTranscript("interrupt-1"), self.input, self.read)
        self.assertEqual(action_files.deliver(ORDINARY, None, _approved(), {}, self.read), {})

    def test_only_the_actions_own_declared_authorization_grants_the_bytes(self) -> None:
        self.assertTrue(action_files.authorized(("approval",), _approved()))
        self.assertFalse(action_files.authorized(("auth:password",), _approved()))
        self.assertFalse(action_files.authorized(("approval",), action_human.ActionTranscript("interrupt-1")))
        self.assertFalse(action_files.authorized(("approval", "auth:totp"), _approved()))

    def test_a_withheld_file_action_cannot_report_success(self) -> None:
        policy = action_execution.RpcResultPolicy(
            human_requests=("approval",), catalog=human_request_fixtures.CATALOG, file_withheld=True
        )
        with self.assertRaises(action_execution.RpcInvalidResultError):
            action_execution.project_rpc_result({"type": "result", "result": {}}, {}, lambda value: value, policy)
        request = {"type": "request", "request": human_request_fixtures.descriptor("approval")}
        with self.assertRaises(action_human.HumanRequestSuspensionError):
            action_execution.project_rpc_result(request, {}, lambda value: value, policy)

    def test_the_preflight_sizes_the_withheld_invocation_and_carries_the_file(self) -> None:
        request = brain_runtime_client.ActionRequest("interrupt-1", "docs", "upload", self.input)
        private = action_execution.require_rpc_envelope(object(), request, lambda *_: {}, lambda *_: {}, self.file)
        self.assertIs(private.file, self.file)
        evidence = action_execution.ActionInvocationEvidence(private, _approved(), "a" * 64, OPERATION_ID)
        resolved = action_execution.resolve_invocation_evidence(evidence, dict, dict)
        self.assertIs(resolved.file, self.file)


class FileRpcAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.file = action_files.selected([_wire()])[FILE_ID]

    def test_only_an_authorized_delivery_holds_the_slot_and_one_shared_deadline(self) -> None:
        delivered = {FILE_ID: {"content": {"type": "delivered", "base64": "YQ=="}}}
        withheld = {FILE_ID: {"content": {"type": "withheld"}}}
        with action_files.admitted(self.file, ("approval",), action_human.ActionTranscript("i"), lambda: False):
            self.assertEqual(action_files.rpc_timeout(withheld, 8.0), 8.0)
            with self.assertRaises(action_files.FileDeliveryError):
                action_files.rpc_timeout(delivered, 8.0)
        with action_files.admitted(None, ("approval",), _approved(), lambda: False):
            self.assertEqual(action_files._FILE_RPC_SLOT._value, 1)
        started = time.monotonic()
        with action_files.admitted(self.file, ("approval",), _approved(), lambda: False):
            self.assertEqual(action_files._FILE_RPC_SLOT._value, 0)
            remaining = action_files.rpc_timeout(delivered, 8.0)
            self.assertLessEqual(remaining, action_files.FILE_RPC_TIMEOUT_SECONDS - (time.monotonic() - started) + 0.01)
            self.assertGreater(remaining, action_files.FILE_RPC_TIMEOUT_SECONDS - 5)
        self.assertEqual(action_files._FILE_RPC_SLOT._value, 1)

    def test_a_waiting_delivery_is_refused_at_its_deadline_or_at_stop_without_holding_bytes(self) -> None:
        outcomes: list[BaseException] = []
        stopped = threading.Event()

        def contend(cancelled) -> None:
            try:
                with action_files.admitted(self.file, ("approval",), _approved(), cancelled):
                    outcomes.append(AssertionError("admitted while the slot was held"))
            except (action_files.FileRpcBusyError, action_files.FileRpcCancelledError) as exc:
                outcomes.append(exc)

        with (
            mock.patch.object(action_files, "FILE_RPC_TIMEOUT_SECONDS", 0.3),
            action_files.admitted(self.file, ("approval",), _approved(), lambda: False),
        ):
            busy = threading.Thread(target=contend, args=(lambda: False,))
            busy.start()
            busy.join(5)
            cancelled = threading.Thread(target=contend, args=(stopped.is_set,))
            started = time.monotonic()
            cancelled.start()
            stopped.set()
            cancelled.join(5)
            self.assertLess(time.monotonic() - started, 1.0)
        self.assertIsInstance(outcomes[0], action_files.FileRpcBusyError)
        self.assertIsInstance(outcomes[1], action_files.FileRpcCancelledError)
        self.assertEqual(action_files._FILE_RPC_SLOT._value, 1)

    def test_stop_that_wins_while_the_slot_frees_is_refused_and_releases_the_slot(self) -> None:
        stopped = threading.Event()
        held = threading.Event()
        release = threading.Event()
        outcome: list[BaseException] = []

        def holder() -> None:
            with action_files.admitted(self.file, ("approval",), _approved(), lambda: False):
                held.set()
                release.wait(5)

        def waiter() -> None:
            try:
                with action_files.admitted(self.file, ("approval",), _approved(), stopped.is_set):
                    outcome.append(AssertionError("admitted after Stop"))
            except action_files.FileRpcCancelledError as exc:
                outcome.append(exc)

        first = threading.Thread(target=holder)
        first.start()
        held.wait(5)
        # Stop wins and the slot frees in the same instant: the waiter acquires it, and must still refuse.
        with mock.patch.object(action_files, "_SLOT_POLL_SECONDS", 5.0):
            second = threading.Thread(target=waiter)
            second.start()
            time.sleep(0.1)
            stopped.set()
            release.set()
            first.join(5)
            second.join(5)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], action_files.FileRpcCancelledError)
        self.assertEqual(action_files._FILE_RPC_SLOT._value, 1)


class BatchAdmissionTests(unittest.TestCase):
    def test_a_refused_admission_never_begins_the_journaled_attempt(self) -> None:
        journal = mock.Mock(spec=action_execution.action_journal.ActionJournal)
        refused = mock.MagicMock()
        refused.__enter__.side_effect = action_files.FileRpcBusyError("busy")
        execute = mock.Mock()
        strategy = action_execution.ActionBatchStrategy(
            lambda _active: ("container", "image"),
            execute,
            lambda _request: action_execution.RpcPrivateInputs({}, {}),
            admit=lambda _request, _evidence: refused,
        )
        request = brain_runtime_client.ActionRequest("interrupt-1", "docs", "upload", {"document": FILE_ID})
        batch = action_execution.ActionBatch(journal, "generation", "thread", {"docs": object()}, strategy)
        operation = batch._operation(request)
        batch._batch, batch._operations = object(), {"interrupt-1": operation}
        batch._prepared_stored_inputs = {"interrupt-1": {}}
        with self.assertRaises(action_files.FileRpcBusyError):
            batch.invoke(request)
        journal.begin.assert_not_called()
        execute.assert_not_called()


def _context(*attachments: dict[str, object]) -> brain_runtime_client.RuntimeContext:
    upload = brain_runtime_client.RuntimeAction(
        id="upload",
        summary="Upload a document.",
        input_schema=FILE_SCHEMA,
        authorization=True,
        input_files=("document",),
    )
    return brain_runtime_client.RuntimeContext(
        thread_id="team:assistant:conversation",
        team_name="Docs",
        assistants=(brain_runtime_client.RuntimeAssistant(id="docs", genesis="Store documents.", actions=(upload,)),),
        provider="openai",
        model="gpt-test",
        api_key="not-a-real-key",
        effort="low",
        attachments=attachments,
    )


def _upload(interrupt_id: str, file_id: str = FILE_ID) -> brain_runtime_client.RuntimeTurn:
    return brain_runtime_client.RuntimeTurn(
        status="action-required",
        reply="",
        actions=(brain_runtime_client.ActionRequest(interrupt_id, "docs", "upload", {"document": file_id}),),
    )


class _Runtime:
    def __init__(self, turns: list[brain_runtime_client.RuntimeTurn]) -> None:
        self.turns = iter(turns)

    def start(self, _context, _message, *, conversation=()):
        return next(self.turns)

    def resume(self, _context, _results):
        return next(self.turns)


class OrchestratorFileTests(unittest.TestCase):
    def _run(self, turns, invoke, *attachments):
        strategy = chat_orchestrator.ChatStrategy(lambda _assistant, _action, payload: payload, invoke)
        with mock.patch.object(brain_runtime_client, "resume_capacity", return_value=7):
            return chat_orchestrator.run_until_pause(_Runtime(turns), _context(*attachments), "Store it", strategy)

    def test_a_file_the_turn_did_not_select_is_refused_before_any_action_runs(self) -> None:
        invoke = mock.Mock()
        with self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "did not select"):
            self._run([_upload("first", "f" * 32)], invoke, _wire())
        invoke.assert_not_called()

    def test_a_turn_admits_at_most_two_file_taking_logical_actions(self) -> None:
        invoke = mock.Mock(return_value={"stored": True})
        turns = [_upload("first"), _upload("second"), _upload("third")]
        with self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "file deliveries"):
            self._run(turns, invoke, _wire())
        self.assertEqual([call.args[0].interrupt_id for call in invoke.call_args_list], ["first", "second"])

    def test_a_paused_file_action_keeps_the_budget_it_had_before_its_round(self) -> None:
        def invoke(request):
            if request.interrupt_id == "second":
                raise action_human.HumanRequestSuspensionError(human_request_fixtures.request("approval"))
            return {"stored": True}

        paused = self._run([_upload("first"), _upload("second")], invoke, _wire())
        self.assertIsInstance(paused, chat_orchestrator.ChatHumanSuspension)
        self.assertEqual(paused.continuation.file_actions, 1)


class DisclosureTests(unittest.TestCase):
    def test_an_authorization_challenge_projects_the_disclosed_file_and_nothing_else_does(self) -> None:
        file = action_files.selected([_wire()])[FILE_ID].metadata()
        approval = human_request_fixtures.requirement(human_request_fixtures.request("approval"), file=file)
        challenge = action_challenges.PendingHumanChallenge("a" * 32, "team_1", 10**12, approval, None)
        with mock.patch.object(action_challenges.time, "monotonic", return_value=10**12 - 60):
            self.assertEqual(action_challenges.challenge_payload(challenge)["file"], file)
        text = human_request_fixtures.requirement(
            human_request_fixtures.request(
                "input:text", label="Code", required=True, placeholder=None, min_length=1, max_length=10
            ),
            file=file,
        )
        self.assertFalse(action_challenges._requirement(text))
        self.assertFalse(action_challenges._requirement(dataclasses.replace(approval, file={**file, "size": 0})))
        plain = human_request_fixtures.requirement(human_request_fixtures.request("approval"))
        with mock.patch.object(action_challenges.time, "monotonic", return_value=10**12 - 60):
            self.assertNotIn(
                "file", action_challenges.challenge_payload(dataclasses.replace(challenge, requirement=plain))
            )


class TransportEdgeTests(unittest.TestCase):
    def test_a_socket_that_is_momentarily_not_ready_is_retried_within_the_deadline(self) -> None:
        ours, workload = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(workload.close)

        class Flaky:
            """A real socket whose first send and first receive each report it would block."""

            def __init__(self) -> None:
                self.blocked = {"send", "recv"}

            def __getattr__(self, name: str):
                return getattr(ours, name)

            def send(self, data) -> int:
                if "send" in self.blocked:
                    self.blocked.discard("send")
                    raise BlockingIOError
                return ours.send(data)

            def recv(self, size: int) -> bytes:
                if "recv" in self.blocked:
                    self.blocked.discard("recv")
                    raise BlockingIOError
                return ours.recv(size)

        workload.sendall(struct.pack(">BxxxL", 1, 2) + b"ok")
        workload.shutdown(socket.SHUT_WR)
        flaky = Flaky()
        self.assertEqual(
            action_execution.exchange_rpc_frames(flaky, b"input", time.monotonic() + 5, 1024), (b"ok", b"")
        )
        self.assertEqual(flaky.blocked, set())
        self.assertEqual(workload.recv(16), b"input")

    def test_delivered_bytes_outside_an_admitted_slot_never_reach_the_workload(self) -> None:
        files = {FILE_ID: {"content": {"type": "delivered", "base64": "YQ=="}}}
        payload = {"input": {}, "integrations": {}, "stored_inputs": {}, "files": files, "operation_id": OPERATION_ID}
        subject = SimpleNamespace(client=SimpleNamespace(api=mock.Mock()), _close_exec_stream=mock.Mock())
        with (
            mock.patch.object(action_execution, "encode_rpc_invocation", return_value=b"{}"),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_assistant_rpc._rpc(subject, SimpleNamespace(id="container"), "upload", payload)
        self.assertEqual(caught.exception.code, "action-file-unavailable")
        subject.client.api.exec_create.assert_not_called()
        request = hosted_assistants.AssistantRpcRequest("team_1", SimpleNamespace(id="c"), "upload", payload, None)
        with (
            mock.patch.object(hosted_assistants.action_execution, "encode_rpc_invocation", return_value=b"{}"),
            mock.patch.object(hosted_assistants, "_exchange_registered") as exchange,
            self.assertRaises(hosted_state.ApiError) as hosted,
        ):
            hosted_assistants._assistant_rpc_exchange(request)
        self.assertEqual(hosted.exception.status, HTTPStatus.CONFLICT)
        exchange.assert_not_called()


class DisclosureVectorTests(unittest.TestCase):
    def test_every_published_disclosure_vector_is_admitted_exactly_or_refused(self) -> None:
        vectors = json.loads((Path(__file__).resolve().parents[1] / "protocol/http/v1/vectors.json").read_bytes())
        for value in vectors["file_disclosure"]["valid"]:
            self.assertEqual(http_payload.canonical_file_disclosure(value), value)
        for value in vectors["file_disclosure"]["invalid"]:
            with self.subTest(value=value):
                self.assertIsNone(http_payload.canonical_file_disclosure(value))


class LocalPreflightTests(unittest.TestCase):
    def test_a_file_the_turn_did_not_select_is_refused_before_journaling(self) -> None:
        upload = SimpleNamespace(spec=SimpleNamespace(actions={"upload": UPLOAD}))
        request = brain_runtime_client.ActionRequest("interrupt-1", "docs", "upload", {"document": FILE_ID})
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_private._require_action_rpc_envelope(SimpleNamespace(), "team_1", {"docs": upload}, request, {})
        self.assertEqual(caught.exception.code, "action-file-unavailable")


if __name__ == "__main__":
    unittest.main()
