"""One viewer's WebSocket carried to the Browser's display: the exact upgrade, bounded copying, and its endings."""

import socket
import struct
import threading
import time
import unittest
from unittest import mock

from local.browser import tunnel
from local.browser.channel import ChannelError, ExecStream
from protocol.http.v1 import browser_view as view_protocol

KEY = "AAAAAAAAAAAAAAAAAAAAAA=="


def framed(data: bytes, stream: int = 1) -> bytes:
    """Bytes as a Docker exec stream frames them."""
    return struct.pack(">BxxxL", stream, len(data)) + data


def admitted_answer(key: str = KEY, extra: str = "") -> bytes:
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {view_protocol.accept(key)}",
        "Sec-WebSocket-Protocol: binary",
    ]
    if extra:
        lines.append(extra)
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class Display:
    """The relay's far side: a socket pair where the test plays the display, framing what it writes as Docker does."""

    def __init__(self) -> None:
        self.team_side, self.display_side = socket.socketpair()
        self.stream = ExecStream(self.team_side)

    def write(self, data: bytes, stream: int = 1) -> None:
        self.display_side.sendall(framed(data, stream))

    def read_request(self) -> bytes:
        self.display_side.settimeout(5)
        data = b""
        while b"\r\n\r\n" not in data:
            data += self.display_side.recv(4096)
        return data

    def close(self) -> None:
        for side in (self.team_side, self.display_side):
            side.close()


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.display = Display()
        self.viewer, self.team_viewer = socket.socketpair()
        self.addCleanup(self.display.close)
        self.addCleanup(self.viewer.close)
        self.addCleanup(self.team_viewer.close)
        self.tunnel = tunnel.Tunnel(self.team_viewer, self.display.stream)

    def upgrade_with(self, answer: bytes) -> tuple[bytes, bytes] | None:
        result = {}
        worker = threading.Thread(target=lambda: result.update(first=self.tunnel.upgrade(KEY)), daemon=True)
        worker.start()
        request = self.display.read_request()
        self.assertEqual(request, tunnel.upgrade_request(KEY))
        self.display.write(answer)
        worker.join(5)
        return result.get("first")

    def test_the_display_receives_one_canonical_request_and_the_viewer_its_exact_101(self):
        first = self.upgrade_with(admitted_answer() + b"RFB 003.008\n")
        self.assertEqual(first, (tunnel.answer(KEY), b"RFB 003.008\n"))
        self.assertIn(b"Origin: http://127.0.0.1:6901\r\n", tunnel.upgrade_request(KEY))

    def test_anything_but_the_exact_101_for_the_key_is_refused(self):
        refused = (
            admitted_answer().replace(b"101 Switching", b"200 OK"),
            admitted_answer(key="BBBBBBBBBBBBBBBBBBBBBA=="),
            admitted_answer().replace(b"binary", b"base64"),
            admitted_answer(extra="Sec-WebSocket-Extensions: permessage-deflate"),
            admitted_answer(extra="Upgrade: websocket"),
            admitted_answer(extra="no colon here"),
            admitted_answer().replace(b"Upgrade: websocket", b"Upgrade: h2c"),
            admitted_answer().replace(b"Connection: Upgrade", b"Connection: keep-alive"),
        )
        for answer in refused:
            with self.subTest(answer=answer):
                self.assertFalse(tunnel.admitted(answer.decode().split("\r\n")[:-2], KEY))
        self.assertFalse(tunnel.admitted([], KEY))
        self.assertTrue(tunnel.admitted(admitted_answer().decode().split("\r\n")[:-2], KEY))

    def test_an_upgrade_the_display_never_answers_or_overflows_fails(self):
        with mock.patch.object(tunnel, "UPGRADE_SECONDS", 0.2), self.assertRaises(tunnel.TunnelError):
            self.tunnel.upgrade(KEY)
        oversized = b"HTTP/1.1 101 Switching Protocols\r\n" + b"X-Filler: " + b"a" * 5000 + b"\r\n\r\n"
        display = Display()
        self.addCleanup(display.close)
        other = tunnel.Tunnel(self.team_viewer, display.stream)
        display.write(oversized)
        with self.assertRaises(tunnel.TunnelError):
            other.upgrade(KEY)
        refused = Display()
        self.addCleanup(refused.close)
        refused.write(admitted_answer().replace(b"101 Switching", b"404 Not"))
        with self.assertRaises(tunnel.TunnelError):
            tunnel.Tunnel(self.team_viewer, refused.stream).upgrade(KEY)

    def splice_in_background(self, ended=lambda: False):
        worker = threading.Thread(target=lambda: self.tunnel.splice(ended), daemon=True)
        worker.start()
        return worker

    def test_bytes_cross_both_ways_until_a_side_ends(self):
        worker = self.splice_in_background()
        self.viewer.sendall(b"to the display")
        self.display.display_side.settimeout(5)
        self.assertEqual(self.display.display_side.recv(100), b"to the display")
        self.display.write(b"to the viewer")
        self.display.write(b"noise on stderr", stream=2)
        self.viewer.settimeout(5)
        self.assertEqual(self.viewer.recv(100), b"to the viewer")
        self.viewer.shutdown(socket.SHUT_WR)
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.tunnel.carried, {"to_viewer": 13, "to_display": 14})

    def test_a_quiet_display_never_strands_the_end_of_an_update(self):
        # One update larger than the viewer's room: the rest moves on once the viewer takes the first part, although
        # the display sends nothing more.
        with mock.patch.object(tunnel, "BUFFER_BYTES", 10):
            worker = self.splice_in_background()
            self.display.write(b"0123456789abcde")
            self.viewer.settimeout(5)
            received = b""
            while len(received) < 15:
                received += self.viewer.recv(100)
            self.assertEqual(received, b"0123456789abcde")
            self.viewer.shutdown(socket.SHUT_WR)
            worker.join(5)
        self.assertEqual(self.tunnel.carried["to_viewer"], 15)

    def test_the_display_ending_ends_the_stream(self):
        worker = self.splice_in_background()
        self.display.display_side.shutdown(socket.SHUT_WR)
        worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_an_interrupt_or_the_task_ending_stops_the_stream_at_once(self):
        worker = self.splice_in_background()
        started = time.monotonic()
        self.tunnel.interrupt()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 2)
        other = Display()
        self.addCleanup(other.close)
        viewer, team_viewer = socket.socketpair()
        self.addCleanup(viewer.close)
        self.addCleanup(team_viewer.close)
        ending = tunnel.Tunnel(team_viewer, other.stream)
        ended = threading.Event()
        worker = threading.Thread(target=lambda: ending.splice(ended.is_set), daemon=True)
        worker.start()
        ended.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        with mock.patch.object(tunnel, "LIFE_SECONDS", 0):
            tunnel.Tunnel(team_viewer, other.stream).splice(lambda: False)

    def test_a_viewer_that_stops_reading_ends_the_stream_and_buffers_stay_bounded(self):
        self.viewer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        with mock.patch.object(tunnel, "WRITE_SECONDS", 0.3), mock.patch.object(tunnel, "BUFFER_BYTES", 64 * 1024):
            failure = {}

            def run():
                try:
                    self.tunnel.splice(lambda: False)
                except tunnel.TunnelError as exc:
                    failure["error"] = exc

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            chunk = b"x" * 60_000
            self.display.display_side.setblocking(False)
            deadline = time.monotonic() + 5
            while worker.is_alive() and time.monotonic() < deadline:
                try:
                    self.display.display_side.send(framed(chunk))
                except BlockingIOError:
                    time.sleep(0.05)
            worker.join(5)
        self.assertIsInstance(failure.get("error"), tunnel.TunnelError)

    def test_a_viewer_connection_that_fails_ends_the_stream(self):
        worker_failure = {}

        def run():
            try:
                self.tunnel.splice(lambda: False)
            except tunnel.TunnelError as exc:
                worker_failure["error"] = exc

        class Failing:
            """The viewer's socket, whose every send fails as a reset connection does."""

            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            def send(self, _data):
                raise ConnectionResetError("gone")

        self.tunnel = tunnel.Tunnel(Failing(self.team_viewer), self.display.stream)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.display.write(b"to the viewer")
        worker.join(5)
        self.assertIsInstance(worker_failure.get("error"), tunnel.TunnelError)


