"""Local Stop aborts the Brain request its chat turn is blocked on (ADR-0079)."""

from __future__ import annotations

import http.client
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_brain_runtime_client import context

from chat import orchestrator as chat_orchestrator
from inference import abort as brain_abort
from inference import client as brain_runtime_client
from local import app as local_app


class _SilentBrain:
    """Accepts one Brain request, never answers, and records when Team hangs up."""

    def __init__(self) -> None:
        self.received = threading.Event()
        self.hung_up = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        connection, _address = self._listener.accept()
        with connection:
            data = b""
            while b"\r\n\r\n" not in data:
                data += connection.recv(65536)
            self.received.set()
            while connection.recv(65536):
                pass
            self.hung_up.set()

    def close(self) -> None:
        self._listener.close()


def _submit(work) -> Future:
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(work)
    executor.shutdown(wait=False)
    return future


def _silent_client(case: unittest.TestCase, client_module) -> tuple[_SilentBrain, object]:
    """A silent Brain and a ``client_module`` runtime client pointed at it, both cleaned up after ``case``."""
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    token_file = Path(directory.name) / "token"
    token_file.write_text("brain-runtime-token", encoding="utf-8")
    brain = _SilentBrain()
    case.addCleanup(brain.close)
    return brain, client_module.BrainRuntimeClient(base_url=f"http://127.0.0.1:{brain.port}", token_file=token_file)


class LocalStopAbortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.brain, self.client = _silent_client(self, brain_runtime_client)
        team_lock = threading.RLock()
        self.service = local_app.ChatTurnService(
            local_app.ChatTurnDependencies(
                integration_challenges=SimpleNamespace(withdraw_team=lambda _team_id: None),
                oauth_pkce=SimpleNamespace(cancel_team=lambda _team_id: None),
                lock_for=lambda _team_id: team_lock,
            )
        )
        self.service._delete_chat_continuation = lambda _team_id: False
        self.service.assistant_lifecycle = SimpleNamespace(_network=lambda _team_id: None)

    def _blocked_turn(self) -> Future:
        def turn() -> None:
            with self.service._exclusive_chat_turn("team_1"):
                self.client.start(context("sk-test-0123456789abcdef"), "Hello", conversation=())

        future = _submit(turn)
        self.assertTrue(self.brain.received.wait(5))
        return future

    def test_stop_wakes_the_blocked_brain_request_and_releases_the_turn(self):
        future = self._blocked_turn()
        started = time.monotonic()
        result = self.service.stop_chat("team_1")

        self.assertIsInstance(future.exception(5), brain_runtime_client.BrainRuntimeError)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(result["accepted"])
        self.assertTrue(self.brain.hung_up.wait(5))
        self.assertEqual(self.service._brain_aborts, {})
        # The Team lock is free for the next turn at once.
        with self.service._exclusive_chat_turn("team_1"):
            pass

    def test_destroying_the_team_aborts_its_brain_request(self):
        future = self._blocked_turn()
        self.service._cancel_chat_for_destroy("team_1")
        self.assertIsInstance(future.exception(5), brain_runtime_client.BrainRuntimeError)
        self.assertTrue(self.brain.hung_up.wait(5))
        self.assertEqual(self.service._brain_aborts, {})


class RequestAbortTests(unittest.TestCase):
    def _client(self, connection) -> brain_runtime_client.BrainRuntimeClient:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        token_file = Path(directory.name) / "token"
        token_file.write_text("brain-runtime-token", encoding="utf-8")
        return brain_runtime_client.BrainRuntimeClient(
            token_file=token_file, connection_factory=lambda *_args: connection
        )

    def test_an_abort_before_the_request_never_connects(self):
        connection = mock.Mock()
        handle = brain_abort.RequestAbort()
        handle.abort()
        with (
            brain_abort.abortable(handle),
            self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "stopped"),
        ):
            self._client(connection).delete_thread("team-thread")
        connection.connect.assert_not_called()
        connection.close.assert_called_once_with()

    def test_an_abort_during_the_bounded_connect_fails_before_sending(self):
        connection = mock.Mock(sock=None)
        handle = brain_abort.RequestAbort()

        def connect() -> None:
            handle.abort()
            connection.sock = mock.Mock()

        connection.connect.side_effect = connect
        with (
            brain_abort.abortable(handle),
            self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "stopped"),
        ):
            self._client(connection).delete_thread("team-thread")
        connection.request.assert_not_called()
        connection.sock.settimeout.assert_called_once_with(brain_runtime_client.RESPONSE_TIMEOUT_SECONDS)

    def test_the_abort_shuts_down_only_an_attached_socket_and_tolerates_a_closed_one(self):
        handle = brain_abort.RequestAbort()
        connection = mock.Mock()
        connection.sock.shutdown.side_effect = OSError("already closed")
        handle.attach(connection)
        handle.abort()
        connection.sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        handle.detach()
        handle.abort()
        connection.sock.shutdown.assert_called_once()

    def test_protocol_failures_and_an_unscoped_request_keep_their_meaning(self):
        connection = mock.Mock()
        connection.getresponse.side_effect = http.client.BadStatusLine("garbage")
        with self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "unavailable"):
            self._client(connection).delete_thread("team-thread")
        connection.close.assert_called_once_with()
        self.assertEqual(brain_runtime_client.CONNECT_TIMEOUT_SECONDS, 5.0)


class StoppedBrainFailureTests(unittest.TestCase):
    def _run(self, runtime, cancelled):
        return chat_orchestrator.run(
            runtime,
            context("sk-test-0123456789abcdef"),
            "Hello",
            chat_orchestrator.ChatStrategy(
                lambda _assistant, _action, payload: payload,
                lambda _request: {"message": "ok"},
                cancelled=cancelled,
            ),
        )

    def test_a_brain_failure_is_a_stopped_turn_only_when_stop_won(self):
        stopped: list[bool] = []

        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                stopped.append(True)
                raise brain_runtime_client.BrainRuntimeError("Brain runtime is unavailable")

        with self.assertRaises(chat_orchestrator.ChatStoppedError):
            self._run(Runtime(), lambda: bool(stopped))
        with self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "unavailable"):
            self._run(Runtime(), lambda: False)


if __name__ == "__main__":
    unittest.main()
