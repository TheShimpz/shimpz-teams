"""One OAuth exchange ends by its total deadline, whatever phase a slow peer stalls it in.

Each scenario points the real broker or provider transport at a loopback peer that keeps every single socket
operation within the per-operation timeout, so only the total deadline can end the exchange.
"""

import http.client
import socket
import threading
import time
import unittest
from collections.abc import Callable
from unittest import mock

from integrations import broker as integration_broker
from integrations import http as integration_http

DEADLINE = 0.3


class SlowPeer:
    """Accept one connection and run ``behaviour`` on it until the client hangs up or the test ends."""

    def __init__(self, behaviour: Callable[[socket.socket, threading.Event], None]) -> None:
        self.stopped = threading.Event()
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, args=(behaviour,), daemon=True)
        self.thread.start()

    def _serve(self, behaviour: Callable[[socket.socket, threading.Event], None]) -> None:
        connection, _address = self.listener.accept()
        with connection:
            connection.recv(65536)
            try:
                behaviour(connection, self.stopped)
            except OSError:
                return

    def close(self) -> None:
        self.stopped.set()
        self.listener.close()
        self.thread.join(5)

    def connection(self, *_args: object, timeout: float, **_kwargs: object) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)


def stall(_connection: socket.socket, stopped: threading.Event) -> None:
    stopped.wait(10)


def trickle(prefix: bytes) -> Callable[[socket.socket, threading.Event], None]:
    def behaviour(connection: socket.socket, stopped: threading.Event) -> None:
        connection.sendall(prefix)
        while not stopped.wait(0.02):
            connection.sendall(b"a")

    return behaviour


class OAuthExchangeDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(integration_http, "TOTAL_TIMEOUT_SECONDS", DEADLINE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _peer(self, behaviour: Callable[[socket.socket, threading.Event], None]) -> SlowPeer:
        peer = SlowPeer(behaviour)
        self.addCleanup(peer.close)
        return peer

    def _broker(self, peer: SlowPeer, *, proxied: bool) -> None:
        transport = (
            integration_broker.FixedBrokerTransport(
                proxy_host="shimpz-account-egress", proxy_capability_file="/run/shimpz-account-egress/token"
            )
            if proxied
            else integration_broker.FixedBrokerTransport()
        )
        with (
            mock.patch.object(integration_broker.http.client, "HTTPSConnection", side_effect=peer.connection),
            mock.patch.object(integration_broker.account_egress, "read_capability", return_value="a" * 64),
        ):
            transport.request(url="https://shimpz.com/api/oauth/cloudflare/claim", headers={}, body=b"{}")

    def _assert_ended_by_the_deadline(self, started: float) -> None:
        self.assertLess(time.monotonic() - started, integration_http.HTTP_TIMEOUT_SECONDS / 2)

    def test_a_stalled_proxy_tunnel_ends_at_the_deadline(self) -> None:
        peer = self._peer(stall)
        started = time.monotonic()
        with self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "unavailable"):
            self._broker(peer, proxied=True)
        self._assert_ended_by_the_deadline(started)

    def test_trickled_broker_headers_end_at_the_deadline(self) -> None:
        peer = self._peer(trickle(b"HTTP/1.1 200 OK\r\nX-Slow: "))
        started = time.monotonic()
        with self.assertRaisesRegex(integration_broker.OAuthBrokerClientError, "unavailable"):
            self._broker(peer, proxied=False)
        self._assert_ended_by_the_deadline(started)

    def test_a_trickled_provider_body_is_refused_not_truncated(self) -> None:
        # A close-delimited body: the read that the deadline cuts short returns the bytes so far, never a response.
        peer = self._peer(trickle(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{"))
        started = time.monotonic()
        with (
            mock.patch.object(integration_http.http.client, "HTTPSConnection", side_effect=peer.connection),
            self.assertRaisesRegex(integration_http.OAuthHTTPError, "unavailable"),
        ):
            integration_http.FixedHTTPSTransport().request(
                method="POST", url="https://provider.test/token", headers={}, body=b"x"
            )
        self._assert_ended_by_the_deadline(started)

    def test_a_deadline_that_passed_before_the_socket_existed_still_ends_it(self) -> None:
        peer = self._peer(stall)
        connection = peer.connection(timeout=integration_http.HTTP_TIMEOUT_SECONDS)
        self.addCleanup(connection.close)
        started = time.monotonic()
        with integration_http.ExchangeDeadline(connection, 0) as deadline:
            while not deadline.expired:
                time.sleep(0.005)
            with self.assertRaises((OSError, http.client.HTTPException)):
                connection.request("POST", "/", body=b"")
                connection.getresponse()
        self._assert_ended_by_the_deadline(started)

    def test_an_ended_deadline_releases_its_sockets_and_touches_no_later_request(self) -> None:
        def respond(connection: socket.socket, _stopped: threading.Event) -> None:
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}")

        peer = self._peer(respond)
        connection = peer.connection(timeout=integration_http.HTTP_TIMEOUT_SECONDS)
        self.addCleanup(connection.close)
        deadline = integration_http.ExchangeDeadline(connection, 60)
        with deadline:
            connection.request("POST", "/", body=b"")
            response = connection.getresponse()
            self.assertEqual(response.read(), b"{}")
        # A timer that fires after the exchange ended finds nothing to shut down.
        deadline._expire()
        self.assertTrue(deadline.expired)
        self.assertEqual(deadline._sockets, [])
        self.assertEqual(connection.sock.getpeername()[1], peer.port)

    def test_an_overdue_response_is_refused_even_when_the_timer_runs_late(self) -> None:
        class LateTimer:
            # The deadline's timer never gets to run, as under a stalled scheduler.
            daemon = False

            def __init__(self, *_args: object) -> None:
                pass

            def start(self) -> None:
                pass

            def cancel(self) -> None:
                pass

        def respond_late(connection: socket.socket, stopped: threading.Event) -> None:
            stopped.wait(DEADLINE * 2)
            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}")

        peer = self._peer(respond_late)
        with (
            mock.patch.object(integration_http.threading, "Timer", LateTimer),
            mock.patch.object(integration_http.http.client, "HTTPSConnection", side_effect=peer.connection),
            self.assertRaisesRegex(integration_http.OAuthHTTPError, "unavailable"),
        ):
            integration_http.FixedHTTPSTransport().request(
                method="POST", url="https://provider.test/token", headers={}, body=b"x"
            )


if __name__ == "__main__":
    unittest.main()
