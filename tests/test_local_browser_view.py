"""One Browser task's watched screen: a stream per viewer, bound to its task and ended with it."""

import socket
import threading
import time
import unittest
from unittest import mock

from local.browser import view
from local.browser.credentials import Credentials
from local.browser.runtime import BrowserError, ExecStartError
from tests.test_local_browser_tunnel import KEY, Display, admitted_answer


class ViewCase(unittest.TestCase):
    """A view over scripted displays: each relay the view opens is a fresh `Display` the test answers."""

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


class ViewTests(ViewCase):
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


class ControlTests(ViewCase):
    """A Browser a person may control: credentials per role, one controller, private control, and the release."""

    def make(self) -> view.View:
        self.credentials = Credentials.draw()
        return view.View(
            lambda: self.open_relay(), self.exited, self.records.append, authorization=self.credentials.authorization
        )

    def carry_in_background(self, screen, answers, control=False, client=None):
        outcome: dict[str, object] = {}

        def run():
            try:
                screen.carry(client or self.team_viewer, KEY, answers.append, control=control)
            except BrowserError as exc:
                outcome["error"] = exc.code

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        return worker, outcome

    def answered(self, count: int) -> Display:
        deadline = time.monotonic() + 5
        while len(self.displays) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        display = self.displays[count - 1]
        self.requests.append(display.read_request())
        display.write(admitted_answer() + b"RFB")
        return display

    def setUp(self):
        super().setUp()
        self.requests: list[bytes] = []

    def test_an_agents_view_offers_no_control(self):
        screen = view.View(lambda: self.open_relay(), self.exited, self.records.append)
        with self.assertRaises(BrowserError) as refused:
            screen.carry(self.team_viewer, KEY, lambda _first: None, control=True)
        self.assertEqual(refused.exception.code, "browser-view-unavailable")

    def test_control_is_private_and_ends_with_every_button_released(self):
        screen = self.make()
        watcher_answers: list[bytes] = []
        watcher, watched = self.carry_in_background(screen, watcher_answers)
        self.answered(1)
        controller_side, controller_team = socket.socketpair()
        self.addCleanup(controller_side.close)
        self.addCleanup(controller_team.close)
        with mock.patch.object(view.input_release, "release") as released:
            controlling, controlled = self.carry_in_background(screen, [], control=True, client=controller_team)
            self.answered(2)
            # Taking control ended the watcher's stream at once.
            watcher.join(5)
            self.assertFalse(watcher.is_alive())
            self.assertEqual(watched, {})
            with self.assertRaises(BrowserError) as busy:
                screen.carry(self.viewer, KEY, lambda _first: None)
            self.assertEqual(busy.exception.code, "browser-view-busy")
            screen.end()
            controlling.join(5)
        self.assertEqual(controlled, {})
        self.assertIn(f"Authorization: {self.credentials.authorization(False)}".encode(), self.requests[0])
        self.assertIn(f"Authorization: {self.credentials.authorization(True)}".encode(), self.requests[1])
        released.assert_called_once()
        self.assertEqual(released.call_args.args[2], self.credentials.authorization(True))
        self.assertIn("control-ended:released=1", self.records)

    def test_a_watcher_whose_relay_opened_as_control_began_is_refused_before_any_frame(self):
        screen = self.make()
        opening, admitted = threading.Event(), threading.Event()
        plain_open = self.open_relay

        def slow_open():
            # The watcher was admitted; its relay opens only once the person took control.
            admitted.set()
            opening.wait(5)
            return plain_open()

        self.open_relay = slow_open
        watcher, watched = self.carry_in_background(screen, [])
        admitted.wait(5)
        self.open_relay = plain_open
        with mock.patch.object(view.input_release, "release"):
            controlling, _ = self.carry_in_background(screen, [], control=True)
            self.answered(1)
            opening.set()
            watcher.join(5)
            self.assertEqual(watched, {"error": "browser-view-busy"})
            screen.end()
            controlling.join(5)
        self.assertTrue(screen.quiesce(time.monotonic() + 5))

    def test_a_private_view_admits_its_person_alone_and_quiesces_only_once_released(self):
        self.credentials = Credentials.draw()
        screen = view.View(
            lambda: self.open_relay(),
            self.exited,
            self.records.append,
            authorization=self.credentials.authorization,
            private=True,
        )
        with self.assertRaises(BrowserError) as refused:
            screen.carry(self.team_viewer, KEY, lambda _first: None)
        self.assertEqual(refused.exception.code, "browser-not-running")
        gate = threading.Event()

        def held_release(*_args, **_kwargs):
            gate.wait(5)

        with mock.patch.object(view.input_release, "release", side_effect=held_release):
            controlling, _ = self.carry_in_background(screen, [], control=True)
            self.answered(1)
            screen.end()
            # Control is still being released: the view is not quiet yet.
            self.assertFalse(screen.quiesce(time.monotonic() + 0.2))
            gate.set()
            self.assertTrue(screen.quiesce(time.monotonic() + 5))
            controlling.join(5)

    def test_a_release_that_fails_leaves_the_view_unavailable(self):
        screen = self.make()
        with mock.patch.object(view.input_release, "release", side_effect=view.input_release.ReleaseError("held")):
            controlling, _controlled = self.carry_in_background(screen, [], control=True)
            display = self.answered(1)
            display.display_side.shutdown(socket.SHUT_RDWR)
            controlling.join(5)
        self.assertIn("control-ended:released=0", self.records)
        with self.assertRaises(BrowserError) as refused:
            screen.carry(self.viewer, KEY, lambda _first: None)
        self.assertEqual(refused.exception.code, "browser-view-unavailable")
        screen.end()
        self.assertFalse(screen.quiesce(time.monotonic() + 1))


if __name__ == "__main__":
    unittest.main()
