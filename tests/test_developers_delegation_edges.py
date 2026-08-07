from __future__ import annotations

import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hosted.install import developers_delegation as delegation
from install.contract import ContractValidationError


class DevelopersDelegationEdgeTests(unittest.TestCase):
    def test_verifier_rejects_contract_and_action_mismatch(self) -> None:
        verifier = object.__new__(delegation.DevelopersDelegationVerifier)
        verifier._service_token = "s" * 48
        verifier._public_key = Ed25519PrivateKey.generate().public_key()
        verifier._replay_guard = SimpleNamespace(consume=mock.Mock())
        claims = {
            "action": "teams:list",
            "jti": "jti",
            "iat": 1_000,
            "exp": 1_060,
        }
        headers = Message()
        with (
            mock.patch.object(delegation, "_verify_service_bearer"),
            mock.patch.object(delegation, "_one_bearer", return_value="token"),
            mock.patch.object(delegation, "_verify_jwt", return_value=claims),
            mock.patch.object(
                delegation._CONTRACTS,
                "validate",
                side_effect=ContractValidationError("invalid"),
            ),
            self.assertRaises(delegation.DevelopersDelegationError),
        ):
            verifier.verify(headers, action="teams:list", now=1_000)

        with (
            mock.patch.object(delegation, "_verify_service_bearer"),
            mock.patch.object(delegation, "_one_bearer", return_value="token"),
            mock.patch.object(delegation, "_verify_jwt", return_value=claims),
            mock.patch.object(delegation._CONTRACTS, "validate"),
            mock.patch.object(delegation, "_verify_time"),
            self.assertRaises(delegation.DevelopersDelegationError),
        ):
            verifier.verify(headers, action="assistant:install", now=1_000)

    def test_key_and_service_token_files_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing"
            with self.assertRaises(RuntimeError):
                delegation._read_service_token(missing)
            token = root / "token"
            token.write_text("short", encoding="ascii")
            with self.assertRaises(RuntimeError):
                delegation._read_service_token(token)
            with self.assertRaises(RuntimeError):
                delegation._read_public_key(missing)
            public = root / "public.pem"
            public.write_bytes(b"value")
            with (
                mock.patch.object(delegation, "load_pem_public_key", return_value=object()),
                self.assertRaises(RuntimeError),
            ):
                delegation._read_public_key(public)

    def test_bearer_and_jwt_shapes_reject_every_ambiguous_form(self) -> None:
        for value in (None, "Basic token", "Bearer ", "Bearer é", "Bearer a b", "Bearer " + "x" * 8193):
            headers = Message()
            if value is not None:
                headers["Authorization"] = value
            with self.subTest(value=value), self.assertRaises(delegation.DevelopersDelegationError):
                delegation._one_bearer(headers, "Authorization")

        key = Ed25519PrivateKey.generate().public_key()
        for encoded in ("one.two", "one..three"):
            with self.subTest(encoded=encoded), self.assertRaises(delegation.DevelopersDelegationError):
                delegation._verify_jwt(encoded, key)
        with (
            mock.patch.object(delegation, "_json_segment", side_effect=({"wrong": True}, {})),
            self.assertRaises(delegation.DevelopersDelegationError),
        ):
            delegation._verify_jwt("one.two.three", key)
        with (
            mock.patch.object(delegation, "_json_segment", side_effect=(delegation._HEADER, [])),
            self.assertRaises(delegation.DevelopersDelegationError),
        ):
            delegation._verify_jwt("one.two.three", key)

    def test_json_and_base64_segments_are_strictly_canonical(self) -> None:
        invalid_json = delegation._encode_segment(b"not-json")
        with self.assertRaises(delegation.DevelopersDelegationError):
            delegation._json_segment(invalid_json)

        noncanonical_json = delegation._encode_segment(json.dumps({"b": 1, "a": 2}).encode())
        with self.assertRaises(delegation.DevelopersDelegationError):
            delegation._json_segment(noncanonical_json)

        for encoded in ("é", "YQ==", "%", "Zh"):
            with self.subTest(encoded=encoded), self.assertRaises(delegation.DevelopersDelegationError):
                delegation._decode_segment(encoded)

    def test_request_binding_requires_the_action_specific_shape(self) -> None:
        with self.assertRaises(delegation.DevelopersDelegationError):
            delegation._verify_request_binding({"action": "teams:list"}, {})
        delegation._verify_request_binding({"action": "teams:list"}, None)
        with self.assertRaises(delegation.DevelopersDelegationError):
            delegation._verify_request_binding({"action": "assistant:install"}, None)


if __name__ == "__main__":
    unittest.main()
