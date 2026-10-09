"""Verified binary storage for canonical Assistant icons."""

import hashlib
import os
import stat
import tempfile
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from install.bindings import DynamicAssistantBinding
from protocol.http.v1 import payload as http_payload

_MAX_ICON_BYTES = 1024 * 1024


class AssistantIconError(RuntimeError):
    """A canonical Assistant icon is missing or violates its binding."""


# Reads the current bindings; deletion calls it only inside the custody lock, so it sees every committed binding.
References = Callable[[], Iterable[DynamicAssistantBinding]]


class AssistantIconStore:
    """Persist immutable icons in provenance-separated namespaces.

    An install retains its icon under a pin until its binding commits or fails. Reference-aware deletion reads the
    current bindings and the pins under one custody lock and never removes a pinned or bound icon, so one Team's
    failed install or uninstall cannot delete an icon that another Team is about to bind.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._custody = threading.Lock()
        self._pins: Counter[Path] = Counter()

    def retained(
        self, resolution: dict[str, Any], contents: bytes, references: References
    ) -> AbstractContextManager[None]:
        """Hold a publication's icon while its binding commits, then discard it unless a binding references it."""
        return self._retained(_publication_identity(resolution), contents, references)

    def retained_local(
        self, record: dict[str, Any], contents: bytes, references: References
    ) -> AbstractContextManager[None]:
        """Hold a Local snapshot's icon while its binding commits, then discard it unless a binding references it."""
        return self._retained(_local_identity(record), contents, references)

    @contextmanager
    def _retained(self, identity: _IconIdentity, contents: bytes, references: References) -> Iterator[None]:
        path = self._path(identity)
        with self._custody:
            self._pins[path] += 1
        try:
            self._put(identity, contents)
            yield
        finally:
            with self._custody:
                self._pins[path] -= 1
                if not self._pins[path]:
                    del self._pins[path]
            self._discard(identity, references)

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

    def discard_unreferenced(self, source_digest: str, references: References) -> None:
        self._discard(
            _publication_identity({"source_digest": source_digest, "icon_digest": source_digest}),
            references,
        )

    def discard_binding(self, retired: DynamicAssistantBinding, references: References) -> None:
        self._discard(_binding_identity(retired), references)

    def retire(
        self, retiring: DynamicAssistantBinding, references: References, delete_binding: Callable[[], object]
    ) -> None:
        """Delete a binding and its icon in one custody transaction, keeping an icon another binding or install holds.

        Deciding the icon's fate and deleting the binding happen under the same lock, so two Teams retiring bindings
        that share an icon can never both keep it. The icon goes first: when its removal fails, the binding stays as
        the retry anchor.
        """
        identity = _binding_identity(retiring)
        owner = (retiring.team_id, retiring.assistant_id)
        path = self._path(identity)
        with self._custody:
            others = {_reference(item) for item in references() if (item.team_id, item.assistant_id) != owner}
            if not self._pins[path] and (identity.namespace, identity.key) not in others:
                _unlink(path)
            delete_binding()

    def _discard(self, identity: _IconIdentity, references: References) -> None:
        path = self._path(identity)
        with self._custody:
            if self._pins[path] or (identity.namespace, identity.key) in {_reference(item) for item in references()}:
                return
            _unlink(path)

    def _path(self, identity: _IconIdentity) -> Path:
        digest = http_payload.SOURCE_DIGEST_RE.fullmatch(identity.key)
        if digest is None or identity.namespace not in {"published", "local"}:
            raise AssistantIconError("the Assistant icon key is invalid")
        return self._root / f"{identity.namespace}-{identity.key.removeprefix('sha256:')}.png"

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
        or http_payload.SOURCE_DIGEST_RE.fullmatch(source) is None
        or not isinstance(expected, str)
        or http_payload.SOURCE_DIGEST_RE.fullmatch(expected) is None
    ):
        raise AssistantIconError("the Assistant icon identity is invalid")
    return _IconIdentity("published", source, expected)


def _local_identity(record: dict[str, Any]) -> _IconIdentity:
    image_id = record.get("image_id")
    expected = record.get("icon_digest")
    if (
        not isinstance(image_id, str)
        or http_payload.SOURCE_DIGEST_RE.fullmatch(image_id) is None
        or not isinstance(expected, str)
        or http_payload.SOURCE_DIGEST_RE.fullmatch(expected) is None
    ):
        raise AssistantIconError("the Local Assistant icon identity is invalid")
    return _IconIdentity("local", image_id, expected)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise AssistantIconError("the retired Assistant icon cannot be removed") from exc


def _reference(binding: DynamicAssistantBinding) -> tuple[str, object]:
    key = "image_id" if binding.provenance == "local" else "source_digest"
    return binding.provenance, binding.document.get(key)


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
