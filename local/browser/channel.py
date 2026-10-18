"""The Browser's `docker exec` channels: the Cua Driver's MCP stdio and the bounded one-shot programs (ADR-0117 §3.5).

Docker multiplexes an exec's stdout and stderr in frames of an 8-byte header. Team reads only stdout, bounded, and
discards stderr up to the same bound; every read waits no longer than its caller's absolute deadline.
"""

from __future__ import annotations

import select
import socket
import struct
import time
from collections.abc import Callable
from contextlib import suppress

HEADER = struct.Struct(">BxxxL")
CHUNK = 64 * 1024
MAX_STDERR = 64 * 1024


class ChannelError(OSError):
    """The exec stream ended, overflowed, broke its framing, or outlived its deadline."""


class ExecStream:
    """Demultiplex one exec socket's stdout, bounded, within absolute deadlines."""

    def __init__(self, raw: socket.socket, clock: Callable[[], float] = time.monotonic) -> None:
        self._raw = raw
        self._clock = clock
        self._pending = bytearray()
        self._stdout = bytearray()
        self._stderr = 0
        self._closed = False
        raw.setblocking(False)

    def send(self, data: bytes) -> None:
        """Write all of `data`, waiting at most a few seconds for room."""
        view = memoryview(data)
        deadline = self._clock() + 5
        while view:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ChannelError("the Browser channel did not accept its input")
            _, writable, _ = select.select([], [self._raw], [], remaining)
            if writable:
                try:
                    view = view[self._raw.send(view) :]
                except BlockingIOError:
                    continue

    def close_input(self) -> None:
        with suppress(OSError):
            self._raw.shutdown(socket.SHUT_WR)

    def receive_line(self, deadline: float, limit: int) -> bytes:
        """The next stdout line without its newline, of at most `limit` bytes."""
        while (end := self._stdout.find(b"\n")) < 0:
            if len(self._stdout) > limit:
                raise ChannelError("the Browser channel sent an oversized line")
            if self._closed:
                raise ChannelError("the Browser channel closed")
            self._fill(deadline, limit)
        if end > limit:
            raise ChannelError("the Browser channel sent an oversized line")
        line = bytes(self._stdout[:end])
        del self._stdout[: end + 1]
        return line

    def receive_exact(self, size: int, deadline: float) -> bytes:
        """Exactly `size` more stdout bytes."""
        while len(self._stdout) < size:
            if self._closed:
                raise ChannelError("the Browser channel closed")
            self._fill(deadline, size)
        data = bytes(self._stdout[:size])
        del self._stdout[:size]
        return data

    def receive_end(self, deadline: float) -> None:
        """Wait for the end of the stream; any further stdout is refused."""
        while not self._closed:
            self._fill(deadline, 0)
        if self._stdout:
            raise ChannelError("the Browser channel sent more than it framed")

    def _fill(self, deadline: float, limit: int) -> None:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise ChannelError("the Browser channel did not answer in time")
        readable, _, _ = select.select([self._raw], [], [], remaining)
        if not readable:
            raise ChannelError("the Browser channel did not answer in time")
        try:
            chunk = self._raw.recv(CHUNK)
        except BlockingIOError:
            return
        if not chunk:
            if self._pending:
                raise ChannelError("the Browser channel ended inside a frame")
            self._closed = True
            return
        self._pending.extend(chunk)
        self._frames(limit)

    def _frames(self, limit: int) -> None:
        while len(self._pending) >= HEADER.size:
            stream, length = HEADER.unpack_from(self._pending)
            if stream not in {1, 2}:
                raise ChannelError("the Browser channel broke its framing")
            end = HEADER.size + length
            if len(self._pending) < end:
                return
            payload = self._pending[HEADER.size : end]
            del self._pending[:end]
            if stream == 2:
                self._stderr += len(payload)
                if self._stderr > MAX_STDERR:
                    raise ChannelError("the Browser channel wrote too much to stderr")
                continue
            self._stdout.extend(payload)
            if len(self._stdout) > max(limit, 0) + CHUNK:
                raise ChannelError("the Browser channel sent more than was asked")
