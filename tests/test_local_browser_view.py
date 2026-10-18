"""One Browser task's screen view: capture on demand, the latest frame only, and an ending bound to its task."""

import queue
import threading
import time
import unittest
from unittest import mock

from browser import view as browser_view
from local.browser import view
from local.browser.runtime import BrowserError

FRAME = b"rgb 1280 800\n" + bytes(browser_view.FRAME_BYTES)
CHANGED = b"rgb 1280 800\n" + b"\x01" * browser_view.FRAME_BYTES
SAME = b"same\n"


class Program:
    """The view program's stdout: the test's ticks in order, and `same` meanwhile, as the program ticks on its own."""

    def __init__(self, *ticks: bytes) -> None:
        self.ticks: queue.Queue[bytes | Exception] = queue.Queue()
        self.buffer = bytearray()
        self.input_closed = threading.Event()
        self.closed = threading.Event()
        for tick in ticks:
            self.send(tick)

    def send(self, tick: bytes | Exception) -> None:
        self.ticks.put(tick)

    def _fill(self, deadline: float) -> None:
        try:
            tick = self.ticks.get(timeout=min(0.02, max(0.0, deadline - time.monotonic())))
        except queue.Empty:
            tick = SAME
        if isinstance(tick, Exception):
            raise tick
        self.buffer.extend(tick)

    def receive_line(self, deadline: float, limit: int) -> bytes:
        while b"\n" not in self.buffer:
            self._fill(deadline)
        line, _, rest = bytes(self.buffer).partition(b"\n")
        self.buffer[:] = rest
        return line

    def receive_exact(self, size: int, deadline: float) -> bytes:
        while len(self.buffer) < size:
            self._fill(deadline)
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def close_input(self) -> None:
        self.input_closed.set()

    def close(self) -> None:
        self.closed.set()


class ViewTests(unittest.TestCase):
    def setUp(self):
        for name, value in {
            "IDLE_SECONDS": 0.3,
            "WAIT_SECONDS": 0.4,
            "TICK_SECONDS": 2.0,
            "MIN_FRAME_SECONDS": 0.0,
            "EXIT_SECONDS": 0.2,
        }.items():
            patcher = mock.patch.object(view, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.programs: list[Program] = []
        self.next_programs: list[Program] = []
        self.records: list[str] = []
        self.exited = mock.Mock(return_value=True)
        self.open_error: Exception | None = None

    def open_program(self):
        if self.open_error is not None:
            raise self.open_error
        program = self.next_programs.pop(0)
        self.programs.append(program)
        return f"exec-{len(self.programs)}", program

    def make(self, *programs: Program, sleep=time.sleep) -> view.View:
        self.next_programs.extend(programs)
        return view.View(self.open_program, self.exited, self.records.append, sleep=sleep)

    def stopped(self, program: Program) -> None:
        self.assertTrue(program.closed.wait(5), "the producer did not stop")

    def test_a_request_starts_capture_and_answers_only_a_newer_frame(self):
        program = Program(FRAME)
        screen = self.make(program)
        self.assertRegex(screen.id, r"^[0-9a-f]{32}$")
        first = screen.frame(0)
        self.assertEqual(first.number, 1)
        self.assertTrue(first.png.startswith(b"\x89PNG"))
        program.send(SAME)
        self.assertIsNone(screen.frame(1))
        program.send(CHANGED)
        self.assertEqual(screen.frame(1).number, 2)
        self.assertEqual(len(self.programs), 1)
        screen.end()
        program.send(SAME)
        self.stopped(program)
        self.assertEqual(self.records, ["started", "stopped"])

    def test_the_task_end_answers_every_waiter_at_once(self):
        program = Program(FRAME)
        screen = self.make(program)
        screen.frame(0)
        with mock.patch.object(view, "WAIT_SECONDS", 30.0):
            waiting = threading.Thread(target=lambda: self.assertRaises(BrowserError, screen.frame, 1))
            started = time.monotonic()
            waiting.start()
            time.sleep(0.1)
            screen.end()
            waiting.join(5)
        self.assertLess(time.monotonic() - started, 5)
        with self.assertRaises(BrowserError) as ended:
            screen.frame(0)
        self.assertEqual(ended.exception.code, "browser-not-running")
        program.send(SAME)
        self.stopped(program)

    def test_capture_stops_when_nobody_asks_and_restarts_after_a_proven_exit(self):
        first, second = Program(FRAME), Program(CHANGED)
        screen = self.make(first, second)
        self.assertEqual(screen.frame(0).number, 1)
        # Ticks keep coming, but no request does: the producer stops within the idle window.
        for _ in range(8):
            first.send(SAME)
        self.stopped(first)
        self.assertTrue(first.input_closed.is_set())
        self.exited.assert_called_once()
        self.assertEqual(self.exited.call_args.args[0], "exec-1")
        self.assertEqual(screen.frame(1).number, 2)
        self.assertEqual(self.records[:3], ["started", "stopped", "started"])
        screen.end()
        second.send(SAME)
        self.stopped(second)

    def test_an_exit_it_cannot_prove_leaves_the_view_unavailable(self):
        self.exited.return_value = False
        program = Program(FRAME)
        screen = self.make(program)
        screen.frame(0)
        for _ in range(8):
            program.send(SAME)
        self.stopped(program)
        self.assertEqual(self.records, ["started", "unavailable"])
        with self.assertRaises(BrowserError) as refused:
            screen.frame(1)
        self.assertEqual(refused.exception.code, "browser-view-unavailable")

    def test_a_program_that_misbehaves_leaves_the_view_unavailable(self):
        program = Program(b"rgb 1920 1080\n")
        screen = self.make(program)
        with self.assertRaises(BrowserError) as refused:
            screen.frame(0)
        self.assertEqual(refused.exception.code, "browser-view-unavailable")
        self.assertTrue(program.closed.is_set())

    def test_a_program_that_cannot_open_leaves_the_view_unavailable(self):
        self.open_error = BrowserError("browser-lost")
        screen = self.make()
        with self.assertRaises(BrowserError) as refused:
            screen.frame(0)
        self.assertEqual(refused.exception.code, "browser-view-unavailable")
        self.exited.assert_not_called()
        self.assertEqual(self.records, ["unavailable"])

    def test_a_program_lost_with_its_task_is_only_the_task_ending(self):
        program = Program(FRAME)
        screen = self.make(program)
        screen.frame(0)
        screen.end()
        program.send(OSError("the Browser channel closed"))
        self.stopped(program)
        self.assertEqual(self.records, ["started", "stopped"])

    def test_frames_are_encoded_no_faster_than_their_interval(self):
        rests: list[float] = []
        program = Program(FRAME, CHANGED)
        with mock.patch.object(view, "MIN_FRAME_SECONDS", 60.0):
            screen = self.make(program, sleep=rests.append)
            screen.frame(0)
            self.assertEqual(screen.frame(1).number, 2)
        self.assertEqual(len(rests), 1)
        self.assertGreater(rests[0], 59)
        screen.end()
        program.send(SAME)
        self.stopped(program)


if __name__ == "__main__":
    unittest.main()
