"""One Browser task's watched screen: a stream per viewer, bound to its task and ended with it."""

import socket
import threading
import time
import unittest
from unittest import mock

from local.browser import view
from local.browser.runtime import BrowserError, ExecStartError
from tests.test_local_browser_tunnel import KEY, Display, admitted_answer


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.records: list[str] = []
        self.exited = mock.Mock(return_value=True)
        self.displays: list[Display] = []
        self.open_error: Exception | None = None
        self.viewer, self.team_viewer = socket.socketpair()
        self.addCleanup(self.viewer.close)
        self.addCleanup(self.team_viewer.close)

    def open_relay(self):
        if self.open_error is not None:
            raise self.open_error
        display = Display()
        self.addCleanup(display.close)
        self.displays.append(display)
        return f"exec-{len(self.displays)}", display.stream

    def make(self) -> view.View:
        return view.View(lambda: self.open_relay(), self.exited, self.records.append)

    def carry_in_background(self, screen: view.View, answers: list[bytes]):
        outcome: dict[str, object] = {}

        def run():
            try:
                screen.carry(self.team_viewer, KEY, answers.append)
            except BrowserError as exc:
                outcome["error"] = exc.code

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, outcome

    def display_answers(self, answer: bytes) -> Display:
        deadline = time.monotonic() + 5
        while not self.displays and time.monotonic() < deadline:
            time.sleep(0.01)
        display = self.displays[-1]
        display.read_request()
        display.write(answer)
        return display

    def test_a_viewer_is_carried_until_the_task_ends(self):
        screen = self.make()
        self.assertRegex(screen.id, r"^[0-9a-f]{32}$")
        answers: list[bytes] = []
        worker, outcome = self.carry_in_background(screen, answers)
        display = self.display_answers(admitted_answer() + b"RFB")
        deadline = time.monotonic() + 5
        while not answers and time.monotonic() < deadline:
            time.sleep(0.01)
        display.write(b"pixels")
        self.viewer.settimeout(5)
        received = b""
        while len(received) < len(b"pixels"):
            received += self.viewer.recv(100)
        self.assertEqual(received, b"pixels")
        screen.end()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome, {})
        self.assertTrue(answers[0].startswith(b"HTTP/1.1 101"))
        self.assertTrue(answers[0].endswith(b"\r\n\r\nRFB"))
        self.exited.assert_called_once()
        self.assertEqual(self.records[0], "started")
        self.assertRegex(self.records[1], r"^ended:proven=1:to_viewer=9:to_display=0$")
        with self.assertRaises(BrowserError) as ended:
            screen.carry(self.team_viewer, KEY, answers.append)
        self.assertEqual(ended.exception.code, "browser-not-running")

    def test_a_display_that_refuses_the_upgrade_leaves_the_viewer_a_closed_refusal(self):
        screen = self.make()
        answers: list[bytes] = []
        worker, outcome = self.carry_in_background(screen, answers)
        self.display_answers(admitted_answer().replace(b"101 Switching", b"403 Forbidden"))
        worker.join(5)
        self.assertEqual(outcome, {"error": "browser-view-unavailable"})
        self.assertEqual(answers, [])
        self.exited.assert_called_once()

    def test_a_relay_that_cannot_open_is_unavailable(self):
        self.open_error = BrowserError("browser-lost")
        with self.assertRaises(BrowserError) as refused:
            self.make().carry(self.team_viewer, KEY, lambda _first: None)
        self.assertEqual(refused.exception.code, "browser-view-unavailable")
        self.exited.assert_not_called()

    def test_a_relay_that_started_without_a_channel_must_be_proven_ended(self):
        self.open_error = ExecStartError("exec-lost")
        screen = self.make()
        with self.assertRaises(BrowserError) as refused:
            screen.carry(self.team_viewer, KEY, lambda _first: None)
        self.assertEqual(refused.exception.code, "browser-view-unavailable")
        self.assertEqual(self.exited.call_args.args[0], "exec-lost")
        self.open_error = None
        self.exited.return_value = False
        self.open_error = ExecStartError("exec-unproven")
        with self.assertRaises(BrowserError):
            screen.carry(self.team_viewer, KEY, lambda _first: None)
        # Unproven, it leaves the view unavailable for the rest of the task: no further relay opens.
        self.open_error = None
        with self.assertRaises(BrowserError) as refused:
            screen.carry(self.team_viewer, KEY, lambda _first: None)
        self.assertEqual(refused.exception.code, "browser-view-unavailable")
        self.assertEqual(self.displays, [])

    def test_an_exit_it_cannot_prove_leaves_the_view_unavailable(self):
        self.exited.return_value = False
        screen = self.make()
        worker, _outcome = self.carry_in_background(screen, [])
        display = self.display_answers(admitted_answer())
        display.display_side.shutdown(socket.SHUT_WR)
        worker.join(5)
        self.assertRegex(self.records[-1], r"^ended:proven=0:")
        with self.assertRaises(BrowserError) as refused:
            screen.carry(self.team_viewer, KEY, lambda _first: None)
        self.assertEqual(refused.exception.code, "browser-view-unavailable")

    def test_a_task_that_ends_while_the_relay_opens_starts_no_stream(self):
        screen = self.make()
        original = self.open_relay

        def ending():
            opened = original()
            screen.end()
            return opened

        self.open_relay = ending
        with self.assertRaises(BrowserError) as refused:
            screen.carry(self.team_viewer, KEY, lambda _first: None)
        self.assertEqual(refused.exception.code, "browser-not-running")
        self.exited.assert_called_once()


if __name__ == "__main__":
    unittest.main()
