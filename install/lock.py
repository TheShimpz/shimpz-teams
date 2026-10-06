"""Advisory file locks over private lock files for Team-owned install state."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class FileLock:
    """Hold ``operation`` (``fcntl.LOCK_EX`` or ``LOCK_SH``) on ``path``; an OS failure raises ``error(message)``."""

    def __init__(self, path: Path, operation: int, error: type[Exception], message: str) -> None:
        self._path = path
        self._operation = operation
        self._error = error
        self._message = message
        self._stream = None

    def __enter__(self) -> None:
        descriptor = -1
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self._stream = os.fdopen(descriptor, "rb+")
            descriptor = -1
            fcntl.flock(self._stream, self._operation)
        except Exception as exc:
            if descriptor >= 0:
                os.close(descriptor)
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            if isinstance(exc, OSError):
                raise self._error(self._message) from exc
            raise

    def __exit__(self, *_args: object) -> None:
        stream = self._stream
        if stream is None:
            raise self._error(self._message)
        try:
            fcntl.flock(stream, fcntl.LOCK_UN)
        finally:
            stream.close()
