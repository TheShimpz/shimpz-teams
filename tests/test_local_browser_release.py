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


# How the scripted display answers unless a test says otherwise.
DISPLAY_ANSWER = {
    "upgrade_status": "101",
    "update": True,
    "version": b"RFB 003.008\n",
    "security": b"\x01\x01",
    "result": b"\x00\x00\x00\x00",
    "first": b"\x00\x00\x00\x00",
    "raw_frames": b"",
    "extra_headers": 0,
    "name_length": 4,
}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.display = Display()
        self.addCleanup(self.display.close)
        self.credentials = Credentials.draw()

    def play(self, **changes: object) -> dict[str, object]:
        """Answer as KasmVNC: the upgrade, the RFB handshake, and one update; record what Team sent."""
        seen: dict[str, object] = {}
        answer_with = {**DISPLAY_ANSWER, **changes}
        upgrade_status, update = answer_with["upgrade_status"], answer_with["update"]
        version, security, result = answer_with["version"], answer_with["security"], answer_with["result"]
        first, raw_frames = answer_with["first"], answer_with["raw_frames"]
        extra_headers, name_length = answer_with["extra_headers"], answer_with["name_length"]

        def serve():
            request = self.display.read_request().decode()
            seen["request"] = request
            key = re.search(r"Sec-WebSocket-Key: (\S+)", request)[1]
            if upgrade_status != "101":
                self.display.write(b"HTTP/1.1 401 Forbidden\r\n\r\n")
                self.display.display_side.shutdown(2)
                return
            init = struct.pack(">HH", 1280, 800) + bytes(16) + struct.pack(">I", name_length) + b"test"
            answer = admitted_answer(key)
            answer = answer[:-2] + b"X-Extra: 1\r\n" * extra_headers + b"\r\n"
            self.display.write(
                answer
                + raw_frames
                + _server_frame(version)
                + _server_frame(security)
                + _server_frame(result)
                + _server_frame(init)
            )
            received = b""
            self.display.display_side.settimeout(5)
            try:
                while len(_client_frames(received)) < 6:
                    chunk = self.display.display_side.recv(4096)
                    if not chunk:
                        return
                    received += chunk
            except OSError:
                return
            seen["frames"] = _client_frames(received)
            if update:
                self.display.write(_server_frame(first))
            else:
                self.display.display_side.shutdown(2)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        self.worker = worker
        return seen

    def release(self, *, proven: bool = True) -> None:
        """Release through the test display, its relay exec proven ended (or not) as `proven` says."""
        self.exits: list[str] = []

        def exited(exec_id: str, _deadline: float) -> bool:
            self.exits.append(exec_id)
            return proven

        release.release(lambda: ("exec-1", self.display.stream), exited, self.credentials.authorization(True))

    def test_team_releases_every_button_as_the_writer_and_presses_nothing(self):
        seen = self.play()
        self.release()
        self.worker.join(5)
        self.assertIn(f"Authorization: {self.credentials.authorization(True)}", seen["request"])
        version, security, shared, encodings, pointer, update = seen["frames"]
        self.assertEqual((version, security, shared), (b"RFB 003.008\n", b"\x01", b"\x01"))
        self.assertEqual(encodings[0], 2)
        self.assertEqual(pointer, struct.pack(">BHHHhh", 5, 0, 0, 0, 0, 0))
        self.assertEqual(update[0], 3)
        self.assertFalse(any(frame[:1] == b"\x04" for frame in seen["frames"]))
        self.assertEqual(self.exits, ["exec-1"])

    def test_a_release_whose_relay_is_not_proven_ended_is_no_release(self):
        self.play()
        with self.assertRaises(release.ReleaseError):
            self.release(proven=False)
        self.worker.join(5)

    def test_a_refused_stream_or_a_display_that_never_answers_is_no_release(self):
        for options in ({"upgrade_status": "401"}, {"update": False}):
            with self.subTest(options=options):
                self.setUp()
                self.play(**options)
                with self.assertRaises(release.ReleaseError):
                    self.release()
                self.worker.join(5)

    def test_a_display_that_speaks_other_than_kasm_does_is_no_release(self):
        long_frame = bytes([0x82, 126]) + struct.pack(">H", 2) + b"\x00\x00"
        huge_frame = bytes([0x82, 127]) + struct.pack(">Q", 1 << 21)
        for options in (
            {"version": b"HTTP/1.1 200\n"},
            {"security": b"\x01\x02"},
            {"result": b"\x00\x00\x00\x01"},
            {"first": b"\x02\x00\x00\x00"},
            {"raw_frames": bytes([0x81, 1]) + b"x"},
            {"raw_frames": huge_frame},
            {"extra_headers": release.MAX_HEADER_LINES},
            {"name_length": release.MAX_NAME_BYTES + 1},
        ):
            with self.subTest(options=list(options)):
                self.setUp()
                self.play(**options)
                with self.assertRaises(release.ReleaseError):
                    self.release()
                self.display.display_side.close()
                self.worker.join(5)
        # A 16-bit length is read as one: two bytes the handshake does not ask for are kept until it does.
        self.setUp()
        self.play(raw_frames=long_frame, version=b"FB 003.008\n")
        with self.assertRaises(release.ReleaseError):
            self.release()


if __name__ == "__main__":
    unittest.main()
