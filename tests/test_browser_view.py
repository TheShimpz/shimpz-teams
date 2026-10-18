"""The screen view's ticks and the PNG Team builds from the display's raw pixels (ADR-0117, M2 Watch)."""

import io
import os
import tracemalloc
import unittest
from unittest import mock

from PIL import Image

from browser import view
from protocol.http.v1 import browser_view as view_protocol


class Ticks:
    """A view program's stdout as the channel answers it: lines and exact reads from one buffer."""

    def __init__(self, data: bytes) -> None:
        self.data = bytearray(data)

    def receive_line(self, deadline: float, limit: int) -> bytes:
        end = self.data.find(b"\n")
        if end < 0 or end > limit:
            raise OSError("the Browser channel closed")
        line = bytes(self.data[:end])
        del self.data[: end + 1]
        return line

    def receive_exact(self, size: int, deadline: float) -> bytes:
        if len(self.data) < size:
            raise OSError("the Browser channel closed")
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk


def pixels(red: int, green: int, blue: int) -> bytes:
    return bytes((red, green, blue)) * (view.WIDTH * view.HEIGHT)


class ViewTickTests(unittest.TestCase):
    def test_a_tick_is_unchanged_or_one_exact_frame(self):
        frame = pixels(1, 2, 3)
        ticks = Ticks(b"same\n" + b"rgb 1280 800\n" + frame + b"same\n")
        self.assertIsNone(view.next_tick(ticks, 0))
        self.assertEqual(view.next_tick(ticks, 0), frame)
        self.assertIsNone(view.next_tick(ticks, 0))

    def test_anything_else_ends_the_view(self):
        for data in (b"rgb 1920 1080\n", b"SAME\n", b"same \n", b"\n"):
            with self.assertRaises(view.ViewError, msg=data):
                view.next_tick(Ticks(data + pixels(0, 0, 0)), 0)
        with self.assertRaises(OSError):
            view.next_tick(Ticks(b"rgb 1280 800\n" + pixels(0, 0, 0)[:-1]), 0)


class PngTests(unittest.TestCase):
    def test_team_builds_a_valid_png_of_the_exact_pixels(self):
        frame = bytearray(pixels(250, 128, 3))
        frame[:3] = b"\x01\x02\x03"
        encoded = view.png(bytes(frame))
        self.assertTrue(encoded.startswith(view_protocol.PNG_SIGNATURE))
        self.assertIsNotNone(view_protocol.canonical_frame({"frame": 1, "length": len(encoded)}))
        with Image.open(io.BytesIO(encoded)) as image:
            image.load()
            self.assertEqual((image.format, image.mode, image.size), ("PNG", "RGB", (1280, 800)))
            self.assertEqual(image.getpixel((0, 0)), (1, 2, 3))
            self.assertEqual(image.getpixel((1279, 799)), (250, 128, 3))

    def test_only_a_whole_frame_is_encoded(self):
        for size in (0, view.FRAME_BYTES - 1, view.FRAME_BYTES + 1):
            with self.assertRaises(view.ViewError):
                view.png(b"\0" * size)

    def test_a_frame_over_its_bound_is_never_sent(self):
        oversized = b"\0" * view_protocol.MAX_FRAME_BYTES
        with mock.patch.object(view.zlib, "compress", return_value=oversized), self.assertRaises(view.ViewError):
            view.png(pixels(0, 0, 0))

    def test_the_worst_frame_stays_within_its_bounds(self):
        noise = os.urandom(view.FRAME_BYTES)
        tracemalloc.start()
        try:
            encoded = view.png(noise)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLessEqual(len(encoded), view_protocol.MAX_FRAME_BYTES)
        # The rows, the compressed data, and the PNG: a small part of Team's 256 MiB.
        self.assertLess(peak, 16 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
