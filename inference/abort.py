"""Stop's handle on the Brain request a chat turn is waiting for (ADR-0079)."""

import contextvars
import http.client
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from inference.errors import BrainRuntimeError


class RequestAbort:
    """Stop's handle on the Brain request a Local chat turn is waiting for (ADR-0079).

    ``abort`` shuts down the attached connection's socket, which wakes the blocked read and makes Brain see the
    disconnect and cancel the turn's provider call. A request attached after the abort fails before connecting, and
    one still connecting fails as soon as its bounded connect returns. The connected socket is pinned, because a
    response that closes the connection detaches it from the connection while its body is still being read.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._aborted = False
        self._connection: http.client.HTTPConnection | None = None
        self._socket: socket.socket | None = None

    def abort(self) -> None:
        # Shutting down under the lock keeps the request from detaching and closing the connection meanwhile.
        with self._lock:
            self._aborted = True
            sock = self._socket or getattr(self._connection, "sock", None)
            if sock is not None:
                with suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    def pin(self, sock: socket.socket) -> None:
        """Keep the connected socket abortable for the whole response, even after the connection releases it."""
        with self._lock:
            self._socket = sock
        self.check()

    def attach(self, connection: http.client.HTTPConnection) -> None:
        with self._lock:
            self._connection = connection
        self.check()

    def check(self) -> None:
        with self._lock:
            if self._aborted:
                raise BrainRuntimeError("Brain runtime request was stopped")

    def detach(self) -> None:
        with self._lock:
            self._connection = None
            self._socket = None


_ABORT: contextvars.ContextVar[RequestAbort | None] = contextvars.ContextVar("brain_request_abort", default=None)


def current() -> RequestAbort | None:
    """The abort handle of the Brain requests this thread makes, if any."""
    return _ABORT.get()


@contextmanager
def abortable(handle: RequestAbort) -> Iterator[None]:
    """Let ``handle`` abort every Brain request this thread makes inside the block."""
    token = _ABORT.set(handle)
    try:
        yield
    finally:
        _ABORT.reset(token)
