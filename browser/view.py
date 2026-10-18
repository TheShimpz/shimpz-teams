"""The screen view a person watches while a Browser task runs (ADR-0117, M2 Watch).

`browser-host view` sends the display's raw pixels: every tick either `same` or the exact frame header and 1280x800
packed RGB pixels. Team admits only that, and builds every PNG the person's browser decodes itself, so no image bytes
the Browser produced ever reach it. The pixels are page content: never logged, stored, audited, or sent to Brain.
"""

from __future__ import annotations

import struct
import zlib
from typing import Protocol

from protocol.http.v1 import browser_view as view_protocol

WIDTH = 1280
HEIGHT = 800
FRAME_BYTES = WIDTH * HEIGHT * 3
FRAME_HEADER = b"rgb 1280 800"
UNCHANGED = b"same"


class ViewError(RuntimeError):
    """The view program sent something other than an exact tick."""


class Stream(Protocol):
    """The view program's stdout: one bounded line, or exact bytes, each before an absolute deadline."""

    def receive_line(self, deadline: float, limit: int) -> bytes: ...

    def receive_exact(self, size: int, deadline: float) -> bytes: ...


def next_tick(stream: Stream, deadline: float) -> bytes | None:
    """The next tick's pixels, or None when the screen did not change since the last frame."""
    line = stream.receive_line(deadline, FRAME_BYTES)
    if line == UNCHANGED:
        return None
    if line != FRAME_HEADER:
        raise ViewError("the view sent an unknown tick")
    return stream.receive_exact(FRAME_BYTES, deadline)


def png(rgb: bytes) -> bytes:
    """One frame's pixels as a PNG Team builds: 8-bit RGB, no filter, fast compression."""
    if len(rgb) != FRAME_BYTES:
        raise ViewError("a frame has the wrong size")
    stride = WIDTH * 3
    rows = b"".join(b"\0" + rgb[row * stride : (row + 1) * stride] for row in range(HEIGHT))
    header = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 2, 0, 0, 0)
    encoded = b"".join(
        (
            view_protocol.PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(rows, 1)),
            _chunk(b"IEND", b""),
        )
    )
    if len(encoded) > view_protocol.MAX_FRAME_BYTES:
        raise ViewError("a frame exceeds its bound")
    return encoded


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
