"""Independent Assistant signature and provenance verification."""

from __future__ import annotations

import base64
import copy
import json
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from install import artifact_trust
from install.artifact_trust import (
    RELEASE_PROXY_URL,
    SIGNER_IDENTITY,
    ArtifactTrustError,
    ArtifactTrustVerifier,
)
from install.contract import CONTRACT_ROOT

VECTORS = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())
RESOLUTION = VECTORS["fixtures"]["resolve_response"]["value"]
AUTH = types.SimpleNamespace(
    docker_auth_config=lambda: {"username": "registry-reader", "password": "x" * 20},
    docker_config=lambda: nullcontext("/run/shimpz-test-registry"),
)


def _signature(resolution: dict[str, object]) -> list[dict[str, object]]:
    return [{"critical": {"image": {"docker-manifest-digest": resolution["oci_digest"]}}}]


def _attestation(resolution: dict[str, object]) -> list[dict[str, str]]:
    statement = {
        "_type": "https://in-toto.io/Statement/v0.1",
        "subject": [
            {
                "name": "ghcr.io/theshimpz/shimpz-assistant",
                "digest": {"sha256": resolution["oci_digest"].removeprefix("sha256:")},
            }
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://shimpz.com/build-types/assistant/v1",
                "externalParameters": {
                    "assistant_id": resolution["assistant_id"],
                    "version": resolution["assistant_version"],
                    "source_digest": resolution["source_digest"],
                    "manifest_digest": resolution["manifest_digest"],
                    "machine_contract_digest": resolution["machine_contract_digest"],
                },
            },
            "runDetails": {"builder": {"id": SIGNER_IDENTITY}},
        },
    }
    return {
        "payloadType": "application/vnd.in-toto+json",
        "payload": base64.b64encode(json.dumps(statement).encode()).decode(),
    }


class ArtifactTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._trust_root = Path(self._temporary_directory.name) / "trust"

    def _verifier(self, resolution: dict[str, object]) -> ArtifactTrustVerifier:
        digests = iter(
            (
                resolution["trust"]["signature_reference"].removeprefix("ghcr.io/theshimpz/shimpz-assistant-trust@"),
                resolution["trust"]["provenance_reference"].removeprefix("ghcr.io/theshimpz/shimpz-assistant-trust@"),
            )
        )
        images = types.SimpleNamespace(
            get_registry_data=lambda _tag, *, auth_config: types.SimpleNamespace(id=next(digests))
        )
        return ArtifactTrustVerifier(
            types.SimpleNamespace(images=images),
            container_id="a" * 64,
            credentials=AUTH,
            trust_root=self._trust_root,
        )

    def test_reuses_private_tuf_cache_but_not_docker_configuration(self) -> None:
        docker_configs: list[str] = []

        class Credentials:
            def docker_config(self) -> object:
                directory = tempfile.TemporaryDirectory()
                docker_configs.append(directory.name)
                return directory

        api = mock.Mock()
        api.exec_create.side_effect = ({"Id": "first"}, {"Id": "second"})
        api.exec_start.return_value = iter(())
        api.exec_inspect.return_value = {"ExitCode": 0}
        verifier = ArtifactTrustVerifier(
            types.SimpleNamespace(api=api),
            container_id="a" * 64,
            credentials=Credentials(),
            trust_root=self._trust_root,
        )

        verifier._run_cosign(("verify", "first"))
        api.exec_start.return_value = iter(())
        verifier._run_cosign(("verify", "second"))

        environments = [call.kwargs["environment"] for call in api.exec_create.call_args_list]
        tuf_roots = [
            next(value for value in environment if value.startswith("TUF_ROOT=")) for environment in environments
        ]
        docker_roots = [
            next(value for value in environment if value.startswith("DOCKER_CONFIG=")) for environment in environments
        ]
        homes = [next(value for value in environment if value.startswith("HOME=")) for environment in environments]
        expected_proxy = {
            f"HTTPS_PROXY={RELEASE_PROXY_URL}",
            f"https_proxy={RELEASE_PROXY_URL}",
            f"HTTP_PROXY={RELEASE_PROXY_URL}",
            f"http_proxy={RELEASE_PROXY_URL}",
            "NO_PROXY=",
            "no_proxy=",
        }
        self.assertEqual(tuf_roots, [f"TUF_ROOT={self._trust_root}"] * 2)
        self.assertNotEqual(docker_roots[0], docker_roots[1])
        self.assertNotEqual(homes[0], homes[1])
        self.assertTrue(
            all(
                {value for value in environment if "_PROXY=" in value or "_proxy=" in value} == expected_proxy
                for environment in environments
            )
        )
        self.assertEqual(self._trust_root.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(not Path(path).exists() for path in docker_configs))
        self.assertTrue(all(not Path(value.removeprefix("HOME=")).exists() for value in homes))

    def test_cosign_has_an_output_independent_process_deadline(self) -> None:
        api = mock.Mock()
        api.exec_create.return_value = {"Id": "timed-out"}
        api.exec_start.return_value = iter(())
        api.exec_inspect.return_value = {"ExitCode": 124}
        verifier = ArtifactTrustVerifier(
            types.SimpleNamespace(api=api),
            container_id="a" * 64,
            credentials=AUTH,
            trust_root=self._trust_root,
        )

        with self.assertRaisesRegex(ArtifactTrustError, "timed out"):
            verifier._run_cosign(("verify", "image"))

        self.assertEqual(
            api.exec_create.call_args.kwargs["cmd"],
            [
                "/usr/bin/timeout",
                "--kill-after=5s",
                "90s",
                "/usr/local/bin/cosign",
                "verify",
                "image",
            ],
        )

    def test_rejects_a_non_private_tuf_cache(self) -> None:
        self._trust_root.mkdir(mode=0o755)

        with self.assertRaisesRegex(RuntimeError, "not a private"):
            ArtifactTrustVerifier(
                types.SimpleNamespace(),
                container_id="a" * 64,
                credentials=AUTH,
                trust_root=self._trust_root,
            )

    def test_accepts_signature_provenance_and_exact_attachment_digests(self) -> None:
        resolution = copy.deepcopy(RESOLUTION)
        resolution["trust"]["signer_identity"] = SIGNER_IDENTITY
        verifier = self._verifier(resolution)
        outputs = iter(
            (
                json.dumps(_signature(resolution)).encode(),
                json.dumps(_attestation(resolution)).encode(),
                b"ghcr.io/theshimpz/shimpz-assistant-trust:sha256-image.sig\n",
                b"ghcr.io/theshimpz/shimpz-assistant-trust:sha256-image.att\n",
            )
        )

        with mock.patch.object(verifier, "_run_cosign", side_effect=lambda _args: next(outputs)):
            verifier.verify(resolution)

    def test_rejects_each_mismatched_authority(self) -> None:
        base = copy.deepcopy(RESOLUTION)
        base["trust"]["signer_identity"] = SIGNER_IDENTITY
        cases = (
            ("identity", ("trust", "signer_identity"), "https://attacker.example/workflow"),
            ("source", ("source_digest",), "sha256:" + "9" * 64),
            (
                "signature-reference",
                ("trust", "signature_reference"),
                "ghcr.io/theshimpz/shimpz-assistant-trust@sha256:" + "9" * 64,
            ),
        )
        for name, path, value in cases:
            resolution = copy.deepcopy(base)
            target = resolution
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            verifier = self._verifier(base)
            outputs = iter(
                (
                    json.dumps(_signature(base)).encode(),
                    json.dumps(_attestation(base)).encode(),
                    b"ghcr.io/theshimpz/shimpz-assistant-trust:sha256-image.sig\n",
                    b"ghcr.io/theshimpz/shimpz-assistant-trust:sha256-image.att\n",
                )
            )
            with (
                self.subTest(name=name),
                mock.patch.object(
                    verifier,
                    "_run_cosign",
                    side_effect=lambda _args, iterator=outputs: next(iterator),
                ),
                self.assertRaises(ArtifactTrustError),
            ):
                verifier.verify(resolution)

    def test_attachment_and_cosign_decoders_reject_invalid_evidence(self) -> None:
        resolution = copy.deepcopy(RESOLUTION)
        verifier = self._verifier(resolution)
        with (
            mock.patch.object(verifier, "_cosign_text", return_value="attacker.example:tag"),
            self.assertRaisesRegex(ArtifactTrustError, "location is invalid"),
        ):
            verifier._verify_attachment("image", "expected", attestation=False)

        docker_client = types.SimpleNamespace(
            images=types.SimpleNamespace(get_registry_data=lambda *_args, **_kwargs: object())
        )
        unavailable = ArtifactTrustVerifier(
            docker_client,
            container_id="a" * 64,
            credentials=AUTH,
            trust_root=self._trust_root,
        )
        with (
            mock.patch.object(
                unavailable,
                "_cosign_text",
                return_value="ghcr.io/theshimpz/shimpz-assistant-trust:tag",
            ),
            self.assertRaisesRegex(ArtifactTrustError, "digest is unavailable"),
        ):
            unavailable._verify_attachment("image", "expected", attestation=False)

        with (
            mock.patch.object(verifier, "_run_cosign", return_value=b"not-json"),
            self.assertRaisesRegex(ArtifactTrustError, "invalid verification evidence"),
        ):
            verifier._cosign_json("verify")
        with (
            mock.patch.object(verifier, "_run_cosign", return_value=b"\xff"),
            self.assertRaisesRegex(ArtifactTrustError, "invalid attachment evidence"),
        ):
            verifier._cosign_text("triangulate")
        with (
            mock.patch.object(verifier, "_run_cosign", return_value=b" \n"),
            self.assertRaisesRegex(ArtifactTrustError, "empty attachment evidence"),
        ):
            verifier._cosign_text("triangulate")

    def test_cosign_stream_is_bounded_and_transport_failures_are_mapped(self) -> None:
        api = mock.Mock()
        api.exec_create.return_value = {"Id": "execution"}
        api.exec_start.return_value = iter(((b"verified", b"warning"),))
        api.exec_inspect.return_value = {"ExitCode": 0}
        verifier = ArtifactTrustVerifier(
            types.SimpleNamespace(api=api),
            container_id="a" * 64,
            credentials=AUTH,
            trust_root=self._trust_root,
        )
        self.assertEqual(verifier._run_cosign(("verify", "image")), b"verified")

        api.exec_start.return_value = iter(((b"still-running", None),))
        with (
            mock.patch.object(artifact_trust.time, "monotonic", side_effect=(0, 91)),
            self.assertRaisesRegex(ArtifactTrustError, "timed out"),
        ):
            verifier._run_cosign(("verify", "image"))

        api.exec_start.return_value = iter(((b"too-large", None),))
        with (
            mock.patch.object(artifact_trust, "_MAX_OUTPUT_BYTES", 1),
            self.assertRaisesRegex(ArtifactTrustError, "output is too large"),
        ):
            verifier._run_cosign(("verify", "image"))

        api.exec_start.return_value = iter(())
        api.exec_inspect.return_value = {"ExitCode": 1}
        with self.assertRaisesRegex(ArtifactTrustError, "verification failed"):
            verifier._run_cosign(("verify", "image"))

        api.exec_create.return_value = {}
        with self.assertRaisesRegex(ArtifactTrustError, "verification is unavailable"):
            verifier._run_cosign(("verify", "image"))

    def test_trust_cache_and_controller_identity_fail_closed(self) -> None:
        unavailable = Path(self._temporary_directory.name) / "unavailable"
        with (
            mock.patch.object(Path, "mkdir", side_effect=OSError("denied")),
            self.assertRaisesRegex(RuntimeError, "cache is unavailable"),
        ):
            artifact_trust._ensure_private_directory(unavailable)

        with (
            mock.patch.object(Path, "read_text", side_effect=OSError("denied")),
            self.assertRaisesRegex(RuntimeError, "identity is unavailable"),
        ):
            artifact_trust._self_container_id()
        with (
            mock.patch.object(Path, "read_text", return_value="not-a-container"),
            self.assertRaisesRegex(RuntimeError, "identity is invalid"),
        ):
            artifact_trust._self_container_id()
        with mock.patch.object(Path, "read_text", return_value="a" * 12):
            self.assertEqual(artifact_trust._self_container_id(), "a" * 12)

    def test_signature_and_provenance_shape_helpers_reject_malformed_records(self) -> None:
        resolution = copy.deepcopy(RESOLUTION)
        with self.assertRaisesRegex(ArtifactTrustError, "does not bind"):
            artifact_trust._verify_signature_payload({}, resolution["oci_digest"])
        for record in (None, {}, {"critical": []}, {"critical": {"image": []}}):
            with self.subTest(record=record):
                self.assertIsNone(artifact_trust._signature_digest(record))

        with self.assertRaisesRegex(ArtifactTrustError, "provenance does not match"):
            artifact_trust._verify_provenance([], resolution)
        self.assertFalse(artifact_trust._provenance_matches(None, resolution))
        self.assertFalse(artifact_trust._provenance_matches({}, resolution))
        self.assertFalse(
            artifact_trust._provenance_matches(
                {"payloadType": "application/vnd.in-toto+json", "payload": 1},
                resolution,
            )
        )
        invalid_payload = base64.b64encode(b"not-json").decode()
        self.assertFalse(
            artifact_trust._provenance_matches(
                {"payloadType": "application/vnd.in-toto+json", "payload": invalid_payload},
                resolution,
            )
        )
        self.assertIsNone(artifact_trust._decode_statement("not-base64"))
        self.assertFalse(artifact_trust._predicate_matches({}, resolution))


if __name__ == "__main__":
    unittest.main()
