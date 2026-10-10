"""Independent Assistant signature and provenance verification."""

import base64
import copy
import hashlib
import json
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from install import artifact_trust
from install.artifact_trust import (
    PROVENANCE_PREDICATE,
    RELEASE_PROXY_URL,
    SIGNATURE_PREDICATE,
    SIGNER_IDENTITY,
    TRUST_REPOSITORY,
    ArtifactTrustError,
    ArtifactTrustVerifier,
)
from install.contract import CONTRACT_ROOT

VECTORS = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())
RESOLUTION = VECTORS["fixtures"]["resolve_response"]["value"]
AUTH = types.SimpleNamespace(docker_config=lambda: nullcontext("/run/shimpz-test-registry"))
BUNDLE = "application/vnd.dev.sigstore.bundle.v0.3+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
REGISTRY_BEARER = "test-token"


def _signature(resolution: dict[str, object]) -> list[dict[str, object]]:
    """Verification output lists the provenance bundle beside the signature bundle."""
    return [
        _claim(resolution["oci_digest"], PROVENANCE_PREDICATE),
        _claim(resolution["oci_digest"], SIGNATURE_PREDICATE),
    ]


def _claim(digest: str, claim_type: str) -> dict[str, object]:
    return {
        "critical": {
            "identity": {"docker-reference": f"ghcr.io/theshimpz/shimpz-assistant@{digest}"},
            "image": {"docker-manifest-digest": digest},
            "type": claim_type,
        },
        "optional": {},
    }


def _bundle_digest(reference: str) -> str:
    return reference.removeprefix(f"{TRUST_REPOSITORY}@")


