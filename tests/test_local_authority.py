"""Local Supervisor assertion signature, binding, replay, and key-custody contracts."""

import base64
import hashlib
import os
import stat
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
        "authority": "session",
        "authority_sha256": hashlib.sha256(b"local-session").hexdigest(),
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


def _binding(**overrides: object) -> authority.RequestBinding:
    value: dict[str, object] = {
        "method": "GET",
        "path": "/v1/teams",
        "body": BODY,
        "model": None,
        "assurance": None,
        "authority_kinds": frozenset({"session"}),
    }
    value.update(overrides)
    return authority.RequestBinding(**value)


class LocalSupervisorAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key_path = Path(self.temporary.name) / "public.pem"
        self._write_public_key()
        self.patches = (
            mock.patch.object(authority, "PUBLIC_KEY_FILE", self.public_key_path),
            mock.patch.object(authority, "VERIFIER_FILE", Path(self.temporary.name) / "team" / "supervisor.pem"),
            mock.patch.object(
                authority.grp,
                "getgrnam",
                return_value=types.SimpleNamespace(gr_gid=os.getgid()),
            ),
        )
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _write_public_key(self) -> None:
        """Publish the Supervisor public key as the group-readable PEM the authority accepts."""
        self.public_key_path.write_bytes(
            self.private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        )
        self.public_key_path.chmod(0o440)

    def _headers(self, claims: dict[str, object]) -> Message:
        headers = Message()
        headers[contract.ASSERTION_HEADER] = f"Bearer {_assertion(self.private_key, claims)}"
        return headers

    def test_accepts_one_exact_assertion_then_rejects_replay(self) -> None:
        guard = authority.ReplayGuard(capacity=2)
        headers = self._headers(_claims())

        evidence = authority.verify(
            headers,
            request=_binding(),
            replay_guard=guard,
            now=NOW,
        )

        self.assertEqual(evidence.supervisor_id, "a" * 32)
        self.assertEqual(evidence.authority_kind, "session")
        self.assertEqual(evidence.authority_digest, hashlib.sha256(b"local-session").hexdigest())
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "replayed"):
            authority.verify(
                headers,
                request=_binding(),
                replay_guard=guard,
                now=NOW,
            )

    def _verify(self, key: Ed25519PrivateKey, jti: str) -> authority.Evidence:
        headers = Message()
        headers[contract.ASSERTION_HEADER] = f"Bearer {_assertion(key, _claims(jti=jti))}"
        return authority.verify(headers, request=_binding(), replay_guard=authority.ReplayGuard(), now=NOW)

    def _rotation(self, key: Ed25519PrivateKey) -> dict[str, str]:
        return {"public_key": _segment(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))}

    def test_team_pins_the_first_published_key_and_ignores_a_replaced_file(self) -> None:
        evidence = self._verify(self.private_key, "1" * 32)
        self.assertEqual(evidence.key_sha256, authority.key_sha256(self.private_key.public_key()))
        self.assertEqual(stat.S_IMODE(authority.VERIFIER_FILE.stat().st_mode), 0o600)
        # Whoever can rewrite Admin's published file cannot make Team accept another key.
        intruder = Ed25519PrivateKey.generate()
        self.public_key_path.chmod(0o640)
        self.public_key_path.write_bytes(
            intruder.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        )
        self.public_key_path.chmod(0o440)
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "signature is invalid"):
            self._verify(intruder, "2" * 32)
        self.assertEqual(self._verify(self.private_key, "3" * 32).key_sha256, evidence.key_sha256)

    def test_a_rotation_signed_by_the_pinned_key_switches_it_once_and_is_idempotent(self) -> None:
        old = self._verify(self.private_key, "1" * 32).key_sha256
        new_key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
        expected = {"rotated": True, "key_sha256": authority.key_sha256(new_key.public_key())}
        self.assertEqual(authority.rotate_supervisor_key(self._rotation(new_key), old), expected)
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "signature is invalid"):
            self._verify(self.private_key, "2" * 32)
        current = self._verify(new_key, "3" * 32).key_sha256
        # A retry of the rotation that took effect succeeds whichever key signed it.
        self.assertEqual(authority.rotate_supervisor_key(self._rotation(new_key), old), expected)
        self.assertEqual(authority.rotate_supervisor_key(self._rotation(new_key), current), expected)
        # A rotation the earlier key signed can no longer switch the pin to any other key.
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "no longer current"):
            authority.rotate_supervisor_key(self._rotation(other), old)
        self.assertEqual(self._verify(new_key, "4" * 32).key_sha256, current)

    def test_a_rotation_fails_closed_without_changing_the_pin(self) -> None:
        current = self._verify(self.private_key, "1" * 32).key_sha256
        pinned = authority.VERIFIER_FILE.read_bytes()
        new_key = Ed25519PrivateKey.generate()
        for body in ({}, {"public_key": "A" * 42}, {"public_key": "A" * 43, "extra": 1}, None):
            with self.subTest(body=body), self.assertRaisesRegex(authority.SupervisorDeniedError, "invalid"):
                authority.rotate_supervisor_key(body, current)
        with (
            mock.patch.object(authority.Ed25519PublicKey, "from_public_bytes", side_effect=ValueError("point")),
            self.assertRaisesRegex(authority.SupervisorDeniedError, "invalid"),
        ):
            authority.rotate_supervisor_key(self._rotation(new_key), current)
        with (
            mock.patch.object(authority.private_state, "replace_durably", side_effect=OSError("full")),
            self.assertRaisesRegex(authority.SupervisorUnavailableError, "could not be saved"),
        ):
            authority.rotate_supervisor_key(self._rotation(new_key), current)
        self.assertEqual(authority.VERIFIER_FILE.read_bytes(), pinned)
        authority.forget_supervisor_key()
        self.assertFalse(authority.VERIFIER_FILE.exists())
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "unavailable"):
            authority.rotate_supervisor_key(self._rotation(new_key), current)

    def test_the_pin_and_its_removal_fail_closed(self) -> None:
        self._verify(self.private_key, "1" * 32)
        with (
            mock.patch.object(authority.os, "read", side_effect=OSError("io")),
            self.assertRaisesRegex(authority.SupervisorUnavailableError, "unavailable"),
        ):
            authority.supervisor_key()
        with (
            mock.patch.object(authority, "VERIFIER_FILE", self.public_key_path / "pin"),
            self.assertRaisesRegex(authority.SupervisorUnavailableError, "unavailable"),
        ):
            authority.supervisor_key()
        with (
            mock.patch.object(authority.private_state, "fsync_directory", side_effect=OSError("io")),
            self.assertRaisesRegex(authority.SupervisorUnavailableError, "could not be removed"),
        ):
            authority.forget_supervisor_key()
        with mock.patch.object(authority, "VERIFIER_FILE", Path(self.temporary.name) / "absent" / "pin"):
            authority.forget_supervisor_key()

    def test_json_body_binding_accepts_the_chat_transport_boundary_only(self) -> None:
        body = {
            "kind": "json",
            "length": contract.MAX_JSON_BODY_BYTES,
            "sha256": "d" * 64,
        }
        self.assertEqual(contract.canonical_claims(_claims(body=body))["body"], body)
        with self.assertRaisesRegex(contract.SupervisorAssertionError, "invalid JSON assertion body"):
            contract.canonical_claims(_claims(body={**body, "length": contract.MAX_JSON_BODY_BYTES + 1}))

    def test_mismatch_and_invalid_signature_do_not_consume_nonce(self) -> None:
        guard = authority.ReplayGuard(capacity=1)
        claims = _claims()
        headers = self._headers(claims)
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "does not match"):
            authority.verify(
                headers,
                request=_binding(path="/v1/assistants"),
                replay_guard=guard,
                now=NOW,
            )
        forged_key = Ed25519PrivateKey.generate()
        forged = Message()
        forged[contract.ASSERTION_HEADER] = f"Bearer {_assertion(forged_key, {**claims, 'jti': 'c' * 32})}"
        with self.assertRaises(authority.SupervisorDeniedError):
            authority.verify(
                forged,
                request=_binding(),
                replay_guard=guard,
                now=NOW,
            )

        evidence = authority.verify(
            headers,
            request=_binding(),
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
                request=_binding(),
                replay_guard=authority.ReplayGuard(),
                now=NOW,
            )
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "valid time"):
            authority.verify(
                self._headers({**claims, "jti": "e" * 32}),
                request=_binding(model=model),
                replay_guard=authority.ReplayGuard(),
                now=NOW + contract.ASSERTION_MAX_TTL_SECONDS + 1,
            )

    def test_a_decision_key_is_admitted_only_with_its_exact_bound_digest(self) -> None:
        decision = {"provider": "typesafe", "key_sha256": "f" * 64}
        claims = _claims(decision=decision)
        evidence = authority.verify(
            self._headers(claims),
            request=_binding(decision=decision),
            replay_guard=authority.ReplayGuard(),
            now=NOW,
        )
        self.assertEqual(evidence.supervisor_id, "a" * 32)
        for request, signed in (
            (_binding(), {**claims, "jti": "c" * 32}),
            (_binding(decision={"provider": "typesafe", "key_sha256": "e" * 64}), {**claims, "jti": "d" * 32}),
            (_binding(decision=decision), _claims(jti="e" * 32)),
        ):
            with (
                self.subTest(request=request.decision),
                self.assertRaisesRegex(authority.SupervisorDeniedError, "does not match"),
            ):
                authority.verify(self._headers(signed), request=request, replay_guard=authority.ReplayGuard(), now=NOW)

    def test_host_reset_authority_is_admitted_only_by_the_explicit_reset_binding(self) -> None:
        claims = _claims(authority="host-reset")
        headers = self._headers(claims)

        with self.assertRaisesRegex(authority.SupervisorDeniedError, "does not match"):
            authority.verify(
                headers,
                request=_binding(),
                replay_guard=authority.ReplayGuard(),
                now=NOW,
            )

        evidence = authority.verify(
            headers,
            request=_binding(authority_kinds=frozenset({"session", "host-reset"})),
            replay_guard=authority.ReplayGuard(),
            now=NOW,
        )
        self.assertEqual(evidence.authority_kind, "host-reset")

    def test_replay_capacity_and_public_key_metadata_fail_closed(self) -> None:
        guard = authority.ReplayGuard(capacity=1)
        authority.verify(
            self._headers(_claims()),
            request=_binding(),
            replay_guard=guard,
            now=NOW,
        )
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "saturated"):
            authority.verify(
                self._headers(_claims(jti="f" * 32)),
                request=_binding(),
                replay_guard=guard,
                now=NOW,
            )
        # Team's pin of the key fails closed on its own metadata.
        authority.VERIFIER_FILE.chmod(0o644)
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "unsafe metadata"):
            authority.verify(
                self._headers(_claims(jti="1" * 32)),
                request=_binding(),
                replay_guard=authority.ReplayGuard(),
                now=NOW,
            )
        # Before Team pins a key, Admin's published key fails closed on its metadata.
        authority.VERIFIER_FILE.unlink()
        self.public_key_path.chmod(0o400)
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "unavailable"):
            authority.verify(
                self._headers(_claims(jti="1" * 32)),
                request=_binding(),
                replay_guard=authority.ReplayGuard(),
                now=NOW,
            )
        self.assertFalse(authority.VERIFIER_FILE.exists())

    def test_human_assurance_is_required_exactly_when_signed(self) -> None:
        assurance = {
            "kind": "auth:password",
            "challenge_id": "2" * 32,
        }
        claims = _claims(assurance=assurance)
        headers = self._headers(claims)
        guard = authority.ReplayGuard()

        with self.assertRaisesRegex(authority.SupervisorDeniedError, "does not match"):
            authority.verify(
                headers,
                request=_binding(),
                replay_guard=guard,
                now=NOW,
            )

        evidence = authority.verify(
            headers,
            request=_binding(assurance=assurance),
            replay_guard=guard,
            now=NOW,
        )
        self.assertEqual(evidence.assertion_id, "b" * 32)

    def test_replay_and_assertion_shapes_are_bounded(self) -> None:
        for capacity in (0, authority.MAX_REPLAY_ENTRIES + 1):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                authority.ReplayGuard(capacity=capacity)

        headers = Message()
        headers[contract.ASSERTION_HEADER] = "Bearer "
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "invalid"):
            authority._one_assertion(headers)

    def test_public_key_loading_maps_io_truncation_and_invalid_key_types(self) -> None:
        with (
            mock.patch.object(authority.os, "open", side_effect=OSError("denied")),
            self.assertRaisesRegex(authority.SupervisorUnavailableError, "unavailable"),
        ):
            authority._public_key()
        with (
            mock.patch.object(authority.os, "read", return_value=b""),
            self.assertRaisesRegex(authority.SupervisorUnavailableError, "unavailable"),
        ):
            authority._public_key()
        with mock.patch.object(authority.os, "close", side_effect=OSError("close failed")):
            self.assertIsInstance(authority._public_key(), authority.Ed25519PublicKey)

        self.public_key_path.chmod(0o600)
        self.public_key_path.write_bytes(b"not a public key")
        self.public_key_path.chmod(0o440)
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "key is invalid"):
            authority._public_key()
        with (
            mock.patch.object(authority, "load_pem_public_key", return_value=object()),
            self.assertRaisesRegex(authority.SupervisorUnavailableError, "key is invalid"),
        ):
            authority._public_key()

    def test_bootstrap_reset_requires_safe_proof_that_supervisor_is_absent(self) -> None:
        self.public_key_path.parent.chmod(0o2770)
        self.public_key_path.unlink()
        authority.require_supervisor_absent()

        self._write_public_key()
        with self.assertRaisesRegex(authority.SupervisorEstablishedError, "already established"):
            authority.require_supervisor_absent()

        self.public_key_path.chmod(0o600)
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "state is unavailable"):
            authority.require_supervisor_absent()

    def test_bootstrap_reset_rejects_unsafe_directory_and_key_entries(self) -> None:
        self.public_key_path.unlink()
        self.public_key_path.parent.chmod(0o700)
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "state is unavailable"):
            authority.require_supervisor_absent()

        self.public_key_path.parent.chmod(0o2770)
        self.public_key_path.symlink_to("missing.pem")
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "state is unavailable"):
            authority.require_supervisor_absent()

        self.public_key_path.unlink()
        self.public_key_path.write_bytes(b"not a public key")
        self.public_key_path.chmod(0o440)
        with self.assertRaisesRegex(authority.SupervisorUnavailableError, "key is invalid"):
            authority.require_supervisor_absent()

    def test_segments_and_claim_shapes_must_be_canonical(self) -> None:
        for encoded in ("é", "a=", "!", "AB"):
            with self.subTest(encoded=encoded), self.assertRaises(authority.SupervisorDeniedError):
                authority._decode_segment(encoded)

        invalid_json = _segment(b"not-json")
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "malformed"):
            authority._json_segment(invalid_json)
        with (
            mock.patch.object(
                authority.contract,
                "canonical_json",
                side_effect=contract.SupervisorAssertionError("invalid"),
            ),
            self.assertRaisesRegex(authority.SupervisorDeniedError, "malformed"),
        ):
            authority._json_segment(_segment(b"{}"))
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "not canonical"):
            authority._json_segment(_segment(b'{"b":1,"a":2}'))

    def test_verified_claims_rejects_malformed_header_and_claim_contract(self) -> None:
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "malformed"):
            authority._verified_claims("one.two", self.private_key.public_key())

        wrong_header = _segment(contract.canonical_json({"alg": "wrong", "typ": "JWT"}))
        valid_claims = _segment(contract.claims_json(_claims()))
        signature = _segment(b"signature")
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "header is invalid"):
            authority._verified_claims(f"{wrong_header}.{valid_claims}.{signature}", self.private_key.public_key())

        valid_header = _segment(contract.canonical_json(contract.JWT_HEADER))
        invalid_claims = _segment(contract.canonical_json({}))
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "claims are invalid"):
            authority._verified_claims(f"{valid_header}.{invalid_claims}.{signature}", self.private_key.public_key())

    def test_verified_request_rejects_non_string_assertion_id(self) -> None:
        headers = Message()
        headers[contract.ASSERTION_HEADER] = "Bearer encoded"
        claims = _claims(jti=1)
        with (
            mock.patch.object(authority, "_public_key", return_value=self.private_key.public_key()),
            mock.patch.object(authority, "_verified_claims", return_value=claims),
            self.assertRaisesRegex(authority.SupervisorDeniedError, "assertion is invalid"),
        ):
            authority.verify(headers, request=_binding(), replay_guard=authority.ReplayGuard(), now=NOW)


