"""Strict Local admission for Developers-owned source-package v1 bytes."""

from __future__ import annotations

import hashlib
import io
import re
import struct
import tarfile
import zlib
from dataclasses import dataclass

_BLOCK_BYTES = 512
_MAX_PACKAGE_BYTES = 32 * 1024 * 1024
_MAX_REGULAR_FILES = 10_000
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_ICON_BYTES = 1024 * 1024
_MAX_RECORDS = _MAX_PACKAGE_BYTES // _BLOCK_BYTES
_REQUIRED_FILES = frozenset({"icon.png", "pyproject.toml", "shimpz.toml"})
_PORTABLE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ACTION_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*\.py$")


class SourcePackageError(RuntimeError):
    """A staged image does not contain one canonical source package."""


@dataclass(frozen=True, slots=True)
class SourcePackage:
    digest: str
    manifest: bytes
    icon: bytes


@dataclass(frozen=True, slots=True)
class _Record:
    path: str
    is_directory: bool
    contents: bytes


def admit(raw: bytes) -> SourcePackage:
    """Validate exact canonical source-package v1 bytes and return admitted display inputs."""
    if (
        not isinstance(raw, bytes)
        or not 3 * _BLOCK_BYTES <= len(raw) <= _MAX_PACKAGE_BYTES
        or len(raw) % _BLOCK_BYTES
        or raw[-2 * _BLOCK_BYTES :] != bytes(2 * _BLOCK_BYTES)
    ):
        raise SourcePackageError("the Local Assistant source package has an invalid size")
    records = _read_records(raw)
    files = _validate_records(records)
    if _build_archive(records) != raw:
        raise SourcePackageError("the Local Assistant source package is not canonical")
    icon = files["icon.png"]
    _validate_icon(icon)
    return SourcePackage(
        digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        manifest=files["shimpz.toml"],
        icon=icon,
    )


def _read_records(raw: bytes) -> tuple[_Record, ...]:
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members = archive.getmembers()
            if not members or len(members) > _MAX_RECORDS:
                raise SourcePackageError("the Local Assistant source package has invalid entries")
            return tuple(_read_record(archive, member) for member in members)
    except SourcePackageError:
        raise
    except (tarfile.TarError, OSError, EOFError, KeyError) as exc:
        raise SourcePackageError("the Local Assistant source package archive is invalid") from exc


def _read_record(archive: tarfile.TarFile, member: tarfile.TarInfo) -> _Record:
    if member.isdir():
        contents = b""
    elif member.isreg() and 0 <= member.size <= _MAX_FILE_BYTES:
        stream = archive.extractfile(member)
        if stream is None:
            raise SourcePackageError("the Local Assistant source package has invalid entries")
        contents = stream.read(_MAX_FILE_BYTES + 1)
        if len(contents) != member.size:
            raise SourcePackageError("the Local Assistant source package has invalid entries")
    else:
        raise SourcePackageError("the Local Assistant source package has invalid entries")
    return _Record(member.name, member.isdir(), contents)


def _validate_records(records: tuple[_Record, ...]) -> dict[str, bytes]:
    if tuple(record.path for record in records) != tuple(sorted(record.path for record in records)):
        raise SourcePackageError("the Local Assistant source package order is invalid")
    files = {record.path: record.contents for record in records if not record.is_directory}
    if len(files) > _MAX_REGULAR_FILES or len(files) != sum(not record.is_directory for record in records):
        raise SourcePackageError("the Local Assistant source package files are invalid")
    for path in files:
        _validate_source_path(path)
    if len({path.lower() for path in files}) != len(files):
        raise SourcePackageError("the Local Assistant source package files collide")
    if not _REQUIRED_FILES.issubset(files) or not any(path.startswith("actions/") for path in files):
        raise SourcePackageError("the Local Assistant source package is incomplete")
    if len(files["icon.png"]) > _MAX_ICON_BYTES:
        raise SourcePackageError("the Local Assistant icon is too large")
    expected_directories = _parent_directories(files)
    actual_directories = {record.path for record in records if record.is_directory}
    if actual_directories != expected_directories or len(actual_directories) != sum(
        record.is_directory for record in records
    ):
        raise SourcePackageError("the Local Assistant source package directories are invalid")
    return files


def _validate_source_path(path: str) -> None:
    try:
        encoded = path.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SourcePackageError("the Local Assistant source package path is invalid") from exc
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or len(encoded) > 256
        or len(parts) > 16
        or any(not part or part in {".", ".."} or _PORTABLE_SEGMENT_RE.fullmatch(part) is None for part in parts)
    ):
        raise SourcePackageError("the Local Assistant source package path is invalid")
    _split_path(path)
    if path in _REQUIRED_FILES:
        return
    if len(parts) == 2 and parts[0] == "actions" and _ACTION_RE.fullmatch(parts[1]) is not None:
        return
    if len(parts) >= 2 and parts[0] in {"lib", "tests"}:
        return
    raise SourcePackageError("the Local Assistant source package path is not allowed")