class EdgeTests(unittest.TestCase):
    """The non-blocking edges of the channel and the tunnel, each in isolation."""

    def test_the_channel_pumps_only_what_is_there_and_ends_cleanly_or_fails_inside_a_frame(self):
        display = Display()
        self.addCleanup(display.close)
        self.assertTrue(display.stream.pump(1024))
        self.assertEqual(display.stream.buffered(), 0)
        display.display_side.sendall(framed(b"abc")[:5])
        display.display_side.shutdown(socket.SHUT_WR)
        time.sleep(0.05)
        self.assertTrue(display.stream.pump(1024))
        with self.assertRaises(ChannelError):
            display.stream.pump(1024)
        clean = Display()
        self.addCleanup(clean.close)
        clean.display_side.shutdown(socket.SHUT_WR)
        self.assertFalse(clean.stream.pump(1024))
        self.assertFalse(clean.stream.pump(1024))

    def test_each_read_takes_at_most_the_room_left(self):
        display = Display()
        self.addCleanup(display.close)
        display.write(b"a" * 4)
        display.write(b"b" * 100)
        time.sleep(0.05)
        # Twenty bytes: the first frame whole and the second one's header, none of its payload.
        self.assertTrue(display.stream.pump(20))
        self.assertEqual(display.stream.take(1024), b"aaaa")
        viewer, team_viewer = socket.socketpair()
        self.addCleanup(viewer.close)
        self.addCleanup(team_viewer.close)
        team_viewer.setblocking(False)
        viewer.sendall(b"0123456789")
        time.sleep(0.05)
        self.assertEqual(tunnel.Tunnel(team_viewer, display.stream)._receive(3), b"012")

    def test_a_full_channel_takes_nothing_more_for_now(self):
        display = Display()
        self.addCleanup(display.close)
        taken = 1
        while taken:
            taken = display.stream.send_some(b"x" * 65536)
        self.assertEqual(display.stream.send_some(b"x"), 0)

    def test_a_viewer_with_nothing_to_say_or_a_failed_socket(self):
        viewer, team_viewer = socket.socketpair()
        self.addCleanup(viewer.close)
        self.addCleanup(team_viewer.close)
        display = Display()
        self.addCleanup(display.close)
        team_viewer.setblocking(False)
        quiet = tunnel.Tunnel(team_viewer, display.stream)
        self.assertEqual(quiet._receive(1024), b"")

        class Broken:
            def recv(self, _size):
                raise ConnectionResetError("gone")

            def send(self, _data):
                raise BlockingIOError

        broken = tunnel.Tunnel(Broken(), display.stream)
        self.assertIsNone(broken._receive(1024))
        self.assertEqual(broken._send(bytearray(b"x")), 0)

    def test_an_upgrade_answer_of_many_lines_over_the_bound_fails(self):
        display = Display()
        self.addCleanup(display.close)
        viewer, team_viewer = socket.socketpair()
        self.addCleanup(viewer.close)
        self.addCleanup(team_viewer.close)
        filler = b"".join(b"X-Filler-%03d: aaaaaaaaaa\r\n" % index for index in range(200))
        lines = b"HTTP/1.1 101 Switching Protocols\r\n" + filler
        display.write(lines + b"\r\n")
        with self.assertRaises(tunnel.TunnelError):
            tunnel.Tunnel(team_viewer, display.stream).upgrade(KEY)


if __name__ == "__main__":
    unittest.main()
