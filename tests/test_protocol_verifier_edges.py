"""Executable conformance coverage for vendored protocol verifiers."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import pathlib
import runpy
import shutil
import sys
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = ROOT / "protocol/account/authority/v1"
ASSISTANT = ROOT / "protocol/assistant/v1"
HTTP = ROOT / "protocol/http/v1"
INSTALL = ROOT / "protocol/install/v1"
SOURCE_PACKAGE = ROOT / "protocol/source-package/v1"


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
        shutil.copytree(source.parent, mirror)
        if mutate is not None:
            mutate(mirror)
        output = io.StringIO()
        module_names = (
            "human_request_validator",
            "payload",
            "progress",
            "schema_validator",
            "supervisor",
            "websocket",
        )

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


def _load_source_package_verifier() -> dict[str, object]:
    return runpy.run_path(str(SOURCE_PACKAGE / "verify.py"), run_name="source_package_verifier")


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


class AccountAuthorityVerifierEdgeTests(unittest.TestCase):
    def test_accepts_the_current_pinned_authority(self) -> None:
        self.assertIn("verified", _execute(ACCOUNT / "verify.py"))

    def test_rejects_inventory_digest_schema_and_vector_drift(self) -> None:
        mutations = (
            lambda root: (root / "contract-files.sha256").write_text("", encoding="ascii"),
            lambda root: (root / "README.md").write_text("drift", encoding="utf-8"),
            lambda root: _rewrite_json(
                root,
                "evaluation-request.schema.json",
                lambda value: value.update({"$schema": "draft"}),
            ),
            lambda root: _rewrite_json(
                root,
                "evaluation-response.schema.json",
                lambda value: value.update({"$id": "invalid"}),
            ),
            lambda root: _rewrite_json(root, "vectors.json", lambda value: value.update({"version": 2})),
            lambda root: _rewrite_json(
                root,
                "vectors.json",
                lambda value: value["vectors"][0].update({"binding": []}),
            ),
            lambda root: _rewrite_json(
                root,
                "vectors.json",
                lambda value: value["vectors"][0].update({"binding_digest": "0" * 64}),
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(ACCOUNT / "verify.py", mutate)


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

    def test_rejects_manifest_and_human_vector_drift(self) -> None:
        mutations = (
            lambda root: _rewrite_json(root, "manifest-vectors.json", lambda value: value.update({"version": 2})),
            lambda root: _rewrite_json(
                root,
                "manifest-vectors.json",
                lambda value: value["cases"][0].update({"name": ""}),
            ),
            lambda root: _rewrite_json(
                root,
                "manifest-vectors.json",
                lambda value: value.update({"cases": [case for case in value["cases"] if case["valid"]]}),
            ),
            lambda root: _rewrite_json(
                root,
                "machine-contract.schema.json",
                lambda value: value["$defs"]["humanRequestCapability"].update({"enum": None}),
            ),
            lambda root: _rewrite_json(
                root,
                "human-request-vectors.json",
                lambda value: value.update({"version": 2}),
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(ASSISTANT / "verify.py", mutate)


class TeamHttpVerifierEdgeTests(unittest.TestCase):
    def test_accepts_the_current_pinned_protocol(self) -> None:
        self.assertIn("golden vectors are valid", _execute(HTTP / "verify.py"))

    def test_rejects_manifest_inventory_digest_root_and_header_drift(self) -> None:
        def malformed_row(root: Path) -> None:
            manifest = root / "contract-files.sha256"
            manifest.write_text("invalid\n", encoding="ascii")

        def remove_row(root: Path) -> None:
            manifest = root / "contract-files.sha256"
            rows = manifest.read_text(encoding="ascii").splitlines()
            manifest.write_text("\n".join(rows[1:]) + "\n", encoding="ascii")

        mutations = (
            malformed_row,
            remove_row,
            lambda root: (root / "README.md").write_text("drift", encoding="utf-8"),
            lambda root: _rewrite_json(root, "vectors.json", lambda value: value.update({"version": 2})),
            lambda root: _rewrite_json(root, "vectors.json", lambda value: value.update({"headers": {}})),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(HTTP / "verify.py", mutate)

    def test_rejects_each_golden_vector_family_when_its_expected_outcome_drifts(self) -> None:
        def flip_case(section: str, *, valid: bool) -> object:
            def mutate(value: dict[str, object]) -> None:
                case = next(item for item in value[section] if item["valid"] is valid)
                case["valid"] = not valid

            return mutate

        mutations = (
            flip_case("frames", valid=True),
            flip_case("frames", valid=False),
            flip_case("human_response_frames", valid=True),
            flip_case("human_response_frames", valid=False),
            flip_case("chat_stream", valid=True),
            flip_case("chat_stream", valid=False),
            flip_case("chat_stream_lines", valid=True),
            flip_case("chat_stream_lines", valid=False),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(
                    HTTP / "verify.py", lambda root, mutation=mutate: _rewrite_json(root, "vectors.json", mutation)
                )

    def test_rejects_supervisor_and_identifier_vector_drift(self) -> None:
        def accepted_supervisor(value: dict[str, object]) -> None:
            value["local_supervisor"]["invalid"] = [value["local_supervisor"]["valid"][0]]

        def rejected_supervisor(value: dict[str, object]) -> None:
            value["local_supervisor"]["valid"] = [value["local_supervisor"]["invalid"][0]]

        def invalid_positive_identifier(value: dict[str, object]) -> None:
            value["identifiers"]["team"]["valid"] = ["Bad"]

        def valid_negative_identifier(value: dict[str, object]) -> None:
            value["identifiers"]["assistant"]["invalid"] = ["assistant"]

        for mutate in (
            accepted_supervisor,
            rejected_supervisor,
            invalid_positive_identifier,
            valid_negative_identifier,
        ):
            with self.subTest(mutate=mutate), self.assertRaises((SystemExit, ValueError)):
                _execute(
                    HTTP / "verify.py", lambda root, mutation=mutate: _rewrite_json(root, "vectors.json", mutation)
                )

    def test_rejects_a_positive_supervisor_vector_that_is_not_canonical(self) -> None:
        from protocol.http.v1 import supervisor

        fake = types.ModuleType("supervisor")
        fake.ASSERTION_HEADER = supervisor.ASSERTION_HEADER
        fake.SupervisorAssertionError = supervisor.SupervisorAssertionError
        fake.canonical_claims = lambda _value: {}
        with self.assertRaises(SystemExit):
            _execute(HTTP / "verify.py", modules={"supervisor": fake})

    def test_rejects_action_label_text_vector_drift(self) -> None:
        def wrong_exemplar(value: dict[str, object]) -> None:
            value["action_label_text"]["exemplars"][0]["canonical"] = "different"

        def admitted_invalid_exemplar(value: dict[str, object]) -> None:
            value["action_label_text"]["invalid_exemplars"] = ["Valid exemplar"]

        def rejected_label(value: dict[str, object]) -> None:
            value["action_label_text"]["labels"] = [" padded "]

        def admitted_invalid_label(value: dict[str, object]) -> None:
            value["action_label_text"]["invalid_labels"] = ["Valid label"]

        for mutate in (wrong_exemplar, admitted_invalid_exemplar, rejected_label, admitted_invalid_label):
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(
                    HTTP / "verify.py",
                    lambda root, mutation=mutate: _rewrite_json(root, "vectors.json", mutation),
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

    def test_semantic_validation_covers_lifetimes_digest_and_integrations(self) -> None:
        semantic_validation = self.api["semantic_validation"]
        semantic_validation("other", None)
        semantic_validation("delegation-claims.schema.json", {"iat": "bad", "exp": 2})
        semantic_validation("install-authorization-receipt.schema.json", {"issued_at": 1, "expires_at": "bad"})
        for name, value, code in (
            ("delegation-claims.schema.json", {"iat": 1, "exp": 62}, "delegation_lifetime"),
            (
                "install-authorization-receipt.schema.json",
                {"issued_at": 2, "expires_at": 1},
                "authorization_lifetime",
            ),
            (
                "resolve-response.schema.json",
                {"oci_digest": "digest", "image_reference": "wrong"},
                "resolve_digest_mismatch",
            ),
        ):
            with self.subTest(name=name), self.assertRaises(self.api["ContractViolationError"]) as caught:
                semantic_validation(name, value)
            self.assertEqual(caught.exception.code, code)

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
        documents = {f"{self.api['SCHEMA_ORIGIN']}install-request.schema.json": {}}
        fixture = {"fixture": {"schema": "install-request.schema.json", "value": {}}}
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