if __name__ == "__main__":
    unittest.main()


class LocalRoutineAuthorityTests(unittest.TestCase):
    """A Routine run is driven only by Admin's separate Routine identity, bound to one exact lease (ADR-0086)."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.routine_key = Ed25519PrivateKey.generate()
        self.supervisor_key = Ed25519PrivateKey.generate()
        directory = Path(self.temporary.name)
        for path, key in (
            (directory / "public.pem", self.supervisor_key),
            (directory / "routine.pem", self.routine_key),
        ):
            path.write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
            path.chmod(0o440)
        for patch in (
            mock.patch.object(authority, "PUBLIC_KEY_FILE", directory / "public.pem"),
            mock.patch.object(authority, "VERIFIER_FILE", directory / "team" / "supervisor.pem"),
            mock.patch.object(authority, "ROUTINE_PUBLIC_KEY_FILE", directory / "routine.pem"),
            mock.patch.object(authority.grp, "getgrnam", return_value=types.SimpleNamespace(gr_gid=os.getgid())),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.lease = hashlib.sha256(b"lease-token").hexdigest()
        self.path = "/v1/teams/team_1/routines/runs/" + "f" * 32

    def claims(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "aud": contract.ROUTINE_AUDIENCE,
            "authority": contract.ROUTINE_AUTHORITY,
            "authority_sha256": self.lease,
            "method": "POST",
            "path": self.path,
        }
        return _claims(**{**values, **overrides})

    def headers(self, claims: dict[str, object], key: Ed25519PrivateKey | None = None, *, header: str = "") -> Message:
        jwt = _segment(contract.canonical_json(contract.ROUTINE_JWT_HEADER))
        payload = _segment(contract.claims_json(claims, audience=claims["aud"]))
        signature = _segment((key or self.routine_key).sign(f"{jwt}.{payload}".encode("ascii")))
        headers = Message()
        headers[header or contract.ROUTINE_ASSERTION_HEADER] = f"Bearer {jwt}.{payload}.{signature}"
        return headers

    def binding(self) -> authority.RequestBinding:
        return _binding(method="POST", path=self.path, authority_kinds=frozenset({"session"}))

    def test_a_routine_assertion_yields_its_lease_and_key_once(self) -> None:
        guard = authority.ReplayGuard(capacity=2)
        headers = self.headers(self.claims())
        evidence = authority.verify_routine(headers, request=self.binding(), replay_guard=guard, now=NOW)
        raw = self.routine_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.assertEqual(evidence.key_fingerprint, hashlib.sha256(raw).hexdigest())
        self.assertEqual(evidence.key_fingerprint, authority.routine_key_fingerprint())
        self.assertEqual(evidence.lease_sha256, self.lease)
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "replayed"):
            authority.verify_routine(headers, request=self.binding(), replay_guard=guard, now=NOW)

    def test_human_and_routine_identities_never_substitute_for_each_other(self) -> None:
        guard = authority.ReplayGuard()
        signed_by_supervisor = self.headers(self.claims(), self.supervisor_key)
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "signature"):
            authority.verify_routine(signed_by_supervisor, request=self.binding(), replay_guard=guard, now=NOW)
        supervisor_headers = Message()
        supervisor_headers[contract.ASSERTION_HEADER] = f"Bearer {_assertion(self.supervisor_key, _claims())}"
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "required"):
            authority.verify_routine(supervisor_headers, request=self.binding(), replay_guard=guard, now=NOW)
        # A Routine assertion presented as a Supervisor assertion fails its header and audience.
        routine_as_human = self.headers(self.claims(jti="d" * 32), header=contract.ASSERTION_HEADER)
        with self.assertRaisesRegex(authority.SupervisorDeniedError, "header is invalid"):
            authority.verify(routine_as_human, request=_binding(), replay_guard=guard, now=NOW)

    def test_a_routine_assertion_is_bound_to_the_exact_request(self) -> None:
        guard = authority.ReplayGuard()
        for claims in (
            self.claims(jti="1" * 32, path="/v1/teams/team_1/chat"),
            self.claims(jti="2" * 32, iat=NOW - 60, exp=NOW - 50),
        ):
            with self.subTest(claims=claims), self.assertRaises(authority.SupervisorDeniedError):
                authority.verify_routine(self.headers(claims), request=self.binding(), replay_guard=guard, now=NOW)
        with self.assertRaises(contract.SupervisorAssertionError):
            contract.canonical_claims(
                self.claims(assurance={"kind": "auth:password", "challenge_id": "e" * 32}),
                audience=contract.ROUTINE_AUDIENCE,
            )
        with self.assertRaisesRegex(contract.SupervisorAssertionError, "unsupported"):
            contract.canonical_claims(self.claims(), audience="team-unsupported")

    def test_a_missing_routine_key_is_unavailable(self) -> None:
        (Path(self.temporary.name) / "routine.pem").unlink()
        with self.assertRaises(authority.SupervisorUnavailableError):
            authority.routine_key_fingerprint()
