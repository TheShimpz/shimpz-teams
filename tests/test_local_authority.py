"""Local Supervisor assertion signature, binding, replay, and key-custody contracts."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import types
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from local import authority
from protocol.http.v1 import supervisor as contract

NOW = 2_200_000_000
BODY = {
    "kind": "none",
    "length": 0,
    "sha256": contract.EMPTY_SHA256,
}


def _segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _claims(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "v": 1,
        "aud": contract.ASSERTION_AUDIENCE,
        "sub": "a" * 32,
        "session_sha256": hashlib.sha256(b"local-session").hexdigest(),
        "jti": "b" * 32,
        "iat": NOW,
        "exp": NOW + contract.ASSERTION_MAX_TTL_SECONDS,
        "method": "GET",
        "path": "/v1/teams",
        "body": BODY,
    }
    value.update(overrides)
    return value


def _assertion(private_key: Ed25519PrivateKey, claims: dict[str, object]) -> str:
    header = _segment(contract.canonical_json(contract.JWT_HEADER))
    payload = _segment(contract.claims_json(claims))
    signing_input = f"{header}.{payload}".encode("ascii")
    return f"{header}.{payload}.{_segment(private_key.sign(signing_input))}"


class LocalSupervisorAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key_path = Path(self.temporary.name) / "public.pem"
        self.public_key_path.write_bytes(
            self.private_key.public_key().public_bytes(
                Encoding.PEM,
                PublicFormat.SubjectPublicKeyInfo,
            )
        )
        self.public_key_path.chmod(0o440)
        self.patches = (
            mock.patch.object(authority, "PUBLIC_KEY_FILE", self.public_key_path),
            mock.patch.object(
                authority.grp,
                "getgrnam",
                return_value=types.SimpleNamespace(gr_gid=os.getgid()),
            ),
        )
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _headers(self, claims: dict[str, object]) -> Message:
        headers = Message()
        headers[contract.ASSERTION_HEADER] = f"Bearer {_assertion(self.private_key, claims)}"
        return headers

    def test_accepts_one_exact_assertion_then_rejects_replay(self) -> None:
        guard = authority.ReplayGuard(capacity=2)
        headers = self._headers(_claims())

        evidence = authority.verify(
            headers,
            method="GET",
            path="/v1/teams",
            body=BODY,
            model=None,
            replay_guard=guard,
            now=NOW,
        )

        self.assertEqual(evidence.supervisor_id, "a" * 32)
        self.assertEqual(evidence.session_digest, hashlib.sha256(b"local-session").hexdigest())
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "replayed"):
            authority.verify(
                headers,
                method="GET",
                path="/v1/teams",
                body=BODY,
                model=None,
                replay_guard=guard,
                now=NOW,
            )

    def test_mismatch_and_invalid_signature_do_not_consume_nonce(self) -> None:
        guard = authority.ReplayGuard(capacity=1)
        claims = _claims()
        headers = self._headers(claims)
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "does not match"):
            authority.verify(
                headers,
                method="GET",
                path="/v1/assistants",
                body=BODY,
                model=None,
                replay_guard=guard,
                now=NOW,
            )
        forged_key = Ed25519PrivateKey.generate()
        forged = Message()
        forged[contract.ASSERTION_HEADER] = f"Bearer {_assertion(forged_key, {**claims, 'jti': 'c' * 32})}"
        with self.assertRaises(authority.SupervisorDeniedError):
            authority.verify(
                forged,
                method="GET",
                path="/v1/teams",
                body=BODY,
                model=None,
                replay_guard=guard,
                now=NOW,
            )

        evidence = authority.verify(
            headers,
            method="GET",
            path="/v1/teams",
            body=BODY,
            model=None,
            replay_guard=guard,
            now=NOW,
        )
        self.assertEqual(evidence.assertion_id, "b" * 32)

    def test_model_binding_and_time_are_fail_closed(self) -> None:
        model = {"provider": "openai", "key_sha256": "d" * 64}
        claims = _claims(model=model)
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "does not match"):
            authority.verify(
                self._headers(claims),
                method="GET",
                path="/v1/teams",
                body=BODY,
                model=None,
                replay_guard=authority.ReplayGuard(),
                now=NOW,
            )
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "valid time"):
            authority.verify(
                self._headers({**claims, "jti": "e" * 32}),
                method="GET",
                path="/v1/teams",
                body=BODY,
                model=model,
                replay_guard=authority.ReplayGuard(),
                now=NOW + contract.ASSERTION_MAX_TTL_SECONDS + 1,
            )

    def test_replay_capacity_and_public_key_metadata_fail_closed(self) -> None:
        guard = authority.ReplayGuard(capacity=1)
        authority.verify(
            self._headers(_claims()),
            method="GET",
            path="/v1/teams",
            body=BODY,
            model=None,
            replay_guard=guard,
            now=NOW,
        )
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "saturated"):
            authority.verify(
                self._headers(_claims(jti="f" * 32)),
                method="GET",
                path="/v1/teams",
                body=BODY,
                model=None,
                replay_guard=guard,
                now=NOW,
            )
        self.public_key_path.chmod(0o400)
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "unavailable"):
            authority.verify(
                self._headers(_claims(jti="1" * 32)),
                method="GET",
                path="/v1/teams",
                body=BODY,
                model=None,
                replay_guard=authority.ReplayGuard(),
                now=NOW,
            )


if __name__ == "__main__":
    unittest.main()
