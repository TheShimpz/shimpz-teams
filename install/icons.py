"""Verified binary storage for canonical Assistant publication icons."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from install.bindings import DynamicAssistantBinding

_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_MAX_ICON_BYTES = 1024 * 1024


class AssistantIconError(RuntimeError):
    """A canonical Assistant icon is missing or violates its binding."""


class AssistantIconStore:
    """Persist immutable icons by source digest and verify every read."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, resolution: dict[str, Any], contents: bytes) -> None:
        source, expected = _identity(resolution)
        _verify(contents, expected)
        destination = self._path(source)
        if destination.exists():
            if self.read(resolution) != contents:
                raise AssistantIconError("the Assistant icon conflicts with its source digest")
            return
        self._write(destination, contents)

    def read(self, resolution: dict[str, Any]) -> bytes:
        source, expected = _identity(resolution)
        path = self._path(source)
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_ICON_BYTES:
                raise AssistantIconError("the Assistant icon is invalid")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                contents = stream.read(_MAX_ICON_BYTES + 1)
        except (OSError, ValueError) as exc:
            raise AssistantIconError("the Assistant icon is unavailable") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        _verify(contents, expected)
        return contents

    def discard_unreferenced(
        self,
        source_digest: str,
        bindings: Iterable[DynamicAssistantBinding],
    ) -> None:
        if any(binding.resolution.get("source_digest") == source_digest for binding in bindings):
            return
        try:
            self._path(source_digest).unlink(missing_ok=True)
        except OSError as exc:
            raise AssistantIconError("the retired Assistant icon cannot be removed") from exc

    def _path(self, source_digest: str) -> Path:
        match = _DIGEST.fullmatch(source_digest)
        if match is None:
            raise AssistantIconError("the Assistant source digest is invalid")
        return self._root / f"{match.group(1)}.png"

    def _write(self, destination: Path, contents: bytes) -> None:
        temporary: Path | None = None
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", dir=self._root, prefix=".icon.", delete=False) as stream:
                temporary = Path(stream.name)
                temporary.chmod(0o600)
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)
            temporary = None
        except OSError as exc:
            raise AssistantIconError("the Assistant icon cannot be persisted") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _identity(resolution: dict[str, Any]) -> tuple[str, str]:
    source = resolution.get("source_digest")
    expected = resolution.get("icon_digest")
    if not isinstance(source, str) or not isinstance(expected, str) or _DIGEST.fullmatch(expected) is None:
        raise AssistantIconError("the Assistant icon identity is invalid")
    return source, expected


def _verify(contents: bytes, expected_digest: str) -> None:
    if not contents or len(contents) > _MAX_ICON_BYTES:
        raise AssistantIconError("the Assistant icon is invalid")
    actual = f"sha256:{hashlib.sha256(contents).hexdigest()}"
    if actual != expected_digest:
        raise AssistantIconError("the Assistant icon digest does not match")
