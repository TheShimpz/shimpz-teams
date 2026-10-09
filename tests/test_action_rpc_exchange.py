"""The bounded Action RPC exchange: concurrent draining, the invocation bound, and one absolute deadline."""

import concurrent.futures
import dataclasses
import json
import socket
import struct
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from action import dispatch as action_dispatch
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
        strategy = rpc_strategy(api, timeout=60)

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
        strategy = rpc_strategy(api, timeout=60, fail_stop=fail_stop, deadline=time.monotonic() - 1)
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
        strategy = rpc_strategy(api, timeout=0.2, fail_stop=fail_stop, close_stream=lambda _stream: closed.set())
        started = time.monotonic()
        with self.assertRaises(action_execution.RpcExchangeError) as caught:
            action_execution.rpc_exchange("container", ["command"], b"request", strategy)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(caught.exception.kind, "timeout")
        fail_stop.assert_called_once_with()
        release.set()
        self.assertTrue(closed.wait(5))


def rpc_strategy(api: object, **changes: object) -> action_execution.RpcExchangeStrategy:
    return dataclasses.replace(
        action_execution.RpcExchangeStrategy(
            api=api,
            user="10001:10001",
            workdir=container_spec.CONTAINER_TMP,
            timeout=0.1,
            maximum=1024,
            transport_errors=(),
            fail_stop=mock.Mock(),
            cancelled=mock.Mock(),
            close_stream=mock.Mock(),
        ),
        **changes,
    )


