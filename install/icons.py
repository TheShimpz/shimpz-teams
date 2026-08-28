"""Verified binary storage for canonical Assistant icons."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from install.bindings import DynamicAssistantBinding

_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
_MAX_ICON_BYTES = 1024 * 1024


class AssistantIconError(RuntimeError):
    """A canonical Assistant icon is missing or violates its binding."""


class AssistantIconStore:
    """Persist immutable icons in provenance-separated namespaces."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, resolution: dict[str, Any], contents: bytes) -> None:
        self._put(_publication_identity(resolution), contents)

    def put_local(self, record: dict[str, Any], contents: bytes) -> None:
        self._put(_local_identity(record), contents)

    def _put(self, identity: _IconIdentity, contents: bytes) -> None:
        _verify(contents, identity.digest)
        destination = self._path(identity)
        if destination.exists():
            if self._read(identity) != contents:
                raise AssistantIconError("the Assistant icon conflicts with its immutable identity")
            return
        self._write(destination, contents)

    def read(self, resolution: dict[str, Any]) -> bytes:
        return self._read(_publication_identity(resolution))

    def read_binding(self, binding: DynamicAssistantBinding) -> bytes:
        return self._read(_binding_identity(binding))

    def _read(self, identity: _IconIdentity) -> bytes:
        path = self._path(identity)
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
        _verify(contents, identity.digest)
        return contents

    def discard_unreferenced(
        self,
        source_digest: str,
        bindings: Iterable[DynamicAssistantBinding],
    ) -> None:
        identity = _publication_identity({"source_digest": source_digest, "icon_digest": source_digest})
        if any(
            binding.provenance == "published" and binding.document.get("source_digest") == source_digest
            for binding in bindings
        ):
            return
        self._discard(identity)

    def discard_binding(
        self,
        retired: DynamicAssistantBinding,
        bindings: Iterable[DynamicAssistantBinding],
    ) -> None:
        identity = _binding_identity(retired)
        if any(_binding_identity(binding) == identity for binding in bindings):
            return
        self._discard(identity)

    def _discard(self, identity: _IconIdentity) -> None:
        try:
            self._path(identity).unlink(missing_ok=True)
        except OSError as exc:
            raise AssistantIconError("the retired Assistant icon cannot be removed") from exc

    def _path(self, identity: _IconIdentity) -> Path:
        match = _DIGEST.fullmatch(identity.key)
        if match is None or identity.namespace not in {"published", "local"}:
            raise AssistantIconError("the Assistant icon key is invalid")
        return self._root / f"{identity.namespace}-{match.group(1)}.png"

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


@dataclass(frozen=True, slots=True)
class _IconIdentity:
    namespace: str
    key: str
    digest: str


def _publication_identity(resolution: dict[str, Any]) -> _IconIdentity:
    source = resolution.get("source_digest")
    expected = resolution.get("icon_digest")
    if (
        not isinstance(source, str)
        or _DIGEST.fullmatch(source) is None
        or not isinstance(expected, str)
        or _DIGEST.fullmatch(expected) is None
    ):
        raise AssistantIconError("the Assistant icon identity is invalid")
    return _IconIdentity("published", source, expected)


def _local_identity(record: dict[str, Any]) -> _IconIdentity:
    image_id = record.get("image_id")
    expected = record.get("icon_digest")
    if (
        not isinstance(image_id, str)
        or _DIGEST.fullmatch(image_id) is None
        or not isinstance(expected, str)
        or _DIGEST.fullmatch(expected) is None
    ):
        raise AssistantIconError("the Local Assistant icon identity is invalid")
    return _IconIdentity("local", image_id, expected)


def _binding_identity(binding: DynamicAssistantBinding) -> _IconIdentity:
    if binding.provenance == "published":
        return _publication_identity(binding.document)
    if binding.provenance == "local":
        return _local_identity(binding.document)
    raise AssistantIconError("the Assistant icon provenance is invalid")


def _verify(contents: bytes, expected_digest: str) -> None:
    if not contents or len(contents) > _MAX_ICON_BYTES:
        raise AssistantIconError("the Assistant icon is invalid")
    actual = f"sha256:{hashlib.sha256(contents).hexdigest()}"
    if actual != expected_digest:
        raise AssistantIconError("the Assistant icon digest does not match")
