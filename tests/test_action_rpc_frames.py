"""Adversarial frame contracts for hosted and local Assistant Action RPC."""

from __future__ import annotations

import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hosted_assistant_fixture import hosted_assistants, runtime_state

from action import execution as action_execution
from action import human as action_human
from action import journal as action_journal
from hosted import container as container_spec
from inference import client as brain_runtime_client
from local import app as local_app
from local.assistant import isolation as local_container_policy
from local.assistant import rpc as local_assistant_rpc


def _frame(stream_id: int, payload: bytes) -> bytes:
    return struct.pack(">BxxxL", stream_id, len(payload)) + payload


@contextmanager
def _socket_bytes(payload: bytes, *, pieces: tuple[int, ...] = ()):
    reader, writer = socket.socketpair()

    def send() -> None:
        offset = 0
        for size in pieces:
            writer.sendall(payload[offset : offset + size])
            offset += size
            time.sleep(0.005)
        writer.sendall(payload[offset:])
        writer.shutdown(socket.SHUT_WR)

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    try:
        yield reader
    finally:
        sender.join(timeout=1)
        reader.close()
        writer.close()


class ActionRpcFrameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.local = object.__new__(local_app.LocalController)
        self.local._wire_collaborators()

    def test_split_stdout_and_stderr_frames_are_read_exactly(self) -> None:
        payload = _frame(1, b'{"ok":') + _frame(2, b"warning") + _frame(1, b"true}")
        with _socket_bytes(payload, pieces=(1, 2, 5, 3, 7)) as hosted_socket:
            stdout, stderr = action_execution.read_rpc_frames(
                hosted_socket,
                time.monotonic() + 1,
                action_execution.MAX_RPC_RESPONSE_BYTES,
            )
        with _socket_bytes(payload, pieces=(4, 1, 6, 2)) as local_socket:
            local_stdout, local_stderr = action_execution.read_rpc_frames(
                local_socket,
                time.monotonic() + 1,
                action_execution.MAX_RPC_RESPONSE_BYTES,
            )

        self.assertEqual(stdout, b'{"ok":true}')
        self.assertEqual(stderr, b"warning")
        self.assertEqual(local_stdout, stdout)
        self.assertEqual(local_stderr, stderr)

    def test_rpc_response_accepts_only_a_direct_spec_v1_object(self) -> None:
        self.assertEqual(
            action_execution.decode_rpc_response(b'{"ok":true}'),
            {"ok": True},
        )
        for invalid in (
            b'{"ok":true,"ok":false}',
            b'{"value":NaN}',
            b"[]",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(action_execution.RpcExchangeError):
                action_execution.decode_rpc_response(invalid)

    def test_rpc_failure_kinds_share_one_http_status_table(self) -> None:
        self.assertEqual(
            {
                kind: action_execution.rpc_failure_status(kind)
                for kind in ("timeout", "ambiguous", "invalid-result", "failed")
            },
            {
                "timeout": HTTPStatus.GATEWAY_TIMEOUT,
                "ambiguous": HTTPStatus.BAD_GATEWAY,
                "invalid-result": HTTPStatus.BAD_GATEWAY,
                "failed": HTTPStatus.BAD_GATEWAY,
            },
        )
        with self.assertRaisesRegex(AssertionError, "unknown RPC failure"):
            action_execution.rpc_failure_status("unknown")
        with self.assertRaisesRegex(AssertionError, "unknown RPC failure"):
            action_execution.rpc_failure_message("unknown")

    def test_rpc_invocation_and_operation_inputs_are_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "envelope"):
            action_execution.integration_access_tokens({"cloud": {"type": "invalid", "access_token": "token"}})
        with self.assertRaisesRegex(ValueError, "invocation"):
            action_execution.encode_rpc_invocation({"value": object()}, {})
        with (
            mock.patch.object(action_execution, "MAX_RPC_REQUEST_BYTES", 1),
            self.assertRaisesRegex(ValueError, "too large"),
        ):
            action_execution.encode_rpc_invocation({}, {})

        request = brain_runtime_client.ActionRequest("interrupt", "assistant", "action", {})
        for container_id, image in (("", "image"), ("container", "")):
            with (
                self.subTest(container_id=container_id, image=image),
                self.assertRaises(action_journal.ActionJournalConflictError),
            ):
                action_execution.action_operation(request, container_id, image)
        malformed = SimpleNamespace(
            assistant_id="assistant",
            action="action",
            interrupt_id="interrupt",
            input=object(),
        )
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "fingerprinted"):
            action_execution.action_operation(malformed, "container", "image")

    def test_local_readiness_requires_only_a_running_prebuilt_container(self) -> None:
        container = SimpleNamespace(status="running", reload=lambda: None)
        self.local.assistant_lifecycle._rpc = mock.Mock()

        self.local.assistant_lifecycle._wait_ready(container, object())

        self.local.assistant_lifecycle._rpc.assert_not_called()

    def test_private_generation_helpers_apply_one_action_contract(self) -> None:
        actions = {
            "lookup": SimpleNamespace(integrations=("cloud",)),
        }
        integration_metadata = mock.Mock(
            return_value=(SimpleNamespace(id="cloud", status="connected", generation=5),),
        )

        self.assertEqual(
            action_execution.integration_generations(
                actions,
                {"cloud": "declaration"},
                "lookup",
                integration_metadata,
            ),
            (("cloud", 5),),
        )
        integration_metadata.assert_called_once_with({"cloud": "declaration"})
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "integration contract"):
            action_execution.integration_generations(actions, {}, "lookup", integration_metadata)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "integration contract"):
            action_execution.integration_generations(actions, {}, "missing", integration_metadata)
        with self.assertRaisesRegex(action_journal.ActionJournalConflictError, "generation"):
            action_execution.private_generations((SimpleNamespace(id="cloud", status="missing", generation=0),))

    def test_rpc_result_projection_rejects_private_and_invalid_outputs(self) -> None:
        projected = action_execution.project_rpc_result(
            {"type": "result", "result": {"ok": True}},
            {"cloud": {"access_token": "private"}},
            lambda value: value,
        )
        self.assertEqual(projected, {"ok": True})

        with self.assertRaises(action_execution.RpcSecretExposureError):
            action_execution.project_rpc_result(
                {"type": "result", "result": {"echo": "private"}},
                {"cloud": {"access_token": "private"}},
                lambda value: value,
            )
        with self.assertRaises(action_execution.RpcInvalidResultError):
            action_execution.project_rpc_result(
                {"type": "result", "result": {"invalid": True}},
                {},
                lambda _value: (_ for _ in ()).throw(ValueError("invalid")),
            )
        for invalid in ([], {"type": "unknown", "result": None}):
            with self.subTest(invalid=invalid), self.assertRaises(action_execution.RpcInvalidResultError):
                action_execution.project_rpc_result(invalid, {}, lambda value: value)

    def test_rpc_request_requires_reviewed_capability_and_canonical_fingerprint(self) -> None:
        request = {
            "kind": "approval",
            "ordinal": 0,
            "title": "Publish zone",
            "description": "Publish this reviewed DNS zone.",
        }
        request["fingerprint"] = action_human._fingerprint(request)

        with self.assertRaises(action_human.HumanRequestSuspensionError) as suspended:
            action_execution.project_rpc_result(
                {"type": "request", "request": request},
                {},
                lambda value: value,
                ("approval",),
            )
        self.assertEqual(suspended.exception.request.payload(), request)

        with self.assertRaises(action_execution.RpcInvalidResultError):
            action_execution.project_rpc_result(
                {"type": "request", "request": request},
                {},
                lambda value: value,
                (),
            )

    def test_rpc_invocation_adds_a_transcript_only_during_replay(self) -> None:
        initial = action_execution.encode_rpc_invocation({}, {})
        response = {
            "kind": "approval",
            "ordinal": 0,
            "fingerprint": "a" * 64,
            "value": True,
        }
        replay = action_execution.encode_rpc_invocation({}, {}, (response,))

        self.assertEqual(initial, b'{"input":{},"integrations":{}}')
        self.assertEqual(
            replay,
            b'{"input":{},"integrations":{},"responses":[{"kind":"approval","ordinal":0,'
            b'"fingerprint":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            b'"value":true}]}',
        )

    def test_malformed_frames_fail_closed_in_both_readers(self) -> None:
        oversized = struct.pack(
            ">BxxxL",
            1,
            action_execution.MAX_RPC_RESPONSE_BYTES + 2,
        )
        cases = (
            b"\x01\x00\x00",
            _frame(1, b"payload")[:-2],
            oversized,
            b"garbage!",
        )

        for payload in cases:
            with self.subTest(payload=payload):
                with _socket_bytes(payload) as hosted_socket, self.assertRaises(ValueError):
                    action_execution.read_rpc_frames(
                        hosted_socket,
                        time.monotonic() + 1,
                        action_execution.MAX_RPC_RESPONSE_BYTES,
                    )
                with _socket_bytes(payload) as local_socket, self.assertRaises(ValueError):
                    action_execution.read_rpc_frames(
                        local_socket,
                        time.monotonic() + 1,
                        action_execution.MAX_RPC_RESPONSE_BYTES,
                    )

    def test_clean_eof_is_the_only_empty_success(self) -> None:
        with _socket_bytes(b"") as hosted_socket:
            self.assertEqual(
                action_execution.read_rpc_frames(
                    hosted_socket,
                    time.monotonic() + 1,
                    action_execution.MAX_RPC_RESPONSE_BYTES,
                ),
                (b"", b""),
            )
        with _socket_bytes(b"") as local_socket:
            self.assertEqual(
                action_execution.read_rpc_frames(
                    local_socket,
                    time.monotonic() + 1,
                    action_execution.MAX_RPC_RESPONSE_BYTES,
                ),
                (b"", b""),
            )

    def test_rpc_exchange_bounds_stdin_write_by_deadline(self) -> None:
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        for current in (reader, writer):
            current.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
            current.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        writer.settimeout(3.0)
        stream = SimpleNamespace(_sock=writer, close=lambda: None)
        api = SimpleNamespace(
            exec_create=lambda *_args, **_kwargs: {"Id": "e"},
            exec_start=lambda *_args, **_kwargs: stream,
            exec_inspect=lambda *_args, **_kwargs: {"ExitCode": 0},
        )
        strategy = action_execution.RpcExchangeStrategy(
            api=api,
            user="10001:10001",
            workdir=container_spec.CONTAINER_TMP,
            timeout=0.5,
            maximum=action_execution.MAX_RPC_REQUEST_BYTES,
            transport_errors=(),
            fail_stop=lambda: None,
            cancelled=lambda _error: None,
            close_stream=lambda _stream: None,
        )

        start = time.monotonic()
        with self.assertRaises(action_execution.RpcExchangeError) as caught:
            action_execution.rpc_exchange(
                "cid",
                ["/bin/true"],
                b"x" * (512 * 1024),
                strategy,
            )
        elapsed = time.monotonic() - start

        self.assertEqual(caught.exception.kind, "timeout")
        self.assertLess(elapsed, 1.5)

    def test_rpc_exchange_classifies_attach_inspection_and_exit_failures(self) -> None:
        class TransportError(RuntimeError):
            pass

        def strategy(api: object) -> tuple[action_execution.RpcExchangeStrategy, mock.Mock, mock.Mock, mock.Mock]:
            fail_stop = mock.Mock()
            cancelled = mock.Mock()
            close = mock.Mock()
            return (
                action_execution.RpcExchangeStrategy(
                    api=api,
                    user="10001:10001",
                    workdir=container_spec.CONTAINER_TMP,
                    timeout=1,
                    maximum=1024,
                    transport_errors=(TransportError,),
                    fail_stop=fail_stop,
                    cancelled=cancelled,
                    close_stream=close,
                ),
                fail_stop,
                cancelled,
                close,
            )

        stream = SimpleNamespace()
        api = SimpleNamespace(
            exec_create=lambda *_args, **_kwargs: {"Id": "exec"},
            exec_start=lambda *_args, **_kwargs: stream,
        )
        current, fail_stop, cancelled, close = strategy(api)
        with self.assertRaises(action_execution.RpcExchangeError) as failed:
            action_execution.rpc_exchange("container", ["command"], b"request", current)
        self.assertEqual(failed.exception.kind, "failed")
        fail_stop.assert_called_once_with()
        cancelled.assert_called_once()
        close.assert_called_once_with(stream)

        raw_socket = SimpleNamespace(shutdown=lambda _how: None)
        stream = SimpleNamespace(_sock=raw_socket)
        for details, inspect_error, expected, unsupported in (
            (None, TransportError("offline"), "ambiguous", False),
            ({"ExitCode": None}, None, "ambiguous", False),
            ({"ExitCode": 2}, None, "unsupported-path", True),
            ({"ExitCode": 1}, None, "failed", False),
        ):
            api = mock.Mock()
            api.exec_create.return_value = {"Id": "exec"}
            api.exec_start.return_value = stream
            if inspect_error is not None:
                api.exec_inspect.side_effect = inspect_error
            else:
                api.exec_inspect.return_value = details
            current, fail_stop, cancelled, close = strategy(api)
            with (
                self.subTest(expected=expected),
                mock.patch.object(action_execution, "_write_all"),
                mock.patch.object(action_execution, "read_rpc_frames", return_value=(b"", b"")),
                self.assertRaises(action_execution.RpcExchangeError) as caught,
            ):
                action_execution.rpc_exchange(
                    "container",
                    ["command"],
                    b"request",
                    current,
                    detect_unsupported_path=unsupported,
                )
            self.assertEqual(caught.exception.kind, expected)
            close.assert_called_once_with(stream)
            if expected == "ambiguous":
                fail_stop.assert_called_once_with()
            else:
                fail_stop.assert_not_called()
            if expected != "unsupported-path":
                cancelled.assert_called_once()

        api = mock.Mock()
        api.exec_create.return_value = {"Id": "exec"}
        api.exec_start.return_value = stream
        api.exec_inspect.return_value = {"ExitCode": 0}
        current, fail_stop, cancelled, close = strategy(api)
        with (
            mock.patch.object(action_execution, "_write_all"),
            mock.patch.object(action_execution, "read_rpc_frames", return_value=(b'{"ok":true}', b"")),
        ):
            self.assertEqual(
                action_execution.rpc_exchange("container", ["command"], b"request", current),
                {"ok": True},
            )
        fail_stop.assert_not_called()
        cancelled.assert_not_called()
        close.assert_called_once_with(stream)

    def test_rpc_exchange_rejects_a_missing_attach_stream(self) -> None:
        api = mock.Mock()
        api.exec_create.return_value = {"Id": "exec"}
        api.exec_start.return_value = None
        fail_stop = mock.Mock()
        cancelled = mock.Mock()
        close = mock.Mock()
        strategy = action_execution.RpcExchangeStrategy(
            api=api,
            user="10001:10001",
            workdir=container_spec.CONTAINER_TMP,
            timeout=1,
            maximum=1024,
            transport_errors=(),
            fail_stop=fail_stop,
            cancelled=cancelled,
            close_stream=close,
        )
        with self.assertRaises(action_execution.RpcExchangeError) as unavailable:
            action_execution.rpc_exchange("container", ["command"], b"request", strategy)
        self.assertEqual(unavailable.exception.kind, "failed")
        fail_stop.assert_called_once_with()
        cancelled.assert_called_once()
        close.assert_not_called()

    def test_rpc_frame_reader_bounds_cumulative_payload_and_deadline(self) -> None:
        with (
            _socket_bytes(_frame(1, b"ab") + _frame(2, b"cd")) as raw_socket,
            self.assertRaisesRegex(
                ValueError,
                "oversized",
            ),
        ):
            action_execution.read_rpc_frames(raw_socket, time.monotonic() + 1, 3)
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        with self.assertRaises(TimeoutError):
            action_execution._read_exact(reader, 1, time.monotonic() - 1)

        response = mock.Mock()
        action_execution.close_exec_stream(SimpleNamespace(_response=response))
        response.close.assert_called_once_with()

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

    def test_action_resolution_failures_have_identical_statuses(self) -> None:
        local_spec = SimpleNamespace(assistant_id="assistant", name="Assistant", actions={}, integrations={})

        hosted_active = SimpleNamespace(
            assistant_id="assistant",
            contract=SimpleNamespace(actions={}, integrations={}),
        )
        self.local.assistant_integrations = object()
        with self.assertRaises(runtime_state.ApiError) as hosted_integration:
            hosted_assistants._resolve_action_integrations("team_1", hosted_active, "missing")
        with self.assertRaises(local_app.ApiProblem) as local_integration:
            self.local.chat_turn_service._resolve_action_integrations("team_1", local_spec, "missing")
        self.assertEqual(
            hosted_integration.exception.status,
            local_integration.exception.status,
            action_execution.INTEGRATION_PRECONDITION_STATUS,
        )

    def test_hosted_exchange_fail_stops_on_malformed_frame(self) -> None:
        with _socket_bytes(b"truncated") as raw_socket:
            stream = SimpleNamespace(_sock=raw_socket, close=lambda: None)
            create = mock.Mock(return_value={"Id": "exec-1"})
            api = SimpleNamespace(
                exec_create=create,
                exec_start=lambda *_args, **_kwargs: stream,
            )
            fail_stop = mock.Mock()
            container = SimpleNamespace(id="assistant-container")
            with (
                mock.patch.object(runtime_state, "_docker", SimpleNamespace(api=api)),
                mock.patch.object(hosted_assistants, "_fail_stop_action", fail_stop),
                mock.patch.object(
                    hosted_assistants.action_execution,
                    "encode_rpc_invocation",
                    return_value=b"request",
                ),
                self.assertRaises(runtime_state.ApiError) as caught,
            ):
                hosted_assistants._assistant_rpc_exchange(
                    hosted_assistants.AssistantRpcRequest(
                        team_id="team_1",
                        container=container,
                        action_id="test",
                        payload={"input": {}, "integrations": {}},
                        token=None,
                    )
                )

        self.assertEqual(caught.exception.status, HTTPStatus.BAD_GATEWAY)
        fail_stop.assert_called_once_with("team_1", container)
        self.assertEqual(create.call_args.args[1], [action_execution.ACTION_COMMAND, "test"])
        self.assertEqual(create.call_args.kwargs["workdir"], container_spec.CONTAINER_TMP)

    def test_local_exchange_fail_stops_on_malformed_frame(self) -> None:
        with _socket_bytes(b"truncated") as raw_socket:
            stream = SimpleNamespace(_sock=raw_socket, close=lambda: None)
            create = mock.Mock(return_value={"Id": "exec-1"})
            api = SimpleNamespace(
                exec_create=create,
                exec_start=lambda *_args, **_kwargs: stream,
            )
            controller = object.__new__(local_app.LocalController)
            controller.client = SimpleNamespace(api=api)
            controller._wire_collaborators()
            controller.assistant_lifecycle._fail_stop_action = mock.Mock()
            with (
                mock.patch.object(
                    local_assistant_rpc.action_execution,
                    "encode_rpc_invocation",
                    return_value=b"request",
                ),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                controller.assistant_lifecycle._rpc(
                    SimpleNamespace(id="assistant-container"),
                    "test",
                    {"input": {}, "integrations": {}},
                )

        self.assertEqual(caught.exception.status, HTTPStatus.BAD_GATEWAY)
        controller.assistant_lifecycle._fail_stop_action.assert_called_once()
        self.assertEqual(create.call_args.args[1], [action_execution.ACTION_COMMAND, "test"])
        self.assertEqual(create.call_args.kwargs["workdir"], local_assistant_rpc.ASSISTANT_WORKDIR)

    def test_local_exchange_carries_replay_responses_only_when_present(self) -> None:
        fake = SimpleNamespace(
            client=SimpleNamespace(api=object()),
            _close_exec_stream=lambda _stream: None,
            _fail_stop_action=lambda _container: None,
        )
        response = {
            "kind": "approval",
            "ordinal": 0,
            "fingerprint": "a" * 64,
            "value": True,
        }
        with (
            mock.patch.object(local_assistant_rpc.action_execution, "rpc_exchange", return_value={"ok": True}),
            mock.patch.object(
                local_assistant_rpc.action_execution,
                "encode_rpc_invocation",
                return_value=b"request",
            ) as encode,
        ):
            local_assistant_rpc._rpc(
                fake,
                SimpleNamespace(id="assistant-container"),
                "test",
                {"input": {}, "integrations": {}, "responses": (response,)},
            )

        encode.assert_called_once_with({}, {}, (response,))

    def test_hosted_exchange_carries_replay_responses_only_when_present(self) -> None:
        response = {
            "kind": "approval",
            "ordinal": 0,
            "fingerprint": "a" * 64,
            "value": True,
        }
        request = hosted_assistants.AssistantRpcRequest(
            team_id="team_1",
            container=SimpleNamespace(id="assistant-container"),
            action_id="test",
            payload={"input": {}, "integrations": {}, "responses": (response,)},
            token=None,
        )
        with (
            mock.patch.object(runtime_state, "_docker", SimpleNamespace(api=object())),
            mock.patch.object(hosted_assistants.action_execution, "rpc_exchange", return_value={"ok": True}),
            mock.patch.object(
                hosted_assistants.action_execution,
                "encode_rpc_invocation",
                return_value=b"request",
            ) as encode,
        ):
            hosted_assistants._assistant_rpc_exchange(request)

        encode.assert_called_once_with({}, {}, (response,))


class RpcMessageParity(unittest.TestCase):
    def _hosted(self, kind):
        request = hosted_assistants.AssistantRpcRequest(
            team_id="t",
            container=SimpleNamespace(id="c"),
            action_id="p",
            payload={"input": {}, "integrations": {}},
            token=None,
        )
        with (
            mock.patch.object(runtime_state, "_docker", SimpleNamespace(api=object())),
            mock.patch.object(
                hosted_assistants.action_execution,
                "rpc_exchange",
                side_effect=hosted_assistants.action_execution.RpcExchangeError(kind),
            ),
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            hosted_assistants._assistant_rpc_exchange(request)
        return caught.exception.message

    def _local(self, kind):
        fake = SimpleNamespace(
            client=SimpleNamespace(api=object()),
            _close_exec_stream=lambda _stream: None,
            _fail_stop_action=lambda _container: None,
            _blocked_action_workloads=set(),
        )
        with (
            mock.patch.object(
                local_assistant_rpc.action_execution,
                "rpc_exchange",
                side_effect=local_assistant_rpc.action_execution.RpcExchangeError(kind),
            ),
            self.assertRaises(local_assistant_rpc.ApiProblem) as caught,
        ):
            local_assistant_rpc._rpc(
                fake,
                SimpleNamespace(id="c"),
                "p",
                {"input": {}, "integrations": {}},
            )
        return caught.exception.message

    def test_same_message_per_kind(self):
        self.assertEqual(
            set(action_execution.RPC_FAILURE_MESSAGES),
            set(action_execution.RPC_FAILURE_STATUSES),
        )
        self.assertEqual(
            action_execution.ASSISTANT_RPC_USER,
            local_container_policy.ASSISTANT_UID,
        )
        for kind in ("timeout", "ambiguous", "invalid-result", "failed"):
            canonical = action_execution.rpc_failure_message(kind)[0]
            self.assertEqual(self._hosted(kind), canonical)
            self.assertEqual(self._local(kind), canonical)


if __name__ == "__main__":
    unittest.main()