class DockerCallBoundTests(unittest.TestCase):
    def test_abandoned_setup_never_accumulates_workers_and_saturation_refuses_dispatch(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)
        starts: list[str] = []

        def hanging_start(exec_id, **_kwargs):
            starts.append(exec_id)
            release.wait(10)
            return SimpleNamespace(_sock=object())

        api = SimpleNamespace(exec_create=lambda *_a, **_k: {"Id": "exec"}, exec_start=hanging_start)
        strategies = [rpc_strategy(api) for _ in range(action_dispatch.MAX_DOCKER_CALLS)]
        for strategy in strategies:
            with self.assertRaises(action_execution.RpcExchangeError) as timed_out:
                action_execution.rpc_exchange("container", ["command"], b"request", strategy)
            self.assertEqual(timed_out.exception.kind, "timeout")
            strategy.fail_stop.assert_called_once_with()
        saturated = rpc_strategy(SimpleNamespace(exec_create=mock.Mock(), exec_start=mock.Mock()))
        with self.assertRaises(action_execution.RpcExchangeError) as refused:
            action_execution.rpc_exchange("container", ["command"], b"request", saturated)
        self.assertEqual(refused.exception.condition, "deadline-expired-before-dispatch")
        saturated.api.exec_create.assert_not_called()
        saturated.fail_stop.assert_not_called()
        workers = [thread for thread in threading.enumerate() if thread.name.startswith("action-docker")]
        self.assertLessEqual(len(workers), action_dispatch.MAX_DOCKER_CALLS)
        release.set()
        grace = time.monotonic() + 5
        while action_dispatch._DOCKER_CALL_SLOTS._value != action_dispatch.MAX_DOCKER_CALLS:
            self.assertLess(time.monotonic(), grace)
            time.sleep(0.01)
        # Every late stream was closed once its abandoned setup finished.
        for strategy in strategies:
            self.assertEqual(strategy.close_stream.call_count, 1)

    def test_a_call_the_pool_cannot_take_returns_its_slot(self) -> None:
        with (
            mock.patch.object(action_dispatch._DOCKER_CALLS, "submit", side_effect=RuntimeError("shut down")),
            self.assertRaises(RuntimeError),
        ):
            action_dispatch.bounded_call(lambda: None, time.monotonic() + 5)
        self.assertEqual(action_dispatch._DOCKER_CALL_SLOTS._value, action_dispatch.MAX_DOCKER_CALLS)

    def test_a_stopped_turn_stops_waiting_for_docker_capacity(self) -> None:
        saturated = mock.Mock(acquire=mock.Mock(side_effect=lambda timeout: time.sleep(timeout) or False))
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        api = SimpleNamespace(exec_create=mock.Mock(), exec_start=mock.Mock())
        waiting = rpc_strategy(api, timeout=30)
        started = time.monotonic()
        with (
            mock.patch.object(action_dispatch, "_DOCKER_CALL_SLOTS", saturated),
            action_dispatch.observing_stop(stop.is_set),
            self.assertRaises(action_execution.RpcExchangeError) as stopped,
        ):
            action_execution.rpc_exchange("container", ["command"], b"request", waiting)
        # Stop ends the wait within one poll slice, long before the deadline, and nothing was dispatched.
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(action_dispatch.never_dispatched(stopped.exception))
        api.exec_create.assert_not_called()
        waiting.fail_stop.assert_not_called()
        waiting.cancelled.assert_called_once()

    def test_a_call_returns_its_slot_before_its_result_is_published(self) -> None:
        published: list[int] = []

        class Inline:
            """A pool that runs the call inline and records the free slots the moment its result is published."""

            @staticmethod
            def submit(function):
                future = concurrent.futures.Future()
                try:
                    result = function()
                except ValueError as exc:  # A failed call publishes its exception the same way.
                    published.append(action_dispatch._DOCKER_CALL_SLOTS._value)
                    future.set_exception(exc)
                else:
                    published.append(action_dispatch._DOCKER_CALL_SLOTS._value)
                    future.set_result(result)
                return future

        with mock.patch.object(action_dispatch, "_DOCKER_CALLS", Inline):
            self.assertEqual(action_dispatch.bounded_call(lambda: "done", time.monotonic() + 5).result(), "done")
            failed = action_dispatch.bounded_call(mock.Mock(side_effect=ValueError("failed")), time.monotonic() + 5)
            self.assertIsInstance(failed.exception(), ValueError)
        self.assertEqual(published, [action_dispatch.MAX_DOCKER_CALLS] * 2)
        self.assertEqual(action_dispatch._DOCKER_CALL_SLOTS._value, action_dispatch.MAX_DOCKER_CALLS)

    def test_stop_that_wins_after_the_wait_returns_the_slot(self) -> None:
        stopped = iter((False, True))
        with self.assertRaises(action_dispatch.DispatchRefusedError):
            action_dispatch.bounded_call(self.fail, time.monotonic() + 5, lambda: next(stopped))
        self.assertEqual(action_dispatch._DOCKER_CALL_SLOTS._value, action_dispatch.MAX_DOCKER_CALLS)

    def test_stop_never_abandons_the_exit_inspection_of_a_dispatched_workload(self) -> None:
        api = SimpleNamespace(exec_inspect=lambda _exec_id: {"ExitCode": 0})
        with action_dispatch.observing_stop(lambda: True):
            details = action_execution._inspect_exec("exec", rpc_strategy(api), time.monotonic() + 5)
        self.assertEqual(details, {"ExitCode": 0})

    def test_exit_inspection_is_bounded_by_the_same_deadline(self) -> None:
        clock = [100.0]
        calls: list[concurrent.futures.Future] = []
        dispatch = action_dispatch.bounded_call

        def recorded(*args, **kwargs):
            calls.append(dispatch(*args, **kwargs))
            return calls[-1]

        recording = mock.patch.object(action_dispatch, "bounded_call", side_effect=recorded)
        recording.start()
        self.addCleanup(recording.stop)

        def late_inspect(_exec_id):
            clock[0] = 200.0  # The exit status arrives after the deadline.
            return {"ExitCode": 0}

        api = SimpleNamespace(
            exec_create=lambda *_a, **_k: {"Id": "exec"},
            exec_start=lambda *_a, **_k: SimpleNamespace(_sock=object()),
            exec_inspect=late_inspect,
        )
        late = rpc_strategy(api, deadline=160.0)
        with (
            mock.patch.object(action_execution.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(action_execution, "exchange_rpc_frames", return_value=(b"{}", b"")),
            self.assertRaises(action_execution.RpcExchangeError) as caught,
        ):
            action_execution.rpc_exchange("container", ["command"], b"request", late)
        self.assertEqual((caught.exception.kind, caught.exception.condition), ("timeout", "exit-unavailable"))
        late.fail_stop.assert_called_once_with()

        release = threading.Event()
        self.addCleanup(release.set)
        hanging = SimpleNamespace(
            exec_create=lambda *_a, **_k: {"Id": "exec"},
            exec_start=lambda *_a, **_k: SimpleNamespace(_sock=object()),
            exec_inspect=lambda _exec_id: release.wait(10) or {"ExitCode": 0},
        )
        slow = rpc_strategy(hanging, timeout=0.3)
        started = time.monotonic()
        with (
            mock.patch.object(action_execution, "exchange_rpc_frames", return_value=(b"{}", b"")),
            self.assertRaises(action_execution.RpcExchangeError) as stalled,
        ):
            action_execution.rpc_exchange("container", ["command"], b"request", slow)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(stalled.exception.kind, "timeout")
        slow.fail_stop.assert_called_once_with()
        release.set()
        # An inspection the turn stopped waiting for keeps its slot until it finishes, and publishes its result only
        # after returning the slot: waiting for every call this test made leaves no slot held behind it.
        concurrent.futures.wait(calls, timeout=10)
        self.assertEqual(action_dispatch._DOCKER_CALL_SLOTS._value, action_dispatch.MAX_DOCKER_CALLS)


if __name__ == "__main__":
    unittest.main()
