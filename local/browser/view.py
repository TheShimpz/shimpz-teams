"""One Browser task's watched screen on the Local Team (ADR-0117, watching over KasmVNC).

A view belongs to exactly one task: its random id is drawn when the task holds the Browser, and it ends when the task
is released or stopped, which interrupts every stream it carries at once. Each viewer's stream is one relay exec into
the display: the display must answer the upgrade exactly before the viewer gets its 101, and the exec must be proven
ended, within a deadline that a stalled Docker inspection cannot extend, once the stream ends; when that proof does
not come, the view stays unavailable for the rest of the task.

A Browser a person may control (ADR-0117, Sign-ins amendment item 3) authenticates every stream: watching carries the
read-only credential and control the read-write one. One person controls at a time; taking control ends every other
stream of the view and admits none while it lasts, and when control ends Team releases every held key and button
before anything else acts. A release that cannot be proven leaves the view unavailable.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from socket import socket

from local.browser import release as input_release
from local.browser.channel import ChannelError, ExecStream
from local.browser.runtime import BrowserError, ExecStartError
from local.browser.tunnel import Tunnel, TunnelError
from protocol.http.v1 import browser_view as view_protocol

# The deadline to prove a stream's relay exec ended.
EXIT_SECONDS = 2.0

# Open one relay exec into the display: its id and its channel.
Open = Callable[[], tuple[str, ExecStream]]
# Whether an exec ended, proven before an absolute deadline.
Exited = Callable[[str, float], bool]
# Audit one stream state, never a pixel: `started`, then `ended` with the bytes carried each way.
Record = Callable[[str], None]
# The display credential a stream's upgrade carries: the read-write one for control, else the read-only one.
Authorization = Callable[[bool], str]


class View:
    """The watched screen of one running task."""

    def __init__(
        self,
        open_relay: Open,
        exited: Exited,
        record: Record,
        *,
        authorization: Authorization | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.id = secrets.token_hex(16)
        self._open = open_relay
        self._exited = exited
        self._record = record
        self._authorization = authorization
        self._clock = clock
        self._lock = threading.Lock()
        self._ended = False
        self._unavailable = False
        self._controlled = False
        self._tunnels: set[Tunnel] = set()

    def end(self) -> None:
        """The task ended: every stream of this view stops at once, and no new one starts."""
        with self._lock:
            self._ended = True
            tunnels = tuple(self._tunnels)
        for tunnel in tunnels:
            tunnel.interrupt()

    def _admit(self) -> None:
        if self._ended:
            raise BrowserError(view_protocol.NOT_RUNNING)
        if self._unavailable:
            raise BrowserError(view_protocol.UNAVAILABLE)

    def carry(self, client: socket, key: str, upgraded: Callable[[bytes], None], *, control: bool = False) -> None:
        """Carry one viewer: upgrade the display with `key`, hand `upgraded` the 101, then copy until the stream ends.

        Before `upgraded` runs, a failure is a closed BrowserError the viewer can be told; after it, the stream ends.
        """
        with self._lock:
            self._admit()
            if control and self._authorization is None:
                raise BrowserError(view_protocol.UNAVAILABLE)
            if self._controlled:
                raise BrowserError(view_protocol.BUSY)
            if control:
                self._controlled = True
                watching = tuple(self._tunnels)
        if control:
            # The person's control is private: every other stream ends, and none starts while it lasts.
            for tunnel in watching:
                tunnel.interrupt()
            try:
                self._carry(client, key, upgraded, control)
            finally:
                self._release()
            return
        self._carry(client, key, upgraded, control)

    def _release(self) -> None:
        """Release what the person held before anything else acts; an unproven release leaves the view unavailable."""
        try:
            input_release.release(self._open, self._authorization(True), clock=self._clock)
            released = True
        except input_release.ReleaseError, BrowserError, ChannelError, OSError:
            released = False
        with self._lock:
            self._controlled = False
            if not released:
                self._unavailable = True
        self._record(f"control-ended:released={int(released)}")

    def _carry(self, client: socket, key: str, upgraded: Callable[[bytes], None], control: bool) -> None:
        try:
            exec_id, display = self._open()
        except ExecStartError as exc:
            # The relay may have started without a channel to it: it must be proven ended like any other.
            if not self._exited(exc.exec_id, self._clock() + EXIT_SECONDS):
                with self._lock:
                    self._unavailable = True
            raise BrowserError(view_protocol.UNAVAILABLE) from exc
        except BrowserError as exc:
            raise BrowserError(view_protocol.UNAVAILABLE) from exc
        tunnel = Tunnel(client, display, self._clock)
        with self._lock:
            self._tunnels.add(tunnel)
            ended = self._ended
        try:
            if ended:
                raise BrowserError(view_protocol.NOT_RUNNING)
            self._record("started")
            try:
                authorization = self._authorization(control) if self._authorization is not None else None
                head, early = tunnel.upgrade(key, authorization)
            except TunnelError as exc:
                raise BrowserError(view_protocol.UNAVAILABLE) from exc
            upgraded(head + early)
            tunnel.carried["to_viewer"] += len(early)
            # A side that failed after the upgrade only ends the stream.
            with suppress(TunnelError, ChannelError, OSError):
                tunnel.splice(lambda: self._ended)
        finally:
            with self._lock:
                self._tunnels.discard(tunnel)
            self._finish(exec_id, display, tunnel)

    def _finish(self, exec_id: str, display: ExecStream, tunnel: Tunnel) -> None:
        """End the relay's input and prove its exec ended before the deadline; then close the channel."""
        display.close_input()
        try:
            proven = self._exited(exec_id, self._clock() + EXIT_SECONDS)
        finally:
            display.close()
        if not proven:
            with self._lock:
                self._unavailable = True
        carried = tunnel.carried
        self._record(f"ended:proven={int(proven)}:to_viewer={carried['to_viewer']}:to_display={carried['to_display']}")
