"""Releasing the buttons a person held when their control ends: Team's own read-write stream to the display."""

import re
import struct
import threading
import unittest

from local.browser import release
from local.browser.credentials import Credentials
from tests.test_local_browser_tunnel import Display, admitted_answer


def _server_frame(payload: bytes) -> bytes:
    return bytes([0x82, len(payload)]) + payload if len(payload) < 126 else b""


def _client_frames(data: bytes) -> list[bytes]:
    """Unmask every complete client frame in `data`; a partial last frame is left out."""
    frames, offset = [], 0
    while offset + 2 <= len(data):
        length, start = data[offset + 1] & 0x7F, offset + 2
        if length == 126:
            length, start = struct.unpack(">H", data[start : start + 2])[0], start + 2
        if start + 4 + length > len(data):
            break
        mask, payload = data[start : start + 4], data[start + 4 : start + 4 + length]
        frames.append(bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))
        offset = start + 4 + length
    return frames


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.display = Display()
        self.addCleanup(self.display.close)
        self.credentials = Credentials.draw()

    def play(self, *, upgrade_status: str = "101", update: bool = True) -> dict[str, object]:
        """Answer as KasmVNC: the upgrade, the RFB handshake, and one update; record what Team sent."""
        seen: dict[str, object] = {}

        def serve():
            request = self.display.read_request().decode()
            seen["request"] = request
            key = re.search(r"Sec-WebSocket-Key: (\S+)", request)[1]
            if upgrade_status != "101":
                self.display.write(b"HTTP/1.1 401 Forbidden\r\n\r\n")
                self.display.display_side.shutdown(2)
                return
            init = struct.pack(">HH", 1280, 800) + bytes(16) + struct.pack(">I", 4) + b"test"
            self.display.write(
                admitted_answer(key)
                + _server_frame(b"RFB 003.008\n")
                + _server_frame(b"\x01\x01")
                + _server_frame(b"\x00\x00\x00\x00")
                + _server_frame(init)
            )
            received = b""
            self.display.display_side.settimeout(5)
            while len(_client_frames(received)) < 6:
                received += self.display.display_side.recv(4096)
            seen["frames"] = _client_frames(received)
            if update:
                self.display.write(_server_frame(b"\x00\x00\x00\x00"))
            else:
                self.display.display_side.shutdown(2)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        self.worker = worker
        return seen

    def test_team_releases_every_button_as_the_writer_and_presses_nothing(self):
        seen = self.play()
        release.release(lambda: ("exec-1", self.display.stream), self.credentials.authorization(True))
        self.worker.join(5)
        self.assertIn(f"Authorization: {self.credentials.authorization(True)}", seen["request"])
        version, security, shared, encodings, pointer, update = seen["frames"]
        self.assertEqual((version, security, shared), (b"RFB 003.008\n", b"\x01", b"\x01"))
        self.assertEqual(encodings[0], 2)
        self.assertEqual(pointer, struct.pack(">BHHHhh", 5, 0, 0, 0, 0, 0))
        self.assertEqual(update[0], 3)
        self.assertFalse(any(frame[:1] == b"\x04" for frame in seen["frames"]))

    def test_a_refused_stream_or_a_display_that_never_answers_is_no_release(self):
        for options in ({"upgrade_status": "401"}, {"update": False}):
            with self.subTest(options=options):
                self.setUp()
                self.play(**options)
                with self.assertRaises(release.ReleaseError):
                    release.release(lambda: ("exec-1", self.display.stream), self.credentials.authorization(True))
                self.worker.join(5)


if __name__ == "__main__":
    unittest.main()
