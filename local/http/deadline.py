"""Absolute request deadlines for the Local Team controller listener.

Team packages its own copy of this pattern: a socket timeout alone restarts on each received byte, so a trickled
request or an idle keep-alive could otherwise hold one of the controller's bounded admission slots indefinitely.
"""

import io
import socket
import time
from http.server import BaseHTTPRequestHandler

HTTP_HEADER_DEADLINE_SECONDS = 10
HTTP_BODY_DEADLINE_SECONDS = 10
HTTP_KEEPALIVE_LIFETIME_SECONDS = 60


class _DeadlineReader(socket.SocketIO):
    """Bound every blocking read by an absolute deadline; a socket timeout alone restarts on each received byte."""

    def __init__(self, sock: socket.socket) -> None:
        super().__init__(sock, "rb")
        self._idle_timeout = sock.gettimeout()
        self.deadline = time.monotonic()

    def readinto(self, buffer) -> int | None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("the HTTP read deadline expired")
        self._sock.settimeout(min(remaining, self._idle_timeout or remaining))
        try:
            return super().readinto(buffer)
        finally:
            self._sock.settimeout(self._idle_timeout)


class DeadlineRequestHandler(BaseHTTPRequestHandler):
    """Bound request headers and body by absolute deadlines and keep-alive reuse by a connection lifetime.

    The per-operation idle timeout is the handler's `timeout`, applied by `setup` before the reader captures it.
    """

    header_deadline: float = HTTP_HEADER_DEADLINE_SECONDS
    body_deadline: float = HTTP_BODY_DEADLINE_SECONDS
    keepalive_lifetime: float = HTTP_KEEPALIVE_LIFETIME_SECONDS
    _lifetime_spent = False

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self._reader = _DeadlineReader(self.connection)
        self.rfile = io.BufferedReader(self._reader)
        self._expires = time.monotonic() + self.keepalive_lifetime
        self._lifetime_spent = False

    def handle_one_request(self) -> None:
        # The idle wait for the next keep-alive request counts toward that request's header deadline.
        self._reader.deadline = time.monotonic() + self.header_deadline
        super().handle_one_request()

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        now = time.monotonic()
        self._reader.deadline = now + self.body_deadline
        self._lifetime_spent = now >= self._expires
        return True

    def end_headers(self) -> None:
        # The response that exhausts the keep-alive lifetime announces the close, so a client never reuses it.
        if self._lifetime_spent and not self.close_connection:
            self.send_header("Connection", "close")
        super().end_headers()
