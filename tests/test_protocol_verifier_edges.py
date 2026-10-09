"""Executable conformance coverage for vendored protocol verifiers."""

import contextlib
import hashlib
import importlib
import io
import json
import os
import pathlib
import runpy
import shutil
import sys
import tempfile
import threading
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ASSISTANT = ROOT / "protocol/assistant/v1"
HTTP = ROOT / "protocol/http/v1"
INSTALL = ROOT / "protocol/install/v1"


@contextlib.contextmanager
def _fresh_modules(*names: str):
    saved = {name: sys.modules.pop(name) for name in names if name in sys.modules}
    try:
        yield
    finally:
        for name in names:
            sys.modules.pop(name, None)
        sys.modules.update(saved)


def _execute(
    source: Path,
    mutate=None,
    *,
    modules: dict[str, object] | None = None,
    run_name: str = "protocol_verifier",
) -> str:
    with tempfile.TemporaryDirectory() as temporary:
        mirror = Path(temporary) / source.parent.name
        # Bytecode left beside an imported mirror module is not part of the pinned tree.
        shutil.copytree(source.parent, mirror, ignore=shutil.ignore_patterns("__pycache__"))
        if mutate is not None:
            mutate(mirror)
        output = io.StringIO()
        module_names = (
            "identifiers",
            "payload",
            "phrase",
            "progress",
            "purpose",
            "routine",
            "routine_context",
            "routine_notice",
            "routine_proposal",
            "routine_run",
            "schema_validator",
            "strict_json",
            "supervisor",
            "turn",
            "validators",
            "validators.action_effect",
            "validators.failure",
            "validators.human_request",
            "validators.input_file",
            "validators.message_catalog",
            "websocket",
        )

        # zoneinfo loads importlib.resources, which must not first import under the patched pathlib.Path.
        importlib.import_module("importlib.resources")

        def redirected_path(value) -> Path:
            path = Path(value)
            return mirror / source.name if path.resolve() == source.resolve() else path

        with (
            _fresh_modules(*module_names),
            mock.patch.object(sys, "path", [str(mirror), *sys.path]),
            mock.patch.dict(sys.modules, modules or {}),
            mock.patch.object(pathlib, "Path", redirected_path),
            contextlib.redirect_stdout(output),
        ):
            runpy.run_path(str(source), run_name=run_name)
        return output.getvalue()


def _load_install_verifier() -> dict[str, object]:
    with (
        _fresh_modules("schema_validator"),
        mock.patch.object(sys, "path", [str(INSTALL), *sys.path]),
    ):
        return runpy.run_path(str(INSTALL / "verify.py"), run_name="install_protocol_verifier")


def _rewrite_json(root: Path, filename: str, mutate) -> None:
    path = root / filename
    value = json.loads(path.read_bytes())
    mutate(value)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    _rehash(root, filename)


def _rehash(root: Path, filename: str) -> None:
    manifest = root / "contract-files.sha256"
    digest = hashlib.sha256((root / filename).read_bytes()).hexdigest()
    rows = manifest.read_text(encoding="ascii").splitlines()
    manifest.write_text(
        "\n".join(f"{digest}  {filename}" if row.endswith(f"  {filename}") else row for row in rows) + "\n",
        encoding="ascii",
    )


def _png_chunk(kind: bytes, data: bytes = b"") -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + kind + data + checksum.to_bytes(4, "big")


