"""Absolute request deadlines on the real loopback Team controller listener."""

import http.client
import select
import socket
import threading
import time
import unittest
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from local.http import audit as http_audit
from local.http import deadline, server

TOKEN = "a" * 64


def _drip(address: tuple[str, int], head: bytes, drips: list[bytes]) -> bytes:
    """Send `head`, then one chunk every 40 ms, and return the whole reply, or b"" once the server cut it."""
    with socket.create_connection(address, timeout=2) as client:
        try:
            client.sendall(head)
            for chunk in drips:
                time.sleep(0.04)
                client.sendall(chunk)
            return b"".join(iter(lambda: client.recv(4096), b""))
        except BrokenPipeError, ConnectionResetError:
            return b""


class AbsoluteDeadlineTests(unittest.TestCase):
    """The idle socket timeout restarts on each byte; the header and body deadlines must not."""

    def setUp(self) -> None:
        audit = mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32)
        audit.start()
        self.addCleanup(audit.stop)

    def serve(self, *, slots: int = 16, **deadlines: float) -> server.BoundedServer:
        handler = type("DeadlineHandler", (server.Handler,), deadlines)
        bounded = server.BoundedServer(("127.0.0.1", 0), handler, SimpleNamespace(), TOKEN)
        bounded._slots = threading.BoundedSemaphore(slots)
        thread = threading.Thread(target=bounded.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()

        def stop() -> None:
            bounded.shutdown()
            bounded.server_close()
            thread.join()

        self.addCleanup(stop)
        return bounded

    def test_trickled_headers_are_cut_at_the_header_deadline_and_free_the_slot(self) -> None:
        bounded = self.serve(slots=1, header_deadline=0.2)
        head = b"GET /v1/teams HTTP/1.1\r\nHost: team\r\n"
        drips = [b"X-Drip: 1\r\n"] * 22 + [b"\r\n"]
        replies: list[bytes] = []
        dripping = threading.Thread(target=lambda: replies.append(_drip(bounded.server_address, head, drips)))
        dripping.start()
        try:
            time.sleep(0.5)
            # The drip is still running, yet its expired deadline already returned the sole admission slot.
            connection = http.client.HTTPConnection(*bounded.server_address, timeout=2)
            try:
                connection.request("GET", "/v1/teams")
                self.assertEqual(connection.getresponse().status, HTTPStatus.UNAUTHORIZED)
            finally:
                connection.close()
        finally:
            dripping.join()
        self.assertEqual(replies, [b""])

    def test_a_trickled_body_is_refused_at_the_body_deadline(self) -> None:
        bounded = self.serve(body_deadline=0.2)
        body = b'{"team_name":"Research team"}'
        head = (
            b"POST /v1/teams/research/create HTTP/1.1\r\nHost: team\r\nContent-Type: application/json\r\n"
            + f"Authorization: Bearer {TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        )
        with socket.create_connection(bounded.server_address, timeout=2) as client:
            client.sendall(head)
            sent = 0
            # Trickle one byte every 40 ms until the controller answers; the whole body would take over a second.
            while sent < len(body) and not select.select([client], [], [], 0.04)[0]:
                client.sendall(body[sent : sent + 1])
                sent += 1
            reply = b"".join(iter(lambda: client.recv(4096), b""))
        self.assertLess(sent, len(body))
        self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply)
        self.assertIn(b"invalid-json", reply)

    def test_an_expired_deadline_refuses_the_read_without_waiting(self) -> None:
        server_side, client_side = socket.socketpair()
        self.addCleanup(server_side.close)
        self.addCleanup(client_side.close)
        server_side.settimeout(5)
        reader = deadline._DeadlineReader(server_side)
        reader.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(TimeoutError, "deadline expired"):
            reader.readinto(bytearray(1))
        self.assertEqual(server_side.gettimeout(), 5)


if __name__ == "__main__":
    unittest.main()
