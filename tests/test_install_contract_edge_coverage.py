"""Exercise malformed schema and cross-field Assistant-install contracts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jsonschema.exceptions import SchemaError

from install import contract as install_contract


class InstallContractEdgeCoverageTests(unittest.TestCase):
    def test_schema_loader_rejects_unavailable_malformed_and_nonobject_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "schema.json")
            for raw in (None, b"\xff", b"{", b"[]"):
                with self.subTest(raw=raw):
                    path.unlink(missing_ok=True)
                    if raw is not None:
                        path.write_bytes(raw)
                    with self.assertRaises(RuntimeError):
                        install_contract._load_json(path)

    def test_validator_rejects_invalid_definitions_and_unknown_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / install_contract.DEFINITIONS).write_text('{"$defs":[]}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "definitions"):
                install_contract.ContractValidator(root)

        validator = install_contract.ContractValidator()
        with self.assertRaisesRegex(install_contract.ContractValidationError, "unknown_contract"):
            validator.validate("unknown.schema.json", {})

    def test_entry_point_reference_and_schema_shape_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry.json").write_text('{"$ref":"wrong"}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "entry point"):
                install_contract._build_validator(root, "entry.json", "entry", {})

            (root / "entry.json").write_text(
                json.dumps({"$ref": f"{install_contract.DEFINITIONS}#/$defs/entry"}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    install_contract.Draft202012Validator,
                    "check_schema",
                    side_effect=SchemaError("invalid"),
                ),
                self.assertRaisesRegex(RuntimeError, "schema is invalid"),
            ):
                install_contract._build_validator(root, "entry.json", "entry", {})

    def test_semantic_lifetimes_ignore_untyped_values_and_reject_invalid_bounds(self) -> None:
        install_contract._validate_semantics("delegation-claims.schema.json", [])
        for value in (
            {"iat": "1", "exp": 2},
            {"iat": 1, "exp": "2"},
        ):
            install_contract._validate_lifetime(value, "iat", "exp", 60, "lifetime")
        for value in ({"iat": 2, "exp": 2}, {"iat": 1, "exp": 62}):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(
                    install_contract.ContractValidationError,
                    "lifetime",
                ),
            ):
                install_contract._validate_lifetime(value, "iat", "exp", 60, "lifetime")

    def test_resolve_semantics_reject_digest_and_integration_mismatches(self) -> None:
        with self.assertRaisesRegex(install_contract.ContractValidationError, "digest_mismatch"):
            install_contract._validate_resolve({"oci_digest": "sha256:" + "a" * 64, "image_reference": "wrong"})

        base = {
            "oci_digest": "sha256:" + "a" * 64,
            "image_reference": "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "a" * 64,
        }
        install_contract._validate_resolve({**base, "integrations": None, "machine_contract": {}})
        self.assertEqual(install_contract._required_integration_ids({"actions": None}), set())

        duplicate = [{"id": "cloudflare"}, {"id": "cloudflare"}]
        contract = {"actions": [{"integrations": ["cloudflare"]}]}
        for intents in (duplicate, [{"id": "other"}]):
            with (
                self.subTest(intents=intents),
                self.assertRaisesRegex(
                    install_contract.ContractValidationError,
                    "integration_mismatch",
                ),
            ):
                install_contract._validate_resolve({**base, "integrations": intents, "machine_contract": contract})


if __name__ == "__main__":
    unittest.main()
