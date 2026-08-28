"""Local admission against the pinned Developers source-package authority."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
