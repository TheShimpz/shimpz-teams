"""One Browser task's watched screen on the Local Team (ADR-0117, watching over KasmVNC).

A view belongs to exactly one task: its random id is drawn when the task holds the Browser, and it ends when the task
is released or stopped, which interrupts every stream it carries at once. Each viewer's stream is one relay exec into
the display: the display must answer the upgrade exactly before the viewer gets its 101, and the exec must be proven
ended, within a deadline that a stalled Docker inspection cannot extend, once the stream ends; when that proof does
not come, the view stays unavailable for the rest of the task.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from socket import socket

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


class View:
    """The watched screen of one running task."""

    def __init__(
        self,
        open_relay: Open,
        exited: Exited,
        record: Record,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.id = secrets.token_hex(16)
        self._open = open_relay
        self._exited = exited
        self._record = record
        self._clock = clock
        self._lock = threading.Lock()
        self._ended = False
        self._unavailable = False
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

    def carry(self, client: socket, key: str, upgraded: Callable[[bytes], None]) -> None:
        """Carry one viewer: upgrade the display with `key`, hand `upgraded` the 101, then copy until the stream ends.

        Before `upgraded` runs, a failure is a closed BrowserError the viewer can be told; after it, the stream ends.
        """
        with self._lock:
            self._admit()
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
                head, early = tunnel.upgrade(key)
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
