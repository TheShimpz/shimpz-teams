"""One viewer's WebSocket carried from Admin to the Browser's display (ADR-0117, watching over KasmVNC).

Team never reads the RFB stream it carries. It sends the display one canonical upgrade request bearing the viewer's own
key, admits only the display's exact 101 answer for that key before anything else passes, and then copies bytes both
ways: each direction buffers at most BUFFER_BYTES (a full buffer stops reading its source), a side that takes nothing
for WRITE_SECONDS ends the stream, and no stream outlives LIFE_SECONDS. Interrupting the tunnel shuts both sides down,
so a waiting copy wakes at once.
"""

from __future__ import annotations

import select
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress

from browser import task as browser_task
from local.browser.channel import ChannelError, ExecStream
from protocol.http.v1 import browser_view as view_protocol

UPGRADE_SECONDS = 5.0
MAX_UPGRADE_BYTES = 4096
WRITE_SECONDS = 10.0
BUFFER_BYTES = 1024 * 1024
# A stream outlives the longest Browser task, its start and its ingest included, so no task outlives its Watch stream
# (ADR-0117, 2026-10-22 amendment §7); the view ends every stream when its task ends.
LIFE_SECONDS = browser_task.MAX_SECONDS + 5 * 60.0
CHUNK = 64 * 1024
TICK_SECONDS = 0.5
# The display answers only an upgrade that names an origin; its own loopback origin is the one it is given.
DISPLAY_ORIGIN = "http://127.0.0.1:6901"


class TunnelError(RuntimeError):
    """The display did not answer the upgrade exactly, or a side of the stream failed."""


def upgrade_request(key: str, authorization: str | None = None) -> bytes:
    """The one upgrade request the display receives, whatever the viewer sent.

    A controlled Browser's display authenticates every stream: Team adds the credential of the stream's role
    (ADR-0117, Sign-ins amendment item 3), which never comes from the viewer.
    """
    lines = (
        "GET /websockify HTTP/1.1",
        "Host: 127.0.0.1:6901",
        f"Origin: {DISPLAY_ORIGIN}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        f"Sec-WebSocket-Version: {view_protocol.WEBSOCKET_VERSION}",
        f"Sec-WebSocket-Protocol: {view_protocol.SUBPROTOCOL}",
        *((f"Authorization: {authorization}",) if authorization is not None else ()),
    )
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def admitted(head: list[str], key: str) -> bool:
    """Whether the display's answer is exactly the 101 for `key`: each header once, no extension, `binary` chosen."""
    if not head or not head[0].startswith("HTTP/1.1 101 "):
        return False
    headers: dict[str, str] = {}
    for line in head[1:]:
        name, separator, value = line.partition(":")
        name = name.strip().lower()
        if not separator or not name or name in headers:
            return False
        headers[name] = value.strip()
    tokens = {token.strip().lower() for token in headers.get("connection", "").split(",")}
    return (
        headers.get("upgrade", "").lower() == "websocket"
        and "upgrade" in tokens
        and headers.get("sec-websocket-accept") == view_protocol.accept(key)
        and headers.get("sec-websocket-protocol") == view_protocol.SUBPROTOCOL
        and "sec-websocket-extensions" not in headers
    )


def answer(key: str) -> bytes:
    """The 101 Team gives the viewer once the display admitted the same key."""
    lines = (
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {view_protocol.accept(key)}",
        f"Sec-WebSocket-Protocol: {view_protocol.SUBPROTOCOL}",
    )
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


class Tunnel:
    """The bytes between one viewer's connection and one relay exec into the display."""

    def __init__(self, client: socket.socket, display: ExecStream, clock: Callable[[], float] = time.monotonic) -> None:
        self._client = client
        self._display = display
        self._clock = clock
        self._interrupted = threading.Event()
        self.carried = {"to_viewer": 0, "to_display": 0}

    def interrupt(self) -> None:
        """End the stream from another thread: both sides shut down, so a waiting copy wakes."""
        self._interrupted.set()
        self._display.shutdown()
        with suppress(OSError):
            self._client.shutdown(socket.SHUT_RDWR)

    def upgrade(self, key: str, authorization: str | None = None) -> tuple[bytes, bytes]:
        """Upgrade the display with the viewer's key; the 101 to send the viewer, and any display bytes already read."""
        deadline = self._clock() + UPGRADE_SECONDS
        try:
            self._display.send(upgrade_request(key, authorization), deadline)
            head: list[str] = []
            size = 0
            while True:
                line = self._display.receive_line(deadline, MAX_UPGRADE_BYTES)
                size += len(line) + 1
                if size > MAX_UPGRADE_BYTES:
                    raise TunnelError("the display's upgrade answer is oversized")
                text = line.decode("latin-1").removesuffix("\r")
                if not text:
                    break
                head.append(text)
        except ChannelError as exc:
            raise TunnelError("the display did not answer the upgrade") from exc
        if not admitted(head, key):
            raise TunnelError("the display's upgrade answer is not the exact 101")
        return answer(key), self._display.take(self._display.buffered())

    def splice(self, ended: Callable[[], bool]) -> None:
        """Copy both ways until a side ends, a side stalls, `ended()` says so, or the stream's life runs out."""
        self._client.setblocking(False)
        to_viewer, to_display = bytearray(), bytearray()
        life = self._clock() + LIFE_SECONDS
        viewer_progress = display_progress = self._clock()
        while not self._interrupted.is_set() and not ended() and self._clock() < life:
            now = self._clock()
            if (to_viewer and now - viewer_progress > WRITE_SECONDS) or (
                to_display and now - display_progress > WRITE_SECONDS
            ):
                raise TunnelError("a side of the stream stalled")
            # Display bytes already read but not yet buffered for the viewer move first, so a quiet display never
            # strands the end of an update.
            to_viewer.extend(self._display.take(BUFFER_BYTES - len(to_viewer)))
            readers = [
                source
                for source, room in ((self._display, to_viewer), (self._client, to_display))
                if len(room) < BUFFER_BYTES
            ]
            writers = [sink for sink, data in ((self._client, to_viewer), (self._display, to_display)) if data]
            readable, writable, _ = select.select(readers, writers, [], TICK_SECONDS)
            # Each read takes at most the room its buffer has left, so neither direction ever holds more.
            if self._display in readable:
                if not self._display.pump(BUFFER_BYTES - len(to_viewer)):
                    return
                to_viewer.extend(self._display.take(BUFFER_BYTES - len(to_viewer)))
            if self._client in readable:
                data = self._receive(BUFFER_BYTES - len(to_display))
                if data is None:
                    return
                to_display.extend(data)
            if self._client in writable:
                sent = self._send(to_viewer)
                del to_viewer[:sent]
                self.carried["to_viewer"] += sent
                viewer_progress = self._clock() if sent or not to_viewer else viewer_progress
            if self._display in writable:
                sent = self._display.send_some(bytes(to_display))
                del to_display[:sent]
                self.carried["to_display"] += sent
                display_progress = self._clock() if sent or not to_display else display_progress
            if not to_viewer:
                viewer_progress = self._clock()
            if not to_display:
                display_progress = self._clock()

    def _receive(self, room: int) -> bytes | None:
        try:
            data = self._client.recv(min(CHUNK, room))
        except BlockingIOError:
            return b""
        except OSError:
            return None
        return data or None

    def _send(self, data: bytearray) -> int:
        try:
            return self._client.send(data)
        except BlockingIOError:
            return 0
        except OSError as exc:
            raise TunnelError("the viewer's connection failed") from exc
