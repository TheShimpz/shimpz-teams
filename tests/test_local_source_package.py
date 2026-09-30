"""Local admission against the pinned Developers source-package authority."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tarfile
import tracemalloc
import types
import unittest
from pathlib import Path
from unittest import mock

from local.install import source_package

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "protocol" / "source-package"
AUTHORITY = PROTOCOL / "v1"


def _authority_module() -> types.ModuleType:
    name = "team_source_package_authority"
    spec = importlib.util.spec_from_file_location(name, AUTHORITY / "verify.py")
    if spec is None or spec.loader is None:
        raise AssertionError("source-package authority cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _packages() -> list[tuple[dict[str, object], bytes, list[object]]]:
    authority = _authority_module()
    contract = authority.load_object(AUTHORITY / "contract.json")
    vectors = authority.load_object(AUTHORITY / "vectors.json")
    packages = []
    for case in vectors["cases"]:
        if not case["valid"]:
            continue
        entries, _splits = authority.validate_entries(authority.expand_case(case), contract)
        raw = authority.build_archive(authority.canonical_records(entries, contract), contract)
        packages.append((case, raw, entries))
    return packages


def _gnu_sparse_header(name: str, declared: int) -> bytes:
    """Return one old-GNU sparse header whose single hole declares ``declared`` bytes with no stored data."""
    header = bytearray(source_package._BLOCK_BYTES)
    header[0 : len(name)] = name.encode("ascii")
    header[100:108] = source_package._octal(0o644, 8)
    header[124:136] = source_package._octal(0, 12)
    header[156:157] = tarfile.GNUTYPE_SPARSE
    header[257:265] = b"ustar  \0"
    header[386:398] = source_package._octal(declared, 12)
    header[398:410] = source_package._octal(0, 12)
    header[483:495] = source_package._octal(declared, 12)
    header[148:156] = b"        "
    header[148:156] = f"{sum(header):06o}\0 ".encode("ascii")
    return bytes(header)


class LocalSourcePackageTests(unittest.TestCase):
    def test_admits_every_pinned_valid_vector(self) -> None:
        for case, raw, entries in _packages():
            with self.subTest(case=case["name"]):
                admitted = source_package.admit(raw)
                contents = {entry.path: entry.content.materialize() for entry in entries}

                self.assertEqual(admitted.manifest, contents["shimpz.toml"])
                self.assertEqual(admitted.icon, contents["icon.png"])
                self.assertRegex(admitted.digest, r"^sha256:[0-9a-f]{64}$")

    def test_rejects_noncanonical_order_duplicates_and_unknown_paths(self) -> None:
        _case, raw, _entries = _packages()[0]
        records = source_package._read_records(raw)
        mutations = (
            source_package._build_archive(tuple(reversed(records))),
            source_package._build_archive((*records, records[-1])),
            source_package._build_archive(
                tuple(
                    sorted(
                        (*records, source_package._Record("secret.txt", False, b"secret")),
                        key=lambda record: record.path,
                    )
                )
            ),
        )

        for mutation in mutations:
            with self.subTest(size=len(mutation)), self.assertRaises(source_package.SourcePackageError):
                source_package.admit(mutation)

    def test_rejects_widened_truncated_and_noncanonical_archives(self) -> None:
        _case, raw, _entries = _packages()[0]
        noncanonical = bytearray(raw)
        noncanonical[100] = ord("0") if noncanonical[100] != ord("0") else ord("1")

        for mutation in (raw + bytes(512), raw[:-512], bytes(noncanonical)):
            with self.subTest(size=len(mutation)), self.assertRaises(source_package.SourcePackageError):
                source_package.admit(mutation)

    def test_rejects_tampered_icon_structure(self) -> None:
        _case, raw, _entries = _packages()[0]
        records = list(source_package._read_records(raw))
        icon_index = next(index for index, record in enumerate(records) if record.path == "icon.png")
        icon = bytearray(records[icon_index].contents)
        icon[-1] ^= 1
        records[icon_index] = source_package._Record("icon.png", False, bytes(icon))

        with self.assertRaisesRegex(source_package.SourcePackageError, "icon"):
            source_package.admit(source_package._build_archive(tuple(records)))

    def test_records_the_exact_developers_authority(self) -> None:
        upstream = json.loads((PROTOCOL / "upstream.json").read_bytes())

        self.assertEqual(upstream["repository"], "https://github.com/TheShimpz/shimpz-developers")
        self.assertEqual(upstream["path"], "protocol/source-package/v1")
        self.assertRegex(upstream["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(upstream["tree"], r"^[0-9a-f]{40}$")
        self.assertRegex(upstream["contract_files_sha256"], r"^[0-9a-f]{64}$")

    def test_tar_record_shapes_fail_closed(self) -> None:
        with self.assertRaisesRegex(source_package.SourcePackageError, "invalid entries"):
            source_package._read_records(bytes(2 * source_package._BLOCK_BYTES))

        archive = mock.Mock()
        regular = tarfile.TarInfo("shimpz.toml")
        regular.size = 4
        archive.extractfile.return_value = None
        with self.assertRaisesRegex(source_package.SourcePackageError, "invalid entries"):
            source_package._read_record(archive, regular)

        archive.extractfile.return_value = mock.Mock(read=mock.Mock(return_value=b"abc"))
        with self.assertRaisesRegex(source_package.SourcePackageError, "invalid entries"):
            source_package._read_record(archive, regular)

    def test_member_encodings_and_declared_sizes_fail_closed_before_reading(self) -> None:
        def member(name: str, kind: bytes = tarfile.REGTYPE, size: int = 0) -> tarfile.TarInfo:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = size
            return info

        sparse = member("lib/sparse.py", size=1)
        sparse.sparse = [(0, 1)]
        extended = member("lib/extended.py")
        extended.pax_headers = {"comment": "x"}
        refused = (
            member("lib/large.py", size=source_package._MAX_FILE_BYTES + 1),
            member("lib/link.py", tarfile.SYMTYPE),
            member("lib/old.py", tarfile.AREGTYPE),
            member("lib/contiguous.py", tarfile.CONTTYPE),
            member("lib/gnu.py", tarfile.GNUTYPE_SPARSE),
            member("lib", tarfile.DIRTYPE, 1),
            sparse,
            extended,
        )
        for info in refused:
            with (
                self.subTest(member=info.name),
                self.assertRaisesRegex(source_package.SourcePackageError, "invalid entries"),
            ):
                source_package._validate_members([info], 4096)

        admitted = [member("lib", tarfile.DIRTYPE), member("lib/a.py", size=600), member("lib/b.py", size=600)]
        source_package._validate_members(admitted, 1200)
        with self.assertRaisesRegex(source_package.SourcePackageError, "invalid entries"):
            source_package._validate_members(admitted, 1199)
        with (
            mock.patch.object(source_package, "_MAX_RECORDS", 2),
            self.assertRaisesRegex(source_package.SourcePackageError, "invalid entries"),
        ):
            source_package._validate_members(admitted, 1200)

    def test_sparse_archives_are_refused_before_any_expansion(self) -> None:
        declared = source_package._MAX_FILE_BYTES
        gnu = b"".join(_gnu_sparse_header(f"lib/f{index:02}.py", declared) for index in range(18))
        pax = io.BytesIO()
        with tarfile.open(fileobj=pax, mode="w", format=tarfile.PAX_FORMAT) as archive:
            info = tarfile.TarInfo("lib/pax.py")
            info.pax_headers = {"GNU.sparse.size": str(declared), "GNU.sparse.map": f"{declared},0"}
            archive.addfile(info, io.BytesIO())
        for name, raw in (("gnu", gnu + bytes(2 * source_package._BLOCK_BYTES)), ("pax", pax.getvalue())):
            with self.subTest(encoding=name):
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
                    self.assertTrue(all(member.sparse is not None for member in archive.getmembers()))
                    self.assertEqual({member.size for member in archive.getmembers()}, {declared})
                tracemalloc.start()
                try:
                    with (
                        mock.patch.object(tarfile.TarFile, "extractfile", autospec=True) as extract,
                        self.assertRaisesRegex(source_package.SourcePackageError, "invalid entries"),
                    ):
                        source_package.admit(raw)
                    _current, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                extract.assert_not_called()
                self.assertLess(peak, declared)

    def test_source_record_inventory_fail_closed(self) -> None:
        _case, raw, _entries = _packages()[0]
        records = source_package._read_records(raw)
        files = tuple(record for record in records if not record.is_directory)

        case_collision = tuple(
            sorted(
                (
                    *records,
                    source_package._Record("lib/A.py", False, b""),
                    source_package._Record("lib/a.py", False, b""),
                    source_package._Record("lib", True, b""),
                ),
                key=lambda record: record.path,
            )
        )
        incomplete = tuple(record for record in records if not record.path.startswith("actions/"))
        missing_directories = tuple(sorted(files, key=lambda record: record.path))
        duplicate_directory = tuple(
            sorted(
                (*records, next(record for record in records if record.is_directory)),
                key=lambda record: record.path,
            )
        )
        for mutation, message in (
            (case_collision, "collide"),
            (incomplete, "incomplete"),
            (missing_directories, "directories"),
            (duplicate_directory, "directories"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(source_package.SourcePackageError, message):
                source_package._validate_records(mutation)

        with (
            mock.patch.object(source_package, "_MAX_REGULAR_FILES", 0),
            self.assertRaisesRegex(
                source_package.SourcePackageError,
                "files are invalid",
            ),
        ):
            source_package._validate_records(records)
        with (
            mock.patch.object(source_package, "_MAX_ICON_BYTES", 0),
            self.assertRaisesRegex(
                source_package.SourcePackageError,
                "icon is too large",
            ),
        ):
            source_package._validate_records(records)

    def test_source_paths_and_header_bounds_fail_closed(self) -> None:
        invalid_paths = (
            "ü.py",
            "",
            "/absolute",
            "lib/" + ("a" * 253),
            "/".join(("lib", *("a" for _index in range(16)))),
            "lib//file.py",
            "lib/./file.py",
            "lib/../file.py",
            "lib/file$.py",
            "unknown.txt",
        )
        for path in invalid_paths:
            with self.subTest(path=path[:40]), self.assertRaisesRegex(source_package.SourcePackageError, "path"):
                source_package._validate_source_path(path)

        self.assertEqual(source_package._split_path("lib/" + ("a" * 97)), ("lib", "a" * 97))
        for path in ("a" * 101, ("a" * 156) + "/file", "lib/" + ("a" * 101)):
            with self.subTest(length=len(path)), self.assertRaisesRegex(source_package.SourcePackageError, "path"):
                source_package._split_path(path)

        with self.assertRaisesRegex(source_package.SourcePackageError, "header"):
            source_package._put(bytearray(4), 0, 1, b"too long")
        with self.assertRaisesRegex(source_package.SourcePackageError, "header"):
            source_package._octal(8**8, 2)

    def test_icon_chunk_and_header_variants_fail_closed(self) -> None:
        signature = b"\x89PNG\r\n\x1a\n"
        for contents in (
            b"not-png",
            signature + b"x",
            signature + b"\x00\x00\x00\xffIHDR" + bytes(4),
        ):
            with self.subTest(size=len(contents)), self.assertRaisesRegex(source_package.SourcePackageError, "icon"):
                source_package._icon_chunks(contents)

        valid_header = bytes.fromhex("00000400000004000806000000")
        invalid_chunk_sets = (
            [],
            [(b"IDAT", b""), (b"IEND", b"")],
            [(b"IHDR", valid_header), (b"IDAT", b"")],
            [(b"IHDR", valid_header), (b"IHDR", valid_header), (b"IDAT", b""), (b"IEND", b"")],
            [(b"IHDR", valid_header), (b"IDAT", b""), (b"IEND", b""), (b"IEND", b"")],
            [(b"IHDR", valid_header), (b"IEND", b"")],
            [(b"IHDR", valid_header), (b"acTL", b""), (b"IDAT", b""), (b"IEND", b"")],
        )
        for chunks in invalid_chunk_sets:
            with (
                mock.patch.object(source_package, "_icon_chunks", return_value=chunks),
                self.assertRaisesRegex(
                    source_package.SourcePackageError,
                    "icon",
                ),
            ):
                source_package.validate_icon(b"ignored")

        invalid_headers = (
            b"short",
            bytes.fromhex("00000001000004000806000000"),
            bytes.fromhex("00000400000004000106000000"),
            bytes.fromhex("00000400000004000806010000"),
            bytes.fromhex("00000400000004000806000100"),
            bytes.fromhex("00000400000004000806000002"),
        )
        for header in invalid_headers:
            with self.subTest(header=header.hex()), self.assertRaisesRegex(source_package.SourcePackageError, "icon"):
                source_package._validate_icon_header(header, [b"IHDR", b"IDAT", b"IEND"])

        palette_header = bytes.fromhex("00000400000004000803000000")
        for kinds in ([b"IHDR", b"IDAT", b"IEND"], [b"IHDR", b"IDAT", b"PLTE", b"IEND"]):
            with self.subTest(kinds=kinds), self.assertRaisesRegex(source_package.SourcePackageError, "icon"):
                source_package._validate_icon_header(palette_header, kinds)


if __name__ == "__main__":
    unittest.main()