def _parent_directories(files: dict[str, bytes]) -> set[str]:
    directories: set[str] = set()
    for path in files:
        parts = path.split("/")
        directories.update("/".join(parts[:end]) for end in range(1, len(parts)))
    return directories


def _split_path(path: str) -> tuple[str, str]:
    if len(path) <= 100:
        return "", path
    try:
        prefix, name = path.rsplit("/", 1)
    except ValueError as exc:
        raise SourcePackageError("the Local Assistant source package path is invalid") from exc
    if len(prefix) > 155 or len(name) > 100:
        raise SourcePackageError("the Local Assistant source package path is invalid")
    return prefix, name


def _build_archive(records: tuple[_Record, ...]) -> bytes:
    output = bytearray()
    for record in records:
        output.extend(_build_header(record))
        output.extend(record.contents)
        output.extend(bytes((-len(record.contents)) % _BLOCK_BYTES))
    output.extend(bytes(2 * _BLOCK_BYTES))
    return bytes(output)


def _build_header(record: _Record) -> bytes:
    prefix, name = _split_path(record.path)
    header = bytearray(_BLOCK_BYTES)
    _put(header, 0, 100, name.encode("ascii"))
    _put(header, 100, 8, _octal(0o755 if record.is_directory else 0o644, 8))
    _put(header, 108, 8, _octal(0, 8))
    _put(header, 116, 8, _octal(0, 8))
    _put(header, 124, 12, _octal(len(record.contents), 12))
    _put(header, 136, 12, _octal(0, 12))
    _put(header, 148, 8, b"        ")
    _put(header, 156, 1, b"5" if record.is_directory else b"0")
    _put(header, 257, 6, b"ustar\0")
    _put(header, 263, 2, b"00")
    _put(header, 329, 8, _octal(0, 8))
    _put(header, 337, 8, _octal(0, 8))
    _put(header, 345, 155, prefix.encode("ascii"))
    _put(header, 148, 8, f"{sum(header):06o}\0 ".encode("ascii"))
    return bytes(header)


def _put(header: bytearray, offset: int, width: int, value: bytes) -> None:
    if len(value) > width:
        raise SourcePackageError("the Local Assistant source package header is invalid")
    header[offset : offset + width] = value.ljust(width, b"\0")


def _octal(value: int, width: int) -> bytes:
    encoded = f"{value:0{width - 1}o}\0".encode("ascii")
    if len(encoded) != width:
        raise SourcePackageError("the Local Assistant source package header is invalid")
    return encoded


def _validate_icon(contents: bytes) -> None:
    chunks = _icon_chunks(contents)
    kinds = [kind for kind, _data in chunks]
    if (
        not chunks
        or chunks[0][0] != b"IHDR"
        or chunks[-1] != (b"IEND", b"")
        or kinds.count(b"IHDR") != 1
        or kinds.count(b"IEND") != 1
        or b"IDAT" not in kinds
        or b"acTL" in kinds
    ):
        raise SourcePackageError("the Local Assistant icon is invalid")
    _validate_icon_header(chunks[0][1], kinds)


def _icon_chunks(contents: bytes) -> list[tuple[bytes, bytes]]:
    if not contents.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SourcePackageError("the Local Assistant icon is invalid")
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(contents):
        if len(contents) - offset < 12:
            raise SourcePackageError("the Local Assistant icon is invalid")
        length = struct.unpack(">I", contents[offset : offset + 4])[0]
        end = offset + 12 + length
        if end > len(contents):
            raise SourcePackageError("the Local Assistant icon is invalid")
        kind = contents[offset + 4 : offset + 8]
        data = contents[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", contents[offset + 8 + length : end])[0]
        if not kind.isalpha() or zlib.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
            raise SourcePackageError("the Local Assistant icon is invalid")
        chunks.append((kind, data))
        offset = end
    return chunks


def _validate_icon_header(ihdr: bytes, kinds: list[bytes]) -> None:
    if len(ihdr) != 13:
        raise SourcePackageError("the Local Assistant icon is invalid")
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", ihdr)
    valid_depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
    if (
        (width, height) != (1024, 1024)
        or depth not in valid_depths.get(color, set())
        or compression != 0
        or filtering != 0
        or interlace not in {0, 1}
        or (color == 3 and (b"PLTE" not in kinds or kinds.index(b"PLTE") > kinds.index(b"IDAT")))
    ):
        raise SourcePackageError("the Local Assistant icon is invalid")
