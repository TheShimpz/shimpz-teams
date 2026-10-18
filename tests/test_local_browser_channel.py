"""The Browser's exec channel: Docker's multiplexed frames, bounded stdout, and deadlines."""

import socket
import struct
import threading
import unittest
from unittest import mock

from local.browser import channel


def _frame(stream: int, payload: bytes) -> bytes:
    return struct.pack(">BxxxL", stream, len(payload)) + payload


class ExecStreamTests(unittest.TestCase):
    def setUp(self):
        self.team, self.docker = socket.socketpair()
        self.addCleanup(self.team.close)
        self.addCleanup(self.docker.close)
        self.now = [0.0]
        self.stream = channel.ExecStream(self.team, clock=lambda: self.now[0])

    def feed(self, *chunks: bytes, close: bool = False):
        for chunk in chunks:
            self.docker.sendall(chunk)
        if close:
            self.docker.shutdown(socket.SHUT_WR)

    def test_lines_and_exact_bytes_come_from_stdout_frames_only(self):
        self.feed(
            _frame(2, b"a warning\n"),
            _frame(1, b'{"id":1}\n{"id"'),
            _frame(1, b":2}\n12\nabc"),
            _frame(1, b"defghijkl"),
            close=True,
        )
        self.assertEqual(self.stream.receive_line(10, 64), b'{"id":1}')
        self.assertEqual(self.stream.receive_line(10, 64), b'{"id":2}')
        self.assertEqual(self.stream.receive_line(10, 64), b"12")
        self.assertEqual(self.stream.receive_exact(12, 10), b"abcdefghijkl")
        self.stream.receive_end(10)

    def test_a_frame_split_across_reads_is_reassembled(self):
        frame = _frame(1, b"split line\n")
        self.feed(frame[:3])
        threading.Timer(0.05, lambda: self.feed(frame[3:], close=True)).start()
        self.assertEqual(self.stream.receive_line(10, 64), b"split line")

    def test_input_is_written_whole_and_can_be_closed(self):
        received = bytearray()

        def drain():
            while len(received) < 300_000:
                received.extend(self.docker.recv(65536))

        reader = threading.Thread(target=drain)
        reader.start()
        self.stream.send(b"x" * 300_000)
        reader.join(10)
        self.assertEqual(len(received), 300_000)
        self.stream.close_input()
        self.assertEqual(self.docker.recv(10), b"")
        self.stream.close_input()

    def test_a_send_waits_out_a_busy_buffer(self):
        sent = []
        attempts = iter([BlockingIOError(), 3])

        def send(view):
            outcome = next(attempts)
            if isinstance(outcome, BaseException):
                raise outcome
            sent.append(bytes(view))
            return outcome

        waits = iter([([], [], []), ([], ["raw"], []), ([], ["raw"], [])])
        stream = channel.ExecStream(self.team, clock=lambda: 0.0)
        stream._raw = type("Raw", (), {"send": staticmethod(send)})()
        with mock.patch.object(channel.select, "select", side_effect=lambda *_: next(waits)):
            stream.send(b"abc")
        self.assertEqual(sent, [b"abc"])

    def test_a_full_input_buffer_outlives_no_deadline(self):
        ticks = iter([0.0, 0.0])
        stream = channel.ExecStream(self.team, clock=lambda: next(ticks, 100.0))
        with self.assertRaises(channel.ChannelError):
            stream.send(b"x" * 10_000_000)

    def test_bounds_framing_and_endings_are_enforced(self):
        cases = [
            ((_frame(1, b"x" * 100),), lambda stream: stream.receive_line(10, 50), "oversized"),
            ((_frame(1, b"x" * 100 + b"\n"),), lambda stream: stream.receive_line(10, 50), "oversized"),
            ((_frame(3, b"x"),), lambda stream: stream.receive_line(10, 50), "framing"),
            ((_frame(2, b"e" * (channel.MAX_STDERR + 1)),), lambda stream: stream.receive_line(10, 50), "stderr"),
            (
                (_frame(1, b"x" * (channel.CHUNK + 60)),),
                lambda stream: stream.receive_exact(10, 10),
                "more than was asked",
            ),
            ((_frame(1, b"partial"),), lambda stream: stream.receive_line(10, 50), "closed"),
            ((_frame(1, b"short"),), lambda stream: stream.receive_exact(10, 10), "closed"),
            ((_frame(1, b"tail"),), lambda stream: stream.receive_end(10), "more than it framed"),
            ((_frame(1, b"cut")[:-1],), lambda stream: stream.receive_end(10), "inside a frame"),
        ]
        for chunks, read, message in cases:
            with self.subTest(message=message):
                team, docker = socket.socketpair()
                self.addCleanup(team.close)
                self.addCleanup(docker.close)
                for chunk in chunks:
                    docker.sendall(chunk)
                docker.shutdown(socket.SHUT_WR)
                with self.assertRaisesRegex(channel.ChannelError, message):
                    read(channel.ExecStream(team, clock=lambda: 0.0))

    def test_a_silent_or_late_stream_fails_at_its_deadline(self):
        with self.assertRaisesRegex(channel.ChannelError, "in time"):
            self.stream.receive_line(0, 10)
        clock = iter([0.0, 0.01])
        stream = channel.ExecStream(self.team, clock=lambda: next(clock, 0.01))
        with self.assertRaisesRegex(channel.ChannelError, "in time"):
            stream.receive_line(0.01, 10)

    def test_a_spurious_wakeup_is_read_again(self):
        stream = channel.ExecStream(self.team, clock=lambda: 0.0)
        original = self.team.recv
        calls = []

        def recv(size):
            calls.append(size)
            if len(calls) == 1:
                raise BlockingIOError
            return original(size)

        self.feed(_frame(1, b"ok\n"), close=True)
        stream._raw = type("Raw", (), {"recv": staticmethod(recv), "fileno": self.team.fileno})()
        self.assertEqual(stream.receive_line(10, 10), b"ok")


if __name__ == "__main__":
    unittest.main()
