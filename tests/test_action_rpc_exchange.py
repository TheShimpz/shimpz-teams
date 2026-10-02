"""The bounded Action RPC exchange: concurrent draining, the invocation bound, and one absolute deadline."""

from __future__ import annotations

import dataclasses
import json
import socket
import struct
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from action import execution as action_execution
from hosted import container as container_spec

OPERATION_ID = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"


def _frame(stream_id: int, payload: bytes) -> bytes:
    return struct.pack(">BxxxL", stream_id, len(payload)) + payload


class ActionRpcExchangeTests(unittest.TestCase):
    def test_a_workload_that_answers_before_reading_its_input_never_stalls(self) -> None:
        ours, workload = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(workload.close)
        for current in (ours, workload):
            current.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            current.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        answer = b"y" * 300_000
        received = bytearray()

        def run_workload() -> None:
            # Writes a large answer first, as a workload may, and only then reads all of its input.
            workload.sendall(_frame(1, answer))
            while chunk := workload.recv(65536):
                received.extend(chunk)
            workload.shutdown(socket.SHUT_WR)

        thread = threading.Thread(target=run_workload, daemon=True)
        thread.start()
        stdout, stderr = action_execution.exchange_rpc_frames(ours, b"x" * 1_000_000, time.monotonic() + 10, 512 * 1024)
        thread.join(5)
        self.assertEqual((stdout, stderr, len(received)), (answer, b"", 1_000_000))
        self.assertIsNone(ours.gettimeout())

    def test_a_workload_that_exits_before_reading_all_input_ends_the_exchange(self) -> None:
        ours, workload = socket.socketpair()
        self.addCleanup(ours.close)
        workload.sendall(_frame(1, b'{"ok":true}'))
        workload.close()
        self.assertEqual(
            action_execution.exchange_rpc_frames(ours, b"x" * 1_000_000, time.monotonic() + 5, 1024),
            (b'{"ok":true}', b""),
        )

    def test_only_delivered_file_content_admits_the_larger_invocation_bound(self) -> None:
        record = {"name": "a.pdf", "media_type": "application/pdf", "size": 1, "sha256": "0" * 64}
        withheld = {"0" * 32: {**record, "content": {"type": "withheld"}}}
        large = {"note": "x" * (600 * 1024)}
        with self.assertRaisesRegex(ValueError, "too large"):
            action_execution.encode_rpc_invocation(large, {}, {}, OPERATION_ID, files=withheld)
        delivered = {"0" * 32: {**record, "content": {"type": "delivered", "base64": "x" * (8 * 1024 * 1024)}}}
        encoded = action_execution.encode_rpc_invocation({"file": "0" * 32}, {}, {}, OPERATION_ID, files=delivered)
        self.assertGreater(len(encoded), action_execution.MAX_RPC_REQUEST_BYTES)
        self.assertEqual(json.loads(encoded)["files"], delivered)
        too_large = {"0" * 32: {**record, "content": {"type": "delivered", "base64": "x" * (12 * 1024 * 1024)}}}
        with self.assertRaisesRegex(ValueError, "too large"):
            action_execution.encode_rpc_invocation({}, {}, {}, OPERATION_ID, files=too_large)

    def test_setup_time_is_charged_to_the_one_absolute_deadline(self) -> None:
        clock = [100.0]
        seen: list[float] = []

        def create(*_args, **_kwargs) -> dict[str, str]:
            clock[0] += 20.0  # A slow exec_create on the fake clock.
            return {"Id": "exec"}

        api = SimpleNamespace(
            exec_create=create,
            exec_start=lambda *_args, **_kwargs: SimpleNamespace(_sock=object()),
            exec_inspect=lambda *_args, **_kwargs: {"ExitCode": 0},
        )
        strategy = action_execution.RpcExchangeStrategy(
            api=api,
            user="10001:10001",
            workdir=container_spec.CONTAINER_TMP,
            timeout=60,
            maximum=1024,
            transport_errors=(),
            fail_stop=mock.Mock(),
            cancelled=mock.Mock(),
            close_stream=mock.Mock(),
        )

        def exchange(_socket, _data, deadline, _maximum) -> tuple[bytes, bytes]:
            seen.append(deadline)
            return b"{}", b""

        with (
            mock.patch.object(action_execution.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(action_execution, "exchange_rpc_frames", side_effect=exchange),
        ):
            action_execution.rpc_exchange("container", ["command"], b"request", strategy)
            action_execution.rpc_exchange(
                "container", ["command"], b"request", dataclasses.replace(strategy, deadline=clock[0] + 30)
            )
        self.assertEqual(seen, [160.0, 150.0])

    def test_an_expired_budget_never_starts_the_workload(self) -> None:
        api = mock.Mock()
        api.exec_create.return_value = {"Id": "exec"}
        fail_stop = mock.Mock()
        strategy = action_execution.RpcExchangeStrategy(
            api=api,
            user="10001:10001",
            workdir=container_spec.CONTAINER_TMP,
            timeout=60,
            maximum=1024,
            transport_errors=(),
            fail_stop=fail_stop,
            cancelled=mock.Mock(),
            close_stream=mock.Mock(),
            deadline=time.monotonic() - 1,
        )
        with self.assertRaises(action_execution.RpcExchangeError) as expired:
            action_execution.rpc_exchange("container", ["command"], b"request", strategy)
        self.assertEqual(
            (expired.exception.kind, expired.exception.condition), ("timeout", "deadline-expired-before-dispatch")
        )
        api.exec_create.assert_not_called()
        fail_stop.assert_not_called()

        clock = [100.0]

        def late_create(*_args, **_kwargs) -> dict[str, str]:
            clock[0] = 200.0  # The budget ran out while the exec was being created.
            return {"Id": "exec"}

        api = mock.Mock()
        api.exec_create.side_effect = late_create
        with (
            mock.patch.object(action_execution.time, "monotonic", side_effect=lambda: clock[0]),
            self.assertRaises(action_execution.RpcExchangeError) as late,
        ):
            action_execution.rpc_exchange(
                "container", ["command"], b"request", dataclasses.replace(strategy, api=api, deadline=150.0)
            )
        self.assertEqual(late.exception.condition, "deadline-expired-before-dispatch")
        api.exec_start.assert_not_called()

    def test_setup_that_outlives_the_budget_fail_stops_and_closes_a_late_stream(self) -> None:
        release = threading.Event()
        stream = SimpleNamespace(_sock=object())
        closed = threading.Event()

        def slow_start(*_args, **_kwargs) -> object:
            release.wait(5)
            return stream

        api = SimpleNamespace(exec_create=lambda *_a, **_k: {"Id": "exec"}, exec_start=slow_start)
        fail_stop = mock.Mock()
        strategy = action_execution.RpcExchangeStrategy(
            api=api,
            user="10001:10001",
            workdir=container_spec.CONTAINER_TMP,
            timeout=0.2,
            maximum=1024,
            transport_errors=(),
            fail_stop=fail_stop,
            cancelled=mock.Mock(),
            close_stream=lambda _stream: closed.set(),
        )
        started = time.monotonic()
        with self.assertRaises(action_execution.RpcExchangeError) as caught:
            action_execution.rpc_exchange("container", ["command"], b"request", strategy)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(caught.exception.kind, "timeout")
        fail_stop.assert_called_once_with()
        release.set()
        self.assertTrue(closed.wait(5))


if __name__ == "__main__":
    unittest.main()