class _Registry:
    """A trust repository holding the resolution's two bundle referrers, recording every request."""

    def __init__(self, resolution: dict[str, object]) -> None:
        oci_digest = resolution["oci_digest"]
        self.requests: list[tuple[str, str, str | None, str | None]] = []
        self.token: object = {"token": REGISTRY_BEARER}
        self.index: dict[str, object] = {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": []}
        self.manifests: dict[str, dict[str, object]] = {}
        for reference, predicate in (
            (resolution["trust"]["signature_reference"], SIGNATURE_PREDICATE),
            (resolution["trust"]["provenance_reference"], PROVENANCE_PREDICATE),
        ):
            digest = _bundle_digest(reference)
            self.index["manifests"].append(
                {"mediaType": OCI_MANIFEST, "digest": digest, "size": 876, "artifactType": BUNDLE}
            )
            self.manifests[digest] = {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "artifactType": BUNDLE,
                "annotations": {"dev.sigstore.bundle.predicateType": predicate},
                "subject": {"mediaType": OCI_INDEX, "digest": oci_digest, "size": 1547},
            }

    def __call__(self, path: str, *, accept: str, token: str | None = None, digest: str | None = None) -> object:
        self.requests.append((path, accept, token, digest))
        if path.startswith("/token?"):
            return self.token
        if "/manifests/sha256-" in path:
            return self.index
        return self.manifests[path.rsplit("/", 1)[1]]


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
                    "pack_digest": resolution["pack_digest"],
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

    def _verifier(self) -> ArtifactTrustVerifier:
        return ArtifactTrustVerifier(
            types.SimpleNamespace(),
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

    def _verify(self, resolution: dict[str, object], registry: _Registry, outputs: tuple[object, ...]) -> mock.Mock:
        verifier = self._verifier()
        evidence = iter(json.dumps(output).encode() for output in outputs)
        with (
            mock.patch.object(artifact_trust, "_registry_json", registry),
            mock.patch.object(verifier, "_run_cosign", side_effect=lambda _args: next(evidence)) as run_cosign,
        ):
            verifier.verify(resolution)
        return run_cosign

    def _trusted(self) -> dict[str, object]:
        resolution = copy.deepcopy(RESOLUTION)
        resolution["trust"]["signer_identity"] = SIGNER_IDENTITY
        return resolution

    def test_accepts_signature_provenance_and_both_listed_bundles(self) -> None:
        resolution = self._trusted()
        registry = _Registry(resolution)

        run_cosign = self._verify(resolution, registry, (_signature(resolution), _attestation(resolution)))

        digest = resolution["oci_digest"].removeprefix("sha256:")
        signature = _bundle_digest(resolution["trust"]["signature_reference"])
        provenance = _bundle_digest(resolution["trust"]["provenance_reference"])
        self.assertEqual(
            registry.requests,
            [
                (
                    "/token?scope=repository:theshimpz/shimpz-assistant-trust:pull&service=ghcr.io",
                    "application/json",
                    None,
                    None,
                ),
                (
                    f"/v2/theshimpz/shimpz-assistant-trust/manifests/sha256-{digest}",
                    OCI_INDEX,
                    REGISTRY_BEARER,
                    None,
                ),
                (
                    f"/v2/theshimpz/shimpz-assistant-trust/manifests/{signature}",
                    OCI_MANIFEST,
                    REGISTRY_BEARER,
                    signature,
                ),
                (
                    f"/v2/theshimpz/shimpz-assistant-trust/manifests/{provenance}",
                    OCI_MANIFEST,
                    REGISTRY_BEARER,
                    provenance,
                ),
            ],
        )
        commands = [call.args[0] for call in run_cosign.call_args_list]
        self.assertEqual([command[0] for command in commands], ["verify", "verify-attestation"])
        self.assertTrue(all(command[-1] == resolution["image_reference"] for command in commands))
        self.assertFalse(
            any(argument.startswith("--new-bundle-format") for command in commands for argument in command)
        )

    def test_bundle_evidence_is_proved_before_cosign_runs(self) -> None:
        resolution = self._trusted()
        signature = _bundle_digest(resolution["trust"]["signature_reference"])
        provenance = _bundle_digest(resolution["trust"]["provenance_reference"])

        def drop_listing(registry: _Registry) -> None:
            registry.index["manifests"] = [
                entry for entry in registry.index["manifests"] if entry["digest"] != provenance
            ]

        def set_entry(field: str, value: object) -> object:
            def change(registry: _Registry) -> None:
                registry.index["manifests"][0][field] = value

            return change

        def set_manifest(digest: str, field: str, value: object) -> object:
            def change(registry: _Registry) -> None:
                registry.manifests[digest][field] = value

            return change

        def set_token(value: object) -> object:
            def change(registry: _Registry) -> None:
                registry.token = value

            return change

        def set_index(value: object) -> object:
            def change(registry: _Registry) -> None:
                registry.index = value

            return change

        def drop_manifest(registry: _Registry) -> None:
            registry.manifests[signature] = []

        def swap_predicates(registry: _Registry) -> None:
            registry.manifests[signature], registry.manifests[provenance] = (
                registry.manifests[provenance],
                registry.manifests[signature],
            )

        cases = (
            ("missing-reference", drop_listing),
            ("wrong-artifact-type", set_entry("artifactType", "application/vnd.dev.cosign.artifact.sig.v1+json")),
            ("index-entry-not-a-manifest", set_entry("mediaType", OCI_INDEX)),
            ("wrong-predicate", swap_predicates),
            ("manifest-not-an-object", drop_manifest),
            ("manifest-artifact-type", set_manifest(signature, "artifactType", "application/json")),
            ("manifest-media-type", set_manifest(signature, "mediaType", OCI_INDEX)),
            ("manifest-annotations", set_manifest(signature, "annotations", [])),
            ("foreign-subject", set_manifest(provenance, "subject", {"digest": "sha256:" + "9" * 64})),
            ("subject-shape", set_manifest(provenance, "subject", "subject")),
            ("missing-token", set_token({})),
            ("token-not-an-object", set_token([])),
            ("token-header-injection", set_token({"token": "test\r\nX-Injected: 1"})),
            ("index-media-type", set_index({"mediaType": OCI_MANIFEST, "manifests": []})),
            ("index-shape", set_index({"mediaType": OCI_INDEX, "manifests": {}})),
            ("index-not-an-object", set_index([])),
            ("entry-digest-object", set_entry("digest", {"sha256": "1" * 64})),
            ("entry-digest-list", set_entry("digest", [signature])),
            ("entry-digest-short", set_entry("digest", "sha256:short")),
            ("entry-not-an-object", set_index({"mediaType": OCI_INDEX, "manifests": ["entry"]})),
        )
        for name, change in cases:
            registry = _Registry(resolution)
            change(registry)
            verifier = self._verifier()
            with (
                self.subTest(name=name),
                mock.patch.object(artifact_trust, "_registry_json", registry),
                mock.patch.object(verifier, "_run_cosign") as run_cosign,
                self.assertRaises(ArtifactTrustError),
            ):
                verifier.verify(resolution)
            run_cosign.assert_not_called()

    def test_bundle_references_and_subject_must_be_well_formed(self) -> None:
        for name, path, value in (
            ("foreign-repository", ("trust", "signature_reference"), "ghcr.io/attacker/signatures@sha256:" + "1" * 64),
            ("lookalike-repository", ("trust", "provenance_reference"), f"{TRUST_REPOSITORY}-x@sha256:" + "2" * 64),
            ("invalid-digest", ("trust", "provenance_reference"), f"{TRUST_REPOSITORY}@sha256:short"),
            ("invalid-subject", ("oci_digest",), "sha256:image"),
        ):
            resolution = self._trusted()
            target = resolution
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            registry = mock.Mock()
            with (
                self.subTest(name=name),
                mock.patch.object(artifact_trust, "_registry_json", registry),
                self.assertRaises(ArtifactTrustError),
            ):
                self._verifier().verify(resolution)
            registry.assert_not_called()

    def test_rejects_each_mismatched_authority(self) -> None:
        base = self._trusted()
        cases = (
            ("identity", ("trust", "signer_identity"), "https://attacker.example/workflow"),
            ("source", ("source_digest",), "sha256:" + "9" * 64),
            # The signed provenance binds the language pack beside the manifest and contract (ADR-0091).
            ("language-pack", ("pack_digest",), "sha256:" + "9" * 64),
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
            with self.subTest(name=name), self.assertRaises(ArtifactTrustError):
                self._verify(resolution, _Registry(base), (_signature(base), _attestation(base)))

    def test_a_verified_provenance_claim_alone_is_not_a_signature(self) -> None:
        resolution = self._trusted()
        digest = resolution["oci_digest"]
        for name, records in (
            ("provenance-only", [_claim(digest, PROVENANCE_PREDICATE)]),
            ("untyped", [{"critical": {"image": {"docker-manifest-digest": digest}}}]),
            ("other-digest", [_claim("sha256:" + "9" * 64, SIGNATURE_PREDICATE)]),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ArtifactTrustError, "does not bind"):
                self._verify(resolution, _Registry(resolution), (records, _attestation(resolution)))

    def test_cosign_decoder_rejects_invalid_evidence(self) -> None:
        verifier = self._verifier()
        with (
            mock.patch.object(verifier, "_run_cosign", return_value=b"not-json"),
            self.assertRaisesRegex(ArtifactTrustError, "invalid verification evidence"),
        ):
            verifier._cosign_json("verify")

    def test_registry_reads_are_tunnelled_bounded_and_content_addressed(self) -> None:
        document = b'{"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
        digest = "sha256:" + hashlib.sha256(document).hexdigest()

        def connection(status: int = 200, body: bytes = document, error: Exception | None = None) -> mock.Mock:
            fake = mock.Mock()
            fake.getresponse.return_value.status = status
            fake.getresponse.return_value.read.side_effect = lambda limit: body[:limit]
            if error is not None:
                fake.request.side_effect = error
            return fake

        accepted = connection()
        with mock.patch.object(artifact_trust.http.client, "HTTPSConnection", return_value=accepted) as opened:
            value = artifact_trust._registry_json(
                "/v2/manifest", accept=OCI_MANIFEST, token=REGISTRY_BEARER, digest=digest
            )
        self.assertEqual(value, {"mediaType": OCI_MANIFEST})
        self.assertEqual(opened.call_args.args, ("shimpz-assistant-release", 8888))
        self.assertIs(opened.call_args.kwargs["context"], artifact_trust._TLS_CONTEXT)
        accepted.set_tunnel.assert_called_once_with("ghcr.io", 443)
        accepted.request.assert_called_once_with(
            "GET",
            "/v2/manifest",
            headers={"Accept": OCI_MANIFEST, "Authorization": "Bearer test-token"},
        )
        accepted.close.assert_called_once_with()

        anonymous = connection()
        with mock.patch.object(artifact_trust.http.client, "HTTPSConnection", return_value=anonymous):
            artifact_trust._registry_json("/token", accept="application/json")
        anonymous.request.assert_called_once_with("GET", "/token", headers={"Accept": "application/json"})

        for name, fake, expected in (
            ("unreachable", connection(error=OSError("refused")), "registry is unavailable"),
            ("protocol", connection(error=artifact_trust.http.client.HTTPException()), "registry is unavailable"),
            ("non-200", connection(status=404), "refused the request"),
            ("oversized", connection(body=b"x" * (256 * 1024 + 1)), "too large"),
            ("invalid-json", connection(body=b"not-json"), "invalid document"),
            ("digest-mismatch", connection(body=b"{}"), "does not match its digest"),
        ):
            with (
                self.subTest(name=name),
                mock.patch.object(artifact_trust.http.client, "HTTPSConnection", return_value=fake),
                self.assertRaisesRegex(ArtifactTrustError, expected),
            ):
                artifact_trust._registry_json(
                    "/v2/manifest",
                    accept=OCI_MANIFEST,
                    digest=digest if name == "digest-mismatch" else None,
                )
            fake.close.assert_called_once_with()

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
                self.assertIsNone(artifact_trust._signature_claim(record))

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
