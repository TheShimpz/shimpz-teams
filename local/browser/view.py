"""One Browser task's screen view on the Local Team (ADR-0117, M2 Watch).

A view belongs to exactly one task: its random id is drawn when the task holds the Browser, and it ends when the task
is released or stopped. Capture runs only on demand: the first frame request starts a producer that reads
`browser-host view`, and the producer stops at most IDLE_SECONDS after the last frame request ended, or when the task
ends.
Only the latest frame is kept, so a slow viewer skips frames and never holds back the producer, the task, or Stop.
A producer's exec must be proven ended before another starts; when Team cannot prove it within its deadline, or the
program misbehaves while the task runs, the view stays unavailable for the rest of the task.
"""

from __future__ import annotations

import contextvars
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from browser import view as browser_view
from local.browser.channel import ExecStream
from local.browser.runtime import BrowserError
from protocol.http.v1 import browser_view as view_protocol

# How long a producer keeps capturing after the last frame request, and how long one request waits for a new frame.
IDLE_SECONDS = 10.0
WAIT_SECONDS = 2.0
# The longest one tick may take to arrive, the shortest time between two encoded frames, and the deadline to prove a
# stopped producer's exec ended.
TICK_SECONDS = 5.0
MIN_FRAME_SECONDS = 0.4
EXIT_SECONDS = 2.0

# Open the view program: its exec id and its stdout.
Open = Callable[[], tuple[str, ExecStream]]
# Whether an exec ended, proven before an absolute deadline.
Exited = Callable[[str, float], bool]
# Audit one capture state: `started`, `stopped`, or `unavailable`, never a pixel.
Record = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class Frame:
    """One PNG Team built, numbered from 1 within its view."""

    number: int
    png: bytes


class View:
    """The screen view of one running task."""

    def __init__(
        self,
        open_program: Open,
        exited: Exited,
        record: Record,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.id = secrets.token_hex(16)
        self._open = open_program
        self._exited = exited
        self._record = record
        self._clock = clock
        self._sleep = sleep
        self._condition = threading.Condition()
        self._frame: Frame | None = None
        self._ended = False
        self._unavailable = False
        self._producing = False
        self._requested = 0.0
        # Requests waiting for a frame now: each is demand however long it waits.
        self._waiting = 0

    def end(self) -> None:
        """The task ended: every waiter answers at once that it no longer runs, and the producer stops."""
        with self._condition:
            self._ended = True
            self._condition.notify_all()

    def frame(self, after: int) -> Frame | None:
        """The latest frame numbered after `after`, waiting at most WAIT_SECONDS; None when none came."""
        deadline = self._clock() + WAIT_SECONDS
        with self._condition:
            self._admit()
            self._requested = self._clock()
            if not self._producing:
                self._producing = True
                # The producer audits under the principal of the request that started it.
                context = contextvars.copy_context()
                threading.Thread(target=context.run, args=(self._produce,), name="browser-view", daemon=True).start()
            self._waiting += 1
            try:
                while self._frame is None or self._frame.number <= after:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        return None
                    self._condition.wait(remaining)
                    self._admit()
                return self._frame
            finally:
                self._waiting -= 1
                self._requested = self._clock()

    def _admit(self) -> None:
        if self._ended:
            raise BrowserError(view_protocol.NOT_RUNNING)
        if self._unavailable:
            raise BrowserError(view_protocol.UNAVAILABLE)

    def _wanted(self) -> bool:
        with self._condition:
            return not self._ended and (self._waiting > 0 or self._clock() - self._requested <= IDLE_SECONDS)

    def _produce(self) -> None:
        opened: tuple[str, ExecStream] | None = None
        failed = False
        try:
            opened = self._open()
            self._record("started")
            self._capture(opened[1])
        except BrowserError, OSError, browser_view.ViewError:
            failed = True
        finally:
            proven = opened is None or self._stop(*opened)
            with self._condition:
                self._producing = False
                if not proven or (failed and not self._ended):
                    self._unavailable = True
                unavailable = self._unavailable
                self._condition.notify_all()
            self._record("unavailable" if unavailable else "stopped")

    def _capture(self, stream: ExecStream) -> None:
        encoded: float | None = None
        while self._wanted():
            rgb = browser_view.next_tick(stream, self._clock() + TICK_SECONDS)
            if rgb is None:
                continue
            if encoded is not None and (rest := MIN_FRAME_SECONDS - (self._clock() - encoded)) > 0:
                self._sleep(rest)
            image = browser_view.png(rgb)
            encoded = self._clock()
            with self._condition:
                number = 1 if self._frame is None else self._frame.number + 1
                self._frame = Frame(number, image)
                self._condition.notify_all()

    def _stop(self, exec_id: str, stream: ExecStream) -> bool:
        """End the program's input and prove its exec ended before the deadline; then close the channel."""
        stream.close_input()
        try:
            return self._exited(exec_id, self._clock() + EXIT_SECONDS)
        finally:
            stream.close()
