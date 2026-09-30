"""Executable conformance coverage for the vendored Developers source-package verifier."""

from __future__ import annotations

import contextlib
import copy
import io
import runpy
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

from test_protocol_verifier_edges import _execute

SOURCE_PACKAGE = Path(__file__).resolve().parents[1] / "protocol/source-package/v1"


def _load_source_package_verifier() -> dict[str, object]:
    return runpy.run_path(str(SOURCE_PACKAGE / "verify.py"), run_name="source_package_verifier")


def _png_chunk(kind: bytes, data: bytes = b"") -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + kind + data + checksum.to_bytes(4, "big")


class SourcePackageVerifierEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api = _load_source_package_verifier()
        cls.module_globals = cls.api["main"].__globals__
        cls.contract = cls.api["load_object"](SOURCE_PACKAGE / "contract.json")
        cls.vectors = cls.api["load_object"](SOURCE_PACKAGE / "vectors.json")

    def case(self, name: str) -> dict[str, object]:
        return copy.deepcopy(next(case for case in self.vectors["cases"] if case["name"] == name))

    def assert_stops(self, function, *args) -> None:
        with self.assertRaises(SystemExit):
            function(*args)

    def test_current_authority_vectors_main_and_sync_are_valid(self) -> None:
        self.api["validate_contract"](self.contract)
        self.api["verify_vectors"](self.contract, self.vectors)
        self.api["verify_authority"]()
        content = self.api["Content"](b"ab", 3)
        self.assertEqual((content.size, content.materialize()), (6, b"ababab"))

        output = io.StringIO()
        with (
            mock.patch.dict(
                self.module_globals,
                {"parse_args": mock.Mock(return_value=types.SimpleNamespace(sync=None))},
            ),
            contextlib.redirect_stdout(output),
        ):
            self.api["main"]()
        self.assertIn("golden vectors are valid", output.getvalue())

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "mirror"
            with (
                mock.patch.dict(
                    self.module_globals,
                    {"parse_args": mock.Mock(return_value=types.SimpleNamespace(sync=target))},
                ),
                contextlib.redirect_stdout(output),
            ):
                self.api["main"]()
            self.assertEqual(
                {path.name for path in target.iterdir()},
                {*self.api["AUTHORITY_FILES"], self.api["MANIFEST"]},
            )

        with mock.patch.object(sys, "argv", ["verify.py"]):
            self.assertIn(
                "golden vectors are valid",
                _execute(SOURCE_PACKAGE / "verify.py", run_name="__main__"),
            )

    def test_json_shape_and_contract_helpers_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            malformed = root / "malformed.json"
            malformed.write_text("{", encoding="utf-8")
            array = root / "array.json"
            array.write_text("[]", encoding="utf-8")
            for path in (root / "missing.json", malformed, array):
                with self.subTest(path=path):
                    self.assert_stops(self.api["load_object"], path)

        for function, value in (
            (self.api["require_object"], []),
            (self.api["require_list"], {}),
            (self.api["require_integer"], True),
            (self.api["require_integer"], -1),
        ):
            with self.subTest(function=function.__name__, value=value):
                self.assert_stops(function, value, "value")
        self.assert_stops(self.api["require_equal"], "actual", "expected", "value")
        changed = copy.deepcopy(self.contract)
        changed["media_type"] = "invalid"
        self.assert_stops(self.api["validate_contract"], changed)

    def test_content_entries_and_generators_reject_malformed_vectors(self) -> None:
        content_from = self.api["content_from"]
        invalid = (
            {},
            {"text": "a", "base64": "YQ=="},
            {"text": 1},
            {"base64": 1},
            {"base64": "!"},
            {"repeat": {"byte": "ab", "count": 1}},
        )
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assert_stops(content_from, raw, "entry")
        self.assertEqual(content_from({"base64": "YQ=="}, "entry").materialize(), b"a")
        self.assertEqual(content_from({"repeat": {"byte": "a", "count": 2}}, "entry").size, 2)

        self.assert_stops(self.api["source_entry_from"], {"path": 1, "type": "regular_file"}, "entry")
        self.assert_stops(
            self.api["generated_entries"],
            {"root": 1, "prefix": "p", "suffix": ".py", "text": "x", "start": 0, "count": 1, "width": 1},
            "generator",
        )
        generated = self.api["generated_entries"](
            {"root": "lib", "prefix": "p", "suffix": ".py", "text": "x", "start": 1, "count": 2, "width": 2},
            "generator",
        )
        self.assertEqual([entry.path for entry in generated], ["lib/p01.py", "lib/p02.py"])

    def test_path_entry_and_icon_edge_contracts(self) -> None:
        limits = self.contract["limits"]
        path_rules = self.contract["path"]
        with self.assertRaisesRegex(self.api["ContractViolationError"], "ustar_name_too_long"):
            self.api["split_ustar_path"]("lib/" + ("a" * 101), limits)
        with self.assertRaisesRegex(self.api["ContractViolationError"], "invalid_path_segment"):
            self.api["validate_path"]("lib/bad name.py", path_rules, limits)
        with self.assertRaisesRegex(self.api["ContractViolationError"], "invalid_entry"):
            self.api["validate_allowlist"]("actions/bad.txt", ["actions", "bad.txt"], self.contract["source_tree"])

        entries = self.api["expand_case"](self.case("minimum"))
        with self.assertRaisesRegex(self.api["ContractViolationError"], "duplicate_path"):
            self.api["validate_entries"]([*entries, entries[0]], self.contract)
        contract_without_icon = copy.deepcopy(self.contract)
        contract_without_icon["source_tree"]["required_root_files"].remove("icon.png")
        entries_without_icon = [entry for entry in entries if entry.path != "icon.png"]
        self.api["validate_entries"](entries_without_icon, contract_without_icon)

        signature = b"\x89PNG\r\n\x1a\n"
        invalid_icons = (
            b"not-png",
            signature + b"short",
            signature + (100).to_bytes(4, "big") + b"IHDR0000",
            signature + _png_chunk(b"1234"),
        )
        for icon in invalid_icons:
            with self.subTest(icon=icon), self.assertRaisesRegex(self.api["ContractViolationError"], "invalid_icon"):
                self.api["parse_icon_chunks"](icon)

        class ChangingLength(bytes):
            calls = 0

            def __len__(self) -> int:
                self.calls += 1
                return 21 if self.calls >= 5 else 20

        changing = ChangingLength(signature + _png_chunk(b"IEND"))
        with self.assertRaisesRegex(self.api["ContractViolationError"], "invalid_icon"):
            self.api["parse_icon_chunks"](changing)

        ihdr = (1024).to_bytes(4, "big") * 2 + bytes((8, 6, 0, 0, 0))
        structural = (
            [],
            [(b"IHDR", ihdr), (b"IDAT", b"")],
            [(b"IHDR", ihdr), (b"IEND", b"")],
            [(b"IHDR", b"short"), (b"IDAT", b""), (b"IEND", b"")],
            [(b"IHDR", ihdr[:8] + bytes((8, 3, 0, 0, 0))), (b"IDAT", b""), (b"IEND", b"")],
        )
        for chunks in structural:
            with (
                self.subTest(chunks=chunks),
                self.assertRaisesRegex(self.api["ContractViolationError"], "invalid_icon"),
            ):
                self.api["validate_icon_structure"](chunks)

    def test_archive_metadata_and_vector_failures_are_diagnostic(self) -> None:
        self.assert_stops(self.api["octal_field"], 8**8, 8)
        self.assert_stops(self.api["put"], bytearray(10), 0, 1, b"too long")
        records = self.api["canonical_records"](
            self.api["validate_entries"](self.api["expand_case"](self.case("minimum")), self.contract)[0],
            self.contract,
        )
        contract = copy.deepcopy(self.contract)
        contract["archive"]["metadata"]["file_typeflag"] = ""
        file_record = next(record for record in records if not record.is_directory)
        self.assert_stops(self.api["build_header"], file_record, contract)

        malformed_split = self.case("minimum")
        malformed_split["expected_ustar"] = {"path": 1, "prefix": "", "name": "x"}
        self.assert_stops(self.api["check_expected_split"], malformed_split, {})
        wrong_split = self.case("minimum")
        wrong_split["expected_ustar"] = {"path": "icon.png", "prefix": "bad", "name": "icon.png"}
        self.assert_stops(self.api["check_expected_split"], wrong_split, {"icon.png": ("", "icon.png")})

        wrong_digest = self.case("minimum")
        wrong_digest["sha256"] = "0" * 64
        self.assert_stops(self.api["verify_valid_case"], wrong_digest, self.contract)
        wrong_error = self.case("absolute_path")
        wrong_error["error"] = "traversal"
        codes = set(self.contract["rejection_codes"])
        self.assert_stops(self.api["verify_invalid_case"], wrong_error, self.contract, codes)
        accepted = self.case("minimum")
        accepted.update(valid=False, error="absolute_path")
        self.assert_stops(self.api["verify_invalid_case"], accepted, self.contract, codes)

    def test_vector_document_validation_is_closed(self) -> None:
        invalid = (
            ({**self.contract, "version": 2}, self.vectors),
            ({**self.contract, "rejection_codes": [1]}, self.vectors),
            (self.contract, {**self.vectors, "cases": [{"name": "", "valid": True}]}),
            (
                self.contract,
                {**self.vectors, "cases": [{"name": "one", "valid": True}, {"name": "one", "valid": True}]},
            ),
        )
        for contract, vectors in invalid:
            with self.subTest(vectors=vectors):
                self.assert_stops(self.api["verify_vectors"], contract, vectors)
        unknown = self.case("absolute_path")
        unknown["error"] = "unknown"
        self.assert_stops(
            self.api["verify_invalid_case"],
            unknown,
            self.contract,
            set(self.contract["rejection_codes"]),
        )

    def test_manifest_verification_and_sync_reject_unsafe_filesystems(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = tuple(self.api["AUTHORITY_FILES"])
            for name in files:
                (root / name).write_bytes(name.encode())

            def write_manifest(rows: list[str]) -> None:
                (root / self.api["MANIFEST"]).write_text("\n".join(rows) + "\n", encoding="ascii")

            rows = [f"{'0' * 64}  {name}" for name in sorted(files)]
            with mock.patch.dict(self.module_globals, {"HERE": root}):
                self.assert_stops(self.api["manifest_rows"])
                write_manifest(["invalid"])
                self.assert_stops(self.api["manifest_rows"])
                write_manifest(rows[:-1])
                self.assert_stops(self.api["manifest_rows"])
                write_manifest(rows)
                self.assert_stops(self.api["verify_authority"])
                (root / files[0]).unlink()
                self.assert_stops(self.api["verify_authority"])
                self.assert_stops(self.api["sync_authority"], root)

            symlink = root / "link"
            symlink.symlink_to(root, target_is_directory=True)
            self.assert_stops(self.api["sync_authority"], symlink)
            target = root / "target"
            target.mkdir()
            (target / "unknown").write_text("x", encoding="ascii")
            self.assert_stops(self.api["sync_authority"], target)
            (target / "unknown").unlink()
            (target / "README.md").write_text("old", encoding="ascii")
            self.api["sync_authority"](target)

            destination = root / "destination-link"
            destination.symlink_to(root / files[0])
            fake_target = mock.MagicMock()
            fake_target.resolve.return_value = root / "other"
            fake_target.is_symlink.return_value = False
            fake_target.iterdir.return_value = iter(())
            fake_target.__truediv__.return_value = destination
            self.assert_stops(self.api["sync_authority"], fake_target)


if __name__ == "__main__":
    unittest.main()
