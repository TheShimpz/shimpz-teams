"""Anonymous registry access for public Assistant artifacts, kept out of process arguments and logs."""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class AnonymousRegistryAccess:
    """Explicit anonymous access for public Assistant artifacts."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "AnonymousRegistryAccess()"

    def docker_auth_config(self) -> None:
        return None

    @contextmanager
    def docker_config(self) -> Iterator[str]:
        with tempfile.TemporaryDirectory(prefix="shimpz-registry-") as directory:
            document = b'{"auths":{}}'
            path = Path(directory, "config.json")
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(document)
            yield directory
