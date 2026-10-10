"""Absolute request deadlines for the Local Team controller listener.

Team packages its own copy of this pattern: a socket timeout alone restarts on each received byte, so a trickled
request could otherwise hold one of the controller's bounded admission slots indefinitely. Every controller response,
including the standard library's error replies, closes its connection, so no keep-alive reuse needs a lifetime bound.
"""

import io
import socket
import time
from http.server import BaseHTTPRequestHandler

HTTP_HEADER_DEADLINE_SECONDS = 10
HTTP_BODY_DEADLINE_SECONDS = 10


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
    """Bound request headers and body by absolute deadlines.

    The per-operation idle timeout is the handler's `timeout`, applied by `setup` before the reader captures it.
    """

    header_deadline: float = HTTP_HEADER_DEADLINE_SECONDS
    body_deadline: float = HTTP_BODY_DEADLINE_SECONDS

    def setup(self) -> None:
        super().setup()
        self.rfile.close()
        self._reader = _DeadlineReader(self.connection)
        self.rfile = io.BufferedReader(self._reader)

    def handle_one_request(self) -> None:
        self._reader.deadline = time.monotonic() + self.header_deadline
        super().handle_one_request()

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        self._reader.deadline = time.monotonic() + self.body_deadline
        return True