class AssistantVerifierEdgeTests(unittest.TestCase):
    def test_accepts_the_current_pinned_protocol(self) -> None:
        self.assertIn("conformance vectors are valid", _execute(ASSISTANT / "verify.py"))

    def test_rejects_manifest_inventory_digest_and_schema_drift(self) -> None:
        def duplicate_row(root: Path) -> None:
            manifest = root / "contract-files.sha256"
            first = manifest.read_text(encoding="ascii").splitlines()[0]
            manifest.write_text(f"{first}\n{first}\n", encoding="ascii")

        def remove_row(root: Path) -> None:
            manifest = root / "contract-files.sha256"
            rows = manifest.read_text(encoding="ascii").splitlines()
            manifest.write_text("\n".join(rows[1:]) + "\n", encoding="ascii")

        mutations = (
            duplicate_row,
            remove_row,
            lambda root: (root / "README.md").write_text("drift", encoding="utf-8"),
            lambda root: _rewrite_json(
                root,
                "manifest.schema.json",
                lambda value: value.update({"$schema": "draft"}),
            ),
            lambda root: _rewrite_json(
                root,
                "result.schema.json",
                lambda value: value.update({"$id": "invalid"}),
            ),
            lambda root: _rewrite_json(
                root,
                "manifest.schema.json",
                lambda value: value["properties"]["stored_inputs"].update({"maxProperties": 7}),
            ),
            lambda root: _rewrite_json(
                root,
                "invocation.schema.json",
                lambda value: value["required"].remove("stored_inputs"),
            ),
            lambda root: _rewrite_json(
                root,
                "fetch.schema.json",
                lambda value: value["$defs"]["request"]["required"].remove("headers"),
            ),
            lambda root: _rewrite_json(
                root,
                "result.schema.json",
                lambda value: value.update(
                    {
                        "oneOf": [
                            envelope
                            for envelope in value["oneOf"]
                            if envelope["properties"]["type"]["const"] != "stored_input_rejected"
                        ]
                    }
                ),
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(ASSISTANT / "verify.py", mutate)

    def test_rejects_any_descendant_outside_the_pinned_layout(self) -> None:
        def nested(root: Path) -> None:
            (root / "validators/extra").mkdir()
            (root / "validators/extra/failure.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

        def bytecode(root: Path) -> None:
            (root / "validators/__pycache__").mkdir()
            (root / "validators/__pycache__/failure.cpython-314.pyc").write_bytes(b"\0")

        def linked_file(root: Path) -> None:
            target = root / "vectors/pattern.json"
            target.rename(root.parent / "pattern.json")
            target.symlink_to(root.parent / "pattern.json")

        def file_input_contract(root: Path) -> None:
            _rewrite_json(
                root,
                "machine-contract.schema.json",
                lambda value: value["$defs"]["action"]["properties"]["input_files"].update({"maxItems": 2}),
            )

        for mutate in (nested, bytecode, linked_file, lambda root: (root / "vectors/unlisted.json").write_text("{}")):
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit) as raised:
                _execute(ASSISTANT / "verify.py", mutate)
            self.assertRegex(str(raised.exception.code), "layout|artifact set")
        with self.assertRaises(SystemExit) as raised:
            _execute(ASSISTANT / "verify.py", file_input_contract)
        self.assertIn("file input contract", str(raised.exception.code))

        def manifest_fifo(root: Path) -> None:
            (root / "contract-files.sha256").unlink()
            os.mkfifo(root / "contract-files.sha256")

        regular = os.lstat(ASSISTANT / "README.md")
        real_lstat = os.lstat

        def swapped(root: Path) -> None:
            manifest = root / "contract-files.sha256"
            content = manifest.read_bytes()
            manifest.unlink()
            os.mkfifo(manifest)

            def write() -> None:
                with contextlib.suppress(OSError), manifest.open("wb") as fifo:
                    fifo.write(content)

            threading.Thread(target=write, daemon=True).start()

        def lstat(path, *args, **kwargs):
            # The manifest looked regular when lstat ran and became a FIFO with a ready writer before it was opened.
            return regular if str(path).endswith("contract-files.sha256") else real_lstat(path, *args, **kwargs)

        with mock.patch.object(os, "lstat", lstat), self.assertRaises(SystemExit) as raised:
            _execute(ASSISTANT / "verify.py", swapped)
        self.assertIn("unexpected entry: contract-files.sha256", str(raised.exception.code))

        for mutate, reason in (
            (manifest_fifo, "unexpected entry"),
            (lambda root: (root / "contract-files.sha256").unlink(), "unreadable"),
            (lambda root: (root / "contract-files.sha256").write_bytes(b"\xff"), "manifest is invalid"),
        ):
            with self.subTest(reason=reason), self.assertRaises(SystemExit) as raised:
                _execute(ASSISTANT / "verify.py", mutate)
            self.assertIn(reason, str(raised.exception.code))

    def test_rejects_manifest_and_human_vector_drift(self) -> None:
        mutations = (
            lambda root: _rewrite_json(root, "vectors/manifest.json", lambda value: value.update({"version": 2})),
            lambda root: _rewrite_json(
                root,
                "vectors/manifest.json",
                lambda value: value["cases"][0].update({"name": ""}),
            ),
            lambda root: _rewrite_json(
                root,
                "vectors/manifest.json",
                lambda value: value.update({"cases": [case for case in value["cases"] if case["valid"]]}),
            ),
            lambda root: _rewrite_json(
                root,
                "machine-contract.schema.json",
                lambda value: value["$defs"]["humanRequestCapability"].update({"enum": None}),
            ),
            lambda root: _rewrite_json(
                root,
                "vectors/human-request.json",
                lambda value: value.update({"version": 2}),
            ),
            lambda root: _rewrite_json(
                root,
                "vectors/human-request.json",
                lambda value: value["catalog"].update({"summary": "Not in the catalog."}),
            ),
            lambda root: _rewrite_json(
                root,
                "machine-contract.schema.json",
                lambda value: value["properties"]["messages"].update({"maxItems": 255}),
            ),
            lambda root: _rewrite_json(
                root,
                "language-pack.schema.json",
                lambda value: value["properties"]["locales"]["required"].append("en"),
            ),
            lambda root: _rewrite_json(root, "vectors/catalog.json", lambda value: value.update({"version": 2})),
            lambda root: _rewrite_json(
                root,
                "vectors/action-schema.json",
                lambda value: value["cases"][0].update({"schema": "closed"}),
            ),
            lambda root: _rewrite_json(
                root,
                "vectors/pattern.json",
                lambda value: value["cases"][0].update({"matches": "yes"}),
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(ASSISTANT / "verify.py", mutate)

    def test_rejects_effect_operation_id_and_failure_contract_drift(self) -> None:
        def flip_first(value: dict[str, object]) -> None:
            value["cases"][0]["valid"] = not value["cases"][0]["valid"]

        mutations = (
            lambda root: _rewrite_json(root, "vectors/action-effect.json", lambda value: value.update({"version": 2})),
            lambda root: _rewrite_json(
                root, "vectors/action-effect.json", lambda value: value["cases"][0].update({"name": ""})
            ),
            lambda root: _rewrite_json(root, "vectors/action-effect.json", flip_first),
            lambda root: _rewrite_json(
                root,
                "vectors/action-effect.json",
                lambda value: value.update({"cases": [case for case in value["cases"] if case["valid"]]}),
            ),
            lambda root: _rewrite_json(
                root,
                "machine-contract.schema.json",
                lambda value: value["$defs"]["action"]["required"].remove("effect"),
            ),
            lambda root: _rewrite_json(
                root,
                "invocation.schema.json",
                lambda value: value["required"].remove("operation_id"),
            ),
            lambda root: _rewrite_json(
                root,
                "result.schema.json",
                lambda value: value["$defs"]["failure"]["required"].remove("truncated"),
            ),
            lambda root: _rewrite_json(root, "vectors/failure.json", flip_first),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(ASSISTANT / "verify.py", mutate)

    def test_rejects_action_schema_vectors_that_do_not_pin_the_expanded_reference_bound(self) -> None:
        definitions: dict[str, object] = {"d0": {"type": "string"}}
        for level in range(1, 9):
            definitions[f"d{level}"] = {"allOf": [{"$ref": f"#/$defs/d{level - 1}"}] * 2}
        mutations = (
            *(
                lambda value, name=name: value.update(
                    {"cases": [case for case in value["cases"] if case["name"] != name]}
                )
                for name in (
                    "references expanding to exactly 4096 subschemas",
                    "references expanding to 4097 subschemas",
                )
            ),
            *(
                lambda value, reference=reference: value["cases"][0]["schema"].update({"$ref": reference})
                for reference in ("#", "#/$defs/missing")
            ),
            lambda value: value["cases"][0]["schema"].update(
                {"allOf": [{"$ref": "#/$defs/d8"}] * 4, "$defs": definitions}
            ),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaisesRegex(SystemExit, "expanded-reference bound"):
                _execute(
                    ASSISTANT / "verify.py",
                    lambda root, mutation=mutation: _rewrite_json(root, "vectors/action-schema.json", mutation),
                )


class AssistantInstallVerifierEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api = _load_install_verifier()
        cls.module_globals = cls.api["main"].__globals__

    def test_main_verifies_and_synchronizes_the_current_authority(self) -> None:
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
            target.mkdir()
            shutil.copyfile(INSTALL / "README.md", target / "README.md")
            output = io.StringIO()
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
            self.assertIn("synchronized", output.getvalue())

        with mock.patch.object(sys, "argv", ["verify.py"]):
            self.assertIn("golden vectors are valid", _execute(INSTALL / "verify.py", run_name="__main__"))

    def test_load_and_schema_documents_reject_malformed_authority(self) -> None:
        load_object = self.api["load_object"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "valid.json"
            valid.write_text('{"ok":true}', encoding="utf-8")
            self.assertEqual(load_object(valid), {"ok": True})
            invalid = root / "invalid.json"
            invalid.write_text("[]", encoding="utf-8")
            for path in (root / "missing.json", invalid):
                with self.subTest(path=path), self.assertRaises(SystemExit):
                    load_object(path)

        schema_documents = self.api["schema_documents"]
        valid_document = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{self.api['SCHEMA_ORIGIN']}{self.api['SCHEMAS'][0]}",
        }
        for document in (
            {**valid_document, "$schema": "draft"},
            {**valid_document, "$id": "invalid"},
        ):
            with (
                mock.patch.dict(self.module_globals, {"load_object": mock.Mock(return_value=document)}),
                self.assertRaises(SystemExit),
            ):
                schema_documents()

        def document_for(path: Path) -> dict[str, object]:
            return {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": f"{self.api['SCHEMA_ORIGIN']}{path.name}",
            }

        violation = self.api["SchemaViolationError"]("bad")
        with (
            mock.patch.dict(
                self.module_globals,
                {
                    "load_object": mock.Mock(side_effect=document_for),
                    "check_schema": mock.Mock(side_effect=violation),
                },
            ),
            self.assertRaises(SystemExit),
        ):
            schema_documents()

    def test_mutations_are_deep_closed_and_object_only(self) -> None:
        apply_mutation = self.api["apply_mutation"]
        original = {"nested": {"value": 1}}
        self.assertEqual(apply_mutation(original, None, "case"), original)
        self.assertEqual(
            apply_mutation(original, {"op": "set", "path": ["nested", "value"], "value": 2}, "case"),
            {"nested": {"value": 2}},
        )
        self.assertEqual(
            apply_mutation(original, {"op": "remove", "path": ["nested", "value"]}, "case"),
            {"nested": {}},
        )
        self.assertEqual(original, {"nested": {"value": 1}})
        invalid = (
            [],
            {"op": "bad", "path": ["nested"]},
            {"op": "set", "path": []},
            {"op": "set", "path": [0], "value": 1},
            {"op": "remove", "path": ["missing"]},
            {"op": "set", "path": ["nested"]},
            {"op": "set", "path": ["missing", "value"], "value": 1},
        )
        for mutation in invalid:
            with self.subTest(mutation=mutation), self.assertRaises(SystemExit):
                apply_mutation(original, mutation, "case")

    def test_semantic_validation_covers_digest_and_integrations(self) -> None:
        semantic_validation = self.api["semantic_validation"]
        semantic_validation("other", None)
        with self.assertRaises(self.api["ContractViolationError"]) as caught:
            semantic_validation("resolve-response.schema.json", {"oci_digest": "digest", "image_reference": "wrong"})
        self.assertEqual(caught.exception.code, "resolve_digest_mismatch")

        base = {
            "oci_digest": "digest",
            "image_reference": "ghcr.io/theshimpz/shimpz-assistant@digest",
        }
        semantic_validation("resolve-response.schema.json", base)
        semantic_validation("resolve-response.schema.json", {**base, "integrations": [], "machine_contract": {}})
        semantic_validation(
            "resolve-response.schema.json",
            {**base, "integrations": [], "machine_contract": {"actions": []}},
        )
        mismatch_cases = (
            {
                **base,
                "integrations": [{"id": "oauth"}, {"id": "oauth"}],
                "machine_contract": {"actions": []},
            },
            {
                **base,
                "integrations": [{"id": "oauth"}],
                "machine_contract": {"actions": []},
            },
        )
        for value in mismatch_cases:
            with self.assertRaises(self.api["ContractViolationError"]):
                semantic_validation("resolve-response.schema.json", value)

    def test_case_validation_maps_schema_semantic_and_fixture_failures(self) -> None:
        validate_case = self.api["validate_case"]
        documents = {f"{self.api['SCHEMA_ORIGIN']}resolve-response.schema.json": {}}
        fixture = {"fixture": {"schema": "resolve-response.schema.json", "value": {}}}
        case = {"name": "case", "fixture": "fixture"}
        for changed_case, changed_fixture in (
            ({**case, "fixture": "missing"}, fixture),
            (case, {"fixture": {"schema": "definitions.schema.json", "value": {}}}),
        ):
            with self.assertRaises(SystemExit):
                validate_case(changed_case, changed_fixture, documents)

        with mock.patch.dict(
            self.module_globals,
            {"validate": mock.Mock(side_effect=self.api["SchemaViolationError"]("bad"))},
        ):
            self.assertEqual(validate_case(case, fixture, documents), "schema_violation")
        with mock.patch.dict(
            self.module_globals,
            {"semantic_validation": mock.Mock(side_effect=self.api["ContractViolationError"]("semantic"))},
        ):
            self.assertEqual(validate_case(case, fixture, documents), "semantic")
        with mock.patch.dict(
            self.module_globals,
            {"validate": mock.Mock(), "semantic_validation": mock.Mock()},
        ):
            self.assertIsNone(validate_case(case, fixture, documents))

    def test_vector_envelope_cases_and_outcomes_are_closed(self) -> None:
        verify_vectors = self.api["verify_vectors"]
        documents: dict[str, dict[str, object]] = {}
        invalid_vectors = (
            {"version": 2, "fixtures": {}, "cases": []},
            {"version": 1, "fixtures": [], "cases": []},
            {"version": 1, "fixtures": {1: {}}, "cases": []},
        )
        for vectors in invalid_vectors:
            with (
                mock.patch.dict(self.module_globals, {"load_object": mock.Mock(return_value=vectors)}),
                self.assertRaises(SystemExit),
            ):
                verify_vectors(documents)

        verify_cases = self.api["verify_cases"]
        invalid_cases = (
            [None],
            [{"name": "", "fixture": "fixture", "valid": True}],
            [
                {"name": "duplicate", "fixture": "fixture", "valid": True},
                {"name": "duplicate", "fixture": "fixture", "valid": True},
            ],
            [{"name": "case", "fixture": "fixture", "valid": "yes"}],
        )
        for cases in invalid_cases:
            with (
                mock.patch.dict(self.module_globals, {"validate_case": mock.Mock(return_value=None)}),
                self.assertRaises(SystemExit),
            ):
                verify_cases(cases, {}, {})

    def test_manifest_authority_and_sync_fail_closed(self) -> None:
        manifest_rows = self.api["manifest_rows"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict(self.module_globals, {"HERE": root}), self.assertRaises(SystemExit):
                manifest_rows()
            (root / self.api["MANIFEST"]).write_text("invalid\n", encoding="ascii")
            with mock.patch.dict(self.module_globals, {"HERE": root}), self.assertRaises(SystemExit):
                manifest_rows()
            (root / self.api["MANIFEST"]).write_text(f"{'0' * 64}  only.py\n", encoding="ascii")
            with mock.patch.dict(self.module_globals, {"HERE": root}), self.assertRaises(SystemExit):
                manifest_rows()

        verify_authority = self.api["verify_authority"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing_rows = mock.Mock(return_value=[("missing", "0" * 64)])
            with (
                mock.patch.dict(self.module_globals, {"HERE": root, "manifest_rows": missing_rows}),
                self.assertRaises(SystemExit),
            ):
                verify_authority()
            file = root / "file"
            file.write_text("body", encoding="utf-8")
            with (
                mock.patch.dict(
                    self.module_globals,
                    {"HERE": root, "manifest_rows": mock.Mock(return_value=[("file", "0" * 64)])},
                ),
                self.assertRaises(SystemExit),
            ):
                verify_authority()

        sync_authority = self.api["sync_authority"]
        with self.assertRaises(SystemExit):
            sync_authority(self.api["HERE"])
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "unknown").write_text("x", encoding="utf-8")
            with self.assertRaises(SystemExit):
                sync_authority(target)

        target = mock.MagicMock()
        target.resolve.return_value = Path("different")
        target.is_symlink.return_value = False
        target.iterdir.return_value = []
        destination = mock.Mock()
        destination.is_symlink.return_value = True
        target.__truediv__.return_value = destination
        with self.assertRaises(SystemExit):
            sync_authority(target)

    def test_argument_parser_accepts_an_explicit_sync_target(self) -> None:
        with mock.patch.object(sys, "argv", ["verify.py", "--sync", "mirror"]):
            parsed = self.api["parse_args"]()
        self.assertEqual(parsed.sync, Path("mirror"))
