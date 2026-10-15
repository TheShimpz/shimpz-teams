"""The single-file Docker archive an Assistant container serves, and a fake container that serves one file."""

import io
import tarfile

from assistant import manifest as assistant_manifest


def archive(content: bytes, *, name: str = "shimpz.toml", mode: int = 0o444, member_type: bytes | None = None) -> bytes:
    """One tar archive holding exactly one member, as Docker's archive read answers for one file."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        member = tarfile.TarInfo(name)
        member.size = len(content)
        member.mode = mode
        if member_type is not None:
            member.type = member_type
        bundle.addfile(member, io.BytesIO(content))
    return output.getvalue()


class FileContainer:
    """One immutable container generation whose archive read serves only its one file and counts the reads.

    An absent file answers like Docker's missing path; ``split_at`` delivers the archive in two chunks.
    """

    def __init__(
        self,
        container_id: str,
        path: str,
        content: bytes | None,
        *,
        name: str,
        mode: int = 0o444,
        split_at: int | None = None,
    ) -> None:
        self.id = container_id
        self.path = path
        self.content = content
        self.name = name
        self.mode = mode
        self.split_at = split_at
        self.reads = 0

    def get_archive(self, path: str):
        self.reads += 1
        if path != self.path:
            raise AssertionError(f"unexpected archive path: {path}")
        if self.content is None:
            raise FileNotFoundError(path)
        payload = archive(self.content, name=self.name, mode=self.mode)
        chunks = (payload,) if self.split_at is None else (payload[: self.split_at], payload[self.split_at :])
        return iter(chunks), {"name": self.name, "size": len(self.content), "mode": self.mode}


class ManifestContainer(FileContainer):
    """A container generation serving its fixed Assistant manifest."""

    def __init__(self, container_id: str, content: bytes, *, split_at: int | None = None) -> None:
        super().__init__(container_id, assistant_manifest.MANIFEST_PATH, content, name="shimpz.toml", split_at=split_at)
