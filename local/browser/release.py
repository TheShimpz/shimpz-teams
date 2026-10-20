"""Releasing every key and button a person held when their control of a Browser ends (ADR-0117, Sign-ins item 3).

A person's control stream can end with a pointer button still pressed in the display: KasmVNC releases a closed
stream's keys itself, but keeps its buttons held (measured on the shipped image: a button pressed by a stream that
then closed stayed down). Before anything else acts on the Browser, Team opens its own read-write stream through the
relay, completes the RFB handshake, sends a pointer event with no button pressed, and waits for one screen update,
which proves the display handled it, then closes the stream and proves its relay exec ended. It never presses
anything, and it reads only what fits its bounds: the display is never trusted to size Team's memory.
"""

from __future__ import annotations

import base64
import os
import struct
import time
from collections.abc import Callable

from local.browser.channel import ChannelError, ExecStream
from local.browser.tunnel import MAX_UPGRADE_BYTES, admitted, upgrade_request

RELEASE_SECONDS = 5.0
# The deadline to prove the release's own relay exec ended, once its stream closed.
EXIT_SECONDS = 2.0
# The upgrade answer's lines, and the desktop name the display announces, each read only within these bounds.
MAX_HEADER_LINES = 32
MAX_NAME_BYTES = 4096
# KasmVNC's pointer event: type 5, a 16-bit button mask, the position, and two scroll deltas.
_POINTER = struct.Struct(">BHHHhh")
# A full update request of one pixel: its answer comes only after the display handled everything sent before it.
_UPDATE_REQUEST = struct.Struct(">BBHHHH").pack(3, 0, 0, 0, 1, 1)
_RAW_ENCODINGS = struct.pack(">BxHi", 2, 1, 0)


class ReleaseError(RuntimeError):
    """The held input could not be proven released."""


def _frame(payload: bytes) -> bytes:
    """One masked binary WebSocket frame, as a client sends it."""
    mask = os.urandom(4)
    length = len(payload)
    header = bytes([0x82, 0x80 | length]) if length < 126 else bytes([0x82, 0x80 | 126]) + struct.pack(">H", length)
    return header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


class _Rfb:
    """The server side of one RFB stream, read through its WebSocket frames."""

    def __init__(self, display: ExecStream, deadline: float, early: bytes) -> None:
        self._display = display
        self._deadline = deadline
        self._buffer = bytearray()
        self._raw = bytearray(early)

    def _wire(self, size: int) -> bytes:
        while len(self._raw) < size:
            self._raw.extend(self._display.receive_exact(1, self._deadline))
        data = bytes(self._raw[:size])
        del self._raw[:size]
        return data

    def read(self, size: int) -> bytes:
        while len(self._buffer) < size:
            first, second = self._wire(2)
            if first & 0x0F not in {0x0, 0x2} or second & 0x80:
                raise ReleaseError("the display sent an unexpected frame")
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._wire(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._wire(8))[0]
            if length > 1 << 20:
                raise ReleaseError("the display sent an oversized frame")
            self._buffer.extend(self._wire(length))
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def send(self, payload: bytes) -> None:
        self._display.send(_frame(payload), self._deadline)


def release(
    open_relay: Callable[[], tuple[str, ExecStream]],
    exited: Callable[[str, float], bool],
    authorization: str,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Release every modifier and pointer button in the display, as the read-write user; raise if it was not done."""
    exec_id, display = open_relay()
    deadline = clock() + RELEASE_SECONDS
    try:
        _release(display, authorization, deadline)
    except ChannelError as exc:
        raise ReleaseError("Team's own stream to the display failed") from exc
    finally:
        display.close_input()
        display.close()
        if not exited(exec_id, clock() + EXIT_SECONDS):
            raise ReleaseError("Team's own relay to the display was not proven ended")


def _release(display: ExecStream, authorization: str, deadline: float) -> None:
    key = base64.b64encode(os.urandom(16)).decode()
    display.send(upgrade_request(key, authorization), deadline)
    head: list[str] = []
    received = 0
    while line := display.receive_line(deadline, MAX_UPGRADE_BYTES).decode("latin-1").removesuffix("\r"):
        head.append(line)
        received += len(line) + 2
        if len(head) > MAX_HEADER_LINES or received > MAX_UPGRADE_BYTES:
            raise ReleaseError("the display's upgrade answer is larger than allowed")
    if not admitted(head, key):
        raise ReleaseError("the display refused Team's own stream")
    rfb = _Rfb(display, deadline, display.take(display.buffered()))
    if not rfb.read(12).startswith(b"RFB 003."):
        raise ReleaseError("the display did not speak RFB")
    rfb.send(b"RFB 003.008\n")
    types = rfb.read(rfb.read(1)[0])
    if 1 not in types:
        raise ReleaseError("the display offered no security type Team's stream uses")
    rfb.send(b"\x01")
    if rfb.read(4) != b"\x00\x00\x00\x00":
        raise ReleaseError("the display refused Team's stream")
    rfb.send(b"\x01")
    rfb.read(20)
    name_length = struct.unpack(">I", rfb.read(4))[0]
    if name_length > MAX_NAME_BYTES:
        raise ReleaseError("the display's name is longer than allowed")
    rfb.read(name_length)
    rfb.send(_RAW_ENCODINGS)
    rfb.send(_POINTER.pack(5, 0, 0, 0, 0, 0))
    rfb.send(_UPDATE_REQUEST)
    # Server messages before the update (a bell, a colour map, cut text) are not expected from this display.
    if rfb.read(1) != b"\x00":
        raise ReleaseError("the display did not answer with a screen update")
