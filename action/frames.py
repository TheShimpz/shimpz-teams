"""Docker exec frame exchange of one Action process: bounded multiplexed output and its provider-call lines.

The invocation is written while output drains on one non-blocking socket within one deadline (ADR-0093). With a
provider broker, stdin stays open: each provider-call line the Action writes is answered by one reply line before
the terminal frame (ADR-0106).
"""

import select
import socket
import struct
import time
from contextlib import suppress
from typing import Protocol

from protocol.http.v1 import strict_json


class ProviderBroker(Protocol):
    """Team's answer to an Action's provider calls; ``calls`` counts every call frame the attempt sent."""

    calls: int

    def __call__(self, frame: object, deadline: float) -> bytes: ...

    def release(self) -> None:
        """Return what the last reply held once it has been written or abandoned; idempotent."""


class _FrameReader:
    """Parse Docker's multiplexed exec frames incrementally within one bound on the output buffered at once."""

    def __init__(self, maximum: int) -> None:
        self._maximum = maximum
        self._pending = bytearray()
        self._stdout = bytearray()
        self._stderr = bytearray()
        # The end of the terminal stdout line once it arrived; nothing may follow it (ADR-0106).
        self._terminal: int | None = None

    def feed(self, data: bytes) -> None:
        self._pending.extend(data)
        while len(self._pending) >= _FRAME_HEADER_BYTES:
            stream_id, length = struct.unpack(">BxxxL", self._pending[:_FRAME_HEADER_BYTES])
            if stream_id not in {1, 2}:
                raise ValueError("invalid Assistant RPC stream")
            if length > self._maximum + 1:
                raise ValueError("oversized Assistant RPC frame")
            end = _FRAME_HEADER_BYTES + length
            if len(self._pending) < end:
                return
            (self._stdout if stream_id == 1 else self._stderr).extend(self._pending[_FRAME_HEADER_BYTES:end])
            del self._pending[:end]
            if len(self._stdout) + len(self._stderr) > self._maximum:
                raise ValueError("oversized Assistant RPC response")

    def next_call(self) -> object | None:
        """Take the next complete provider-call line before the terminal line; output after the terminal is refused."""
        frame = None
        if self._terminal is None and (end := self._stdout.find(b"\n")) >= 0:
            try:
                line = strict_json.loads(bytes(self._stdout[:end]))
            except (UnicodeError, RecursionError) as exc:
                raise ValueError("invalid Assistant RPC line") from exc
            if isinstance(line, dict) and line.get("type") == "fetch":
                del self._stdout[: end + 1]
                frame = line
            else:
                self._terminal = end + 1
        if self._terminal is not None and len(self._stdout) > self._terminal:
            raise ValueError("Assistant RPC output follows its terminal frame")
        return frame

    def finish(self) -> tuple[bytes, bytes]:
        if self._pending:
            raise ValueError("truncated Assistant RPC frame")
        return bytes(self._stdout), bytes(self._stderr)


_FRAME_HEADER_BYTES = 8
_CHUNK_BYTES = 64 * 1024


def exchange_rpc_frames(
    raw_socket: socket.socket,
    data: bytes,
    deadline: float,
    maximum: int,
    broker: ProviderBroker | None = None,
) -> tuple[bytes, bytes]:
    """Write stdin while draining output; return the bounded stdout and stderr at end of stream.

    Reading and writing interleave on a non-blocking socket within one deadline, so a workload that answers before it
    reads all of a large invocation never stalls on a full buffer (ADR-0093). Without a broker, stdin is one request
    and is half-closed once written. With one, the request is the first stdin line and stdin stays open: each complete
    stdout provider-call line is answered by one broker line, and the first other line is the terminal frame (ADR-0106).
    A call is answered only once the previous reply has been written, so at most one reply is ever held.
    """
    reader = _FrameReader(maximum)
    pending = bytearray(data if broker is None else data + b"\n")
    closed = False
    previous = raw_socket.gettimeout()
    raw_socket.setblocking(False)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            readable, writable, _ = select.select([raw_socket], [raw_socket] if pending else [], [], remaining)
            if not readable and not writable:
                raise TimeoutError
            if writable:
                _send_pending(raw_socket, pending, broker is not None)
                if not pending and broker is None and not closed:
                    closed = True
                    with suppress(OSError):
                        raw_socket.shutdown(socket.SHUT_WR)
            if readable:
                try:
                    chunk = raw_socket.recv(_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if not chunk:
                    return reader.finish()
                reader.feed(chunk)
            if broker is not None and not pending:
                broker.release()
                _answer_call(reader, broker, pending, deadline)
    finally:
        raw_socket.settimeout(previous)
        if broker is not None:
            broker.release()


def _send_pending(raw_socket: socket.socket, pending: bytearray, replies: bool) -> None:
    try:
        del pending[: raw_socket.send(pending[:_CHUNK_BYTES])]
    except BlockingIOError:
        pass
    except BrokenPipeError:
        # A provider-call reply that cannot be written ends the attempt as a transport fault, so no later call is
        # answered; without calls the workload merely stopped reading, and its output still decides the outcome.
        if replies:
            raise
        pending.clear()


def _answer_call(reader: _FrameReader, broker: ProviderBroker, pending: bytearray, deadline: float) -> None:
    frame = reader.next_call()
    if frame is None:
        return
    try:
        reply = broker(frame, deadline)
    except (RuntimeError, OSError, ValueError, TypeError, KeyError) as exc:
        # An unauditable or broken call ends the attempt as uncertain, never silently.
        raise OSError("the provider call failed inside Team") from exc
    pending.extend(reply + b"\n")
