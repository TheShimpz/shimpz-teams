"""Bounded OAuth exchanges for the controller's broker client.

One exchange ends at its total deadline, whatever phase a slow peer stalls it in. The token set is the only OAuth
material the controller keeps, sealed by the Integration store; it never holds an OAuth Client Secret.
"""

import http.client
import math
import socket
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass

# One whole exchange ends by this deadline even when each socket operation stays within its own timeout.
TOTAL_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    access_token: str
    refresh_token: str | None
    scopes: tuple[str, ...]
    expires_in: int
    broker_lease: str | None = None


class ExchangeDeadline:
    """End one OAuth exchange at its total deadline, whatever phase it is in.

    The per-operation timeout alone lets a peer that trickles bytes hold the caller, and the Team lifecycle lock an
    OAuth completion holds, far longer. Every socket the connection creates is tracked through a duplicate, which keeps
    naming the same connection after TLS takes over the original, so the deadline shuts down a CONNECT tunnel, a TLS
    handshake, and the response reads alike. Name resolution and the TCP connect itself come before that socket exists
    and stay bounded only by the resolver and the per-operation timeout. Expiry is also read from the clock, so a timer
    that runs late never lets an overdue response through.
    """

    def __init__(self, connection: http.client.HTTPConnection, seconds: float) -> None:
        self._guard = threading.Lock()
        self._expired = False
        self._seconds = seconds
        self._deadline = math.inf
        self._sockets: list[socket.socket] = []
        create = connection._create_connection

        def tracked(*args: object, **kwargs: object) -> socket.socket:
            created = create(*args, **kwargs)
            self._track(created.dup())
            return created

        connection._create_connection = tracked
        self._timer = threading.Timer(seconds, self.expire)
        self._timer.daemon = True

    def __enter__(self) -> ExchangeDeadline:
        self._deadline = time.monotonic() + self._seconds
        self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._timer.cancel()
        with self._guard:
            sockets, self._sockets = self._sockets, []
        for duplicate in sockets:
            duplicate.close()

    @property
    def expired(self) -> bool:
        with self._guard:
            return self._overdue()

    def _overdue(self) -> bool:
        return self._expired or time.monotonic() >= self._deadline

    def _track(self, duplicate: socket.socket) -> None:
        with self._guard:
            self._sockets.append(duplicate)
            if self._overdue():
                _shutdown(duplicate)

    def expire(self) -> None:
        """End the exchange now: shut down every tracked socket and count the deadline as passed."""
        with self._guard:
            self._expired = True
            for duplicate in self._sockets:
                _shutdown(duplicate)


def _shutdown(duplicate: socket.socket) -> None:
    with suppress(OSError):
        duplicate.shutdown(socket.SHUT_RDWR)


def exchange(
    connection: http.client.HTTPConnection,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    limit: int,
) -> tuple[int, str, bytes]:
    """Send one request and read at most ``limit + 1`` response bytes before the total deadline.

    A response the deadline cut short is refused as a timeout, never returned truncated.
    """
    with ExchangeDeadline(connection, TOTAL_TIMEOUT_SECONDS) as deadline:
        connection.request("POST", path, body=body, headers=dict(headers))
        response = connection.getresponse()
        payload = response.read(limit + 1)
        if deadline.expired:
            raise TimeoutError("OAuth exchange exceeded its total deadline")
    return response.status, response.getheader("Content-Type", ""), payload
