"""Independent Assistant signature and provenance verification."""

import base64
import copy
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from install import artifact_trust
from install.artifact_trust import (
    OIDC_ISSUER,
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
BUNDLE = "application/vnd.dev.sigstore.bundle.v0.3+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
IN_TOTO = "application/vnd.in-toto+json"
REGISTRY_BEARER = "test-token"
TRUST = "/v2/theshimpz/shimpz-assistant-trust"
TOKEN_PATH = "/token?scope=repository:theshimpz/shimpz-assistant-trust:pull&service=ghcr.io"
BLOB_HOST = "pkg-containers.githubusercontent.com"


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _bundle(statement: object, *, payload_type: str = IN_TOTO, media_type: str = BUNDLE) -> bytes:
    payload = base64.b64encode(json.dumps(statement).encode()).decode()
    return json.dumps(
        {
            "mediaType": media_type,
            "verificationMaterial": {},
            "dsseEnvelope": {"payload": payload, "payloadType": payload_type, "signatures": []},
        }
    ).encode()


def _subject(resolution: dict[str, object]) -> list[dict[str, object]]:
    return [{"digest": {"sha256": resolution["oci_digest"].removeprefix("sha256:")}}]


def _signature_statement(resolution: dict[str, object]) -> dict[str, object]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": _subject(resolution),
        "predicateType": SIGNATURE_PREDICATE,
        "predicate": {},
    }


def _provenance_statement(resolution: dict[str, object]) -> dict[str, object]:
    return {
        "_type": "https://in-toto.io/Statement/v0.1",
        "subject": [{"name": "ghcr.io/theshimpz/shimpz-assistant", **_subject(resolution)[0]}],
        "predicateType": PROVENANCE_PREDICATE,
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


class _Registry:
    """GHCR and its blob storage behind the release proxy, holding one resolution's two recorded bundles.

    Construction records the content address of each bundle manifest in the resolution, as Developers does.
    """

    def __init__(self, resolution: dict[str, object]) -> None:
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.responses: dict[tuple[str, str], tuple[int, str | None, bytes]] = {}
        self.serve("ghcr.io", TOKEN_PATH, json.dumps({"token": REGISTRY_BEARER}).encode())
        self.bundles: dict[str, bytes] = {}
        for field, predicate, statement in (
            ("signature_reference", SIGNATURE_PREDICATE, _signature_statement(resolution)),
            ("provenance_reference", PROVENANCE_PREDICATE, _provenance_statement(resolution)),
        ):
            self.bundles[field] = _bundle(statement)
            manifest = self.publish(resolution, predicate, self.bundles[field])
            resolution["trust"][field] = f"{TRUST_REPOSITORY}@{manifest}"

    def serve(self, host: str, path: str, body: bytes, *, status: int = 200, location: str | None = None) -> None:
        self.responses[(host, path)] = (status, location, body)

    def publish(self, resolution: dict[str, object], predicate: str, bundle: bytes, **changes: object) -> str:
        """Store one bundle layer and its manifest; return the manifest digest."""
        layer = _digest(bundle)
        self.serve_blob(layer, bundle)
        manifest = {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "artifactType": BUNDLE,
            "config": {"mediaType": "application/vnd.oci.empty.v1+json", "digest": "sha256:" + "4" * 64, "size": 2},
            "layers": [{"mediaType": BUNDLE, "digest": layer, "size": len(bundle)}],
            "annotations": {"dev.sigstore.bundle.predicateType": predicate},
            "subject": {"mediaType": OCI_INDEX, "digest": resolution["oci_digest"], "size": 1547},
            **changes,
        }
        raw = json.dumps(manifest).encode()
        self.serve("ghcr.io", f"{TRUST}/manifests/{_digest(raw)}", raw)
        return _digest(raw)

    def serve_blob(self, digest: str, body: bytes) -> None:
        """GHCR redirects every blob read to its storage host, as it does in production."""
        location = f"https://{BLOB_HOST}/ghcrblobs01/blobs/{digest}?se=test&sig=test"
        self.serve("ghcr.io", f"{TRUST}/blobs/{digest}", b"", status=307, location=location)
        self.serve(BLOB_HOST, f"/ghcrblobs01/blobs/{digest}?se=test&sig=test", body)

    def __call__(self, host: str, path: str, headers: dict[str, str]) -> tuple[int, str | None, bytes]:
        self.requests.append((host, path, dict(headers)))
        return self.responses.get((host, path), (404, None, b'{"errors":[]}'))


class ArtifactTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self._trust_root = Path(self._temporary_directory.name) / "trust"

    def _verifier(self, api: object | None = None) -> ArtifactTrustVerifier:
        return ArtifactTrustVerifier(
            types.SimpleNamespace(api=api),
            container_id="a" * 64,
            trust_root=self._trust_root,
        )

    def _trusted(self) -> tuple[dict[str, object], _Registry]:
        resolution = copy.deepcopy(RESOLUTION)
        resolution["trust"]["signer_identity"] = SIGNER_IDENTITY
        return resolution, _Registry(resolution)

    def _verify(self, resolution: dict[str, object], registry: _Registry, cosign: object = None) -> mock.Mock:
        verifier = self._verifier()
        with (
            mock.patch.object(artifact_trust, "_tunnelled_get", registry),
            mock.patch.object(verifier, "_run_cosign", side_effect=cosign) as run_cosign,
        ):
            verifier.verify(resolution)
        return run_cosign

    def _refuse(self, resolution: dict[str, object], registry: _Registry, expected: str = "") -> None:
        verifier = self._verifier()
        with (
            mock.patch.object(artifact_trust, "_tunnelled_get", registry),
            mock.patch.object(verifier, "_run_cosign") as run_cosign,
            self.assertRaisesRegex(ArtifactTrustError, expected),
        ):
            verifier.verify(resolution)
        run_cosign.assert_not_called()

    def test_cosign_verifies_exactly_the_recorded_bundle_bytes(self) -> None:
        resolution, registry = self._trusted()

        run_cosign = self._verify(resolution, registry)

        signature = resolution["trust"]["signature_reference"].removeprefix(f"{TRUST_REPOSITORY}@")
        provenance = resolution["trust"]["provenance_reference"].removeprefix(f"{TRUST_REPOSITORY}@")
        signature_layer = _digest(registry.bundles["signature_reference"])
        provenance_layer = _digest(registry.bundles["provenance_reference"])
        bearer = {"Authorization": "Bearer test-token"}
        self.assertEqual(
            registry.requests,
            [
                ("ghcr.io", TOKEN_PATH, {"Accept": "application/json"}),
                ("ghcr.io", f"{TRUST}/manifests/{signature}", {"Accept": OCI_MANIFEST, **bearer}),
                ("ghcr.io", f"{TRUST}/blobs/{signature_layer}", {"Accept": BUNDLE, **bearer}),
                (BLOB_HOST, f"/ghcrblobs01/blobs/{signature_layer}?se=test&sig=test", {"Accept": BUNDLE}),
                ("ghcr.io", f"{TRUST}/manifests/{provenance}", {"Accept": OCI_MANIFEST, **bearer}),
                ("ghcr.io", f"{TRUST}/blobs/{provenance_layer}", {"Accept": BUNDLE, **bearer}),
                (BLOB_HOST, f"/ghcrblobs01/blobs/{provenance_layer}?se=test&sig=test", {"Accept": BUNDLE}),
            ],
        )
        hexadecimal = resolution["oci_digest"].removeprefix("sha256:")
        self.assertEqual(
            [call.args for call in run_cosign.call_args_list],
            [
                (
                    registry.bundles[field],
                    (
                        "verify-blob-attestation",
                        "--certificate-identity",
                        SIGNER_IDENTITY,
                        "--certificate-oidc-issuer",
                        OIDC_ISSUER,
                        "--type",
                        predicate,
                        "--digest",
                        hexadecimal,
                        "--digestAlg",
                        "sha256",
                    ),
                )
                for field, predicate in (
                    ("signature_reference", SIGNATURE_PREDICATE),
                    ("provenance_reference", PROVENANCE_PREDICATE),
                )
            ],
        )

    def test_mutable_registry_state_cannot_substitute_a_bundle(self) -> None:
        resolution, registry = self._trusted()
        subject_index = f"{TRUST}/manifests/sha256-{resolution['oci_digest'].removeprefix('sha256:')}"
        # Another bundle by the same signer for the same digest, valid on its own, listed in the referrers index.
        unrelated_bundle = _bundle({**_signature_statement(resolution), "predicate": {"other": 1}})
        unrelated = registry.publish(resolution, SIGNATURE_PREDICATE, unrelated_bundle)
        index = {
            "schemaVersion": 2,
            "mediaType": OCI_INDEX,
            "manifests": [{"mediaType": OCI_MANIFEST, "digest": unrelated, "artifactType": BUNDLE}],
        }
        registry.serve("ghcr.io", subject_index, json.dumps(index).encode())
        verified: list[bytes] = []

        def replace_registry_state(bundle: bytes, _arguments: tuple[str, ...]) -> None:
            # After the reads, every mutable registry document now names only the unrelated bundle.
            for field in ("signature_reference", "provenance_reference"):
                registry.serve_blob(_digest(registry.bundles[field]), unrelated_bundle)
            registry.serve("ghcr.io", subject_index, json.dumps({**index, "manifests": []}).encode())
            verified.append(bundle)

        self._verify(resolution, registry, replace_registry_state)

        self.assertEqual(verified, [registry.bundles["signature_reference"], registry.bundles["provenance_reference"]])
        self.assertNotIn(subject_index, [path for _host, path, _headers in registry.requests])
        self.assertNotIn(f"{TRUST}/manifests/{unrelated}", [path for _host, path, _headers in registry.requests])

    def test_recorded_manifest_must_carry_exactly_one_matching_bundle_layer(self) -> None:
        def manifest_case(**changes: object) -> object:
            def change(resolution: dict[str, object], registry: _Registry) -> None:
                bundle = registry.bundles["signature_reference"]
                digest = registry.publish(resolution, SIGNATURE_PREDICATE, bundle, **changes)
                resolution["trust"]["signature_reference"] = f"{TRUST_REPOSITORY}@{digest}"

            return change

        def layer(**changes: object) -> list[dict[str, object]]:
            return [{"mediaType": BUNDLE, "digest": "sha256:" + "1" * 64, "size": 1, **changes}]

        def tampered_layer(resolution: dict[str, object], registry: _Registry) -> None:
            bundle = registry.bundles["signature_reference"]
            registry.serve_blob(_digest(bundle), bundle.replace(b"verificationMaterial", b"verificationMaterials"))

        def foreign_redirect(location: str | None, status: int = 307) -> object:
            def change(resolution: dict[str, object], registry: _Registry) -> None:
                digest = _digest(registry.bundles["signature_reference"])
                registry.serve("ghcr.io", f"{TRUST}/blobs/{digest}", b"", status=status, location=location)
                registry.serve("attacker.example", f"/blobs/{digest}", registry.bundles["signature_reference"])

            return change

        def tampered_manifest(resolution: dict[str, object], registry: _Registry) -> None:
            digest = resolution["trust"]["signature_reference"].removeprefix(f"{TRUST_REPOSITORY}@")
            registry.serve("ghcr.io", f"{TRUST}/manifests/{digest}", b'{"mediaType":"' + OCI_MANIFEST.encode() + b'"}')

        def array_manifest(resolution: dict[str, object], registry: _Registry) -> None:
            registry.serve("ghcr.io", f"{TRUST}/manifests/{_digest(b'[]')}", b"[]")
            resolution["trust"]["signature_reference"] = f"{TRUST_REPOSITORY}@{_digest(b'[]')}"

        cases = (
            ("manifest-not-an-object", array_manifest, "does not match"),
            ("zero-layers", manifest_case(layers=[]), "exactly one bundle layer"),
            ("two-layers", manifest_case(layers=layer() * 2), "exactly one bundle layer"),
            ("layers-not-a-list", manifest_case(layers={}), "exactly one bundle layer"),
            ("layer-media-type", manifest_case(layers=layer(mediaType="application/json")), "exactly one bundle"),
            ("layer-not-an-object", manifest_case(layers=["layer"]), "descriptor is invalid"),
            ("layer-digest-object", manifest_case(layers=layer(digest={"sha256": "1" * 64})), "descriptor"),
            ("layer-digest-list", manifest_case(layers=layer(digest=["sha256:" + "1" * 64])), "descriptor"),
            ("layer-digest-short", manifest_case(layers=layer(digest="sha256:short")), "descriptor is invalid"),
            ("layer-bytes-mismatch", tampered_layer, "does not match its digest"),
            ("manifest-bytes-mismatch", tampered_manifest, "does not match its digest"),
            ("redirect-foreign-host", foreign_redirect("https://attacker.example/blobs/x"), "redirect"),
            ("redirect-plain-http", foreign_redirect(f"http://{BLOB_HOST}/blobs/x"), "redirect"),
            ("redirect-host-port", foreign_redirect(f"https://{BLOB_HOST}:8443/blobs/x"), "redirect"),
            ("redirect-userinfo", foreign_redirect(f"https://user@{BLOB_HOST}/blobs/x"), "redirect"),
            ("redirect-relative", foreign_redirect("blobs/x"), "redirect"),
            ("redirect-missing", foreign_redirect(None), "redirect"),
            ("redirect-permanent", foreign_redirect(f"https://{BLOB_HOST}/blobs/x", 301), "refused"),
            ("blob-missing", foreign_redirect(f"https://{BLOB_HOST}/blobs/missing"), "refused"),
            ("manifest-artifact-type", manifest_case(artifactType="application/json"), "does not match"),
            ("manifest-media-type", manifest_case(mediaType=OCI_INDEX), "does not match"),
            ("manifest-annotations", manifest_case(annotations=[]), "does not match"),
            ("manifest-predicate", manifest_case(annotations={"dev.sigstore.bundle.predicateType": "x"}), "match"),
            ("manifest-subject", manifest_case(subject={"digest": "sha256:" + "9" * 64}), "does not match"),
            ("manifest-subject-shape", manifest_case(subject="subject"), "does not match"),
        )
        for name, change, expected in cases:
            resolution, registry = self._trusted()
            change(resolution, registry)
            with self.subTest(name=name):
                self._refuse(resolution, registry, expected)

    def test_bundle_statement_must_bind_the_digest_and_predicate_before_cosign_runs(self) -> None:
        def bundle_case(field: str, bundle: object) -> object:
            def change(resolution: dict[str, object], registry: _Registry) -> None:
                predicate = SIGNATURE_PREDICATE if field == "signature_reference" else PROVENANCE_PREDICATE
                raw = bundle(resolution) if callable(bundle) else bundle
                digest = registry.publish(resolution, predicate, raw)
                resolution["trust"][field] = f"{TRUST_REPOSITORY}@{digest}"

            return change

        def statement(field: str, **changes: object) -> object:
            build = _signature_statement if field == "signature_reference" else _provenance_statement
            return lambda resolution: _bundle({**build(resolution), **changes})

        signature_field = "signature_reference"
        cases = (
            ("bundle-not-an-object", bundle_case(signature_field, b"[]"), "not a DSSE"),
            ("bundle-not-json", bundle_case(signature_field, b"not-json"), "invalid document"),
            ("bundle-deeply-nested", bundle_case(signature_field, b"[" * 100_000), "invalid document"),
            (
                "bundle-media-type",
                bundle_case(signature_field, lambda r: _bundle(_signature_statement(r), media_type="application/json")),
                "not a DSSE",
            ),
            (
                "bundle-payload-type",
                bundle_case(signature_field, lambda r: _bundle(_signature_statement(r), payload_type="text/plain")),
                "not a DSSE",
            ),
            (
                "bundle-without-envelope",
                bundle_case(signature_field, json.dumps({"mediaType": BUNDLE}).encode()),
                "not a DSSE",
            ),
            (
                "payload-not-base64",
                bundle_case(
                    signature_field,
                    json.dumps(
                        {"mediaType": BUNDLE, "dsseEnvelope": {"payload": "%%%", "payloadType": IN_TOTO}}
                    ).encode(),
                ),
                "not a DSSE",
            ),
            (
                "payload-not-a-string",
                bundle_case(
                    signature_field,
                    json.dumps({"mediaType": BUNDLE, "dsseEnvelope": {"payload": 1, "payloadType": IN_TOTO}}).encode(),
                ),
                "not a DSSE",
            ),
            ("statement-not-an-object", bundle_case(signature_field, _bundle([])), "not a DSSE"),
            (
                "bundle-duplicate-key",
                bundle_case(
                    signature_field,
                    lambda r: _bundle(_signature_statement(r)).replace(
                        b'"verificationMaterial": {}', b'"dsseEnvelope": {}, "verificationMaterial": {}'
                    ),
                ),
                "invalid document",
            ),
            (
                "statement-duplicate-key",
                bundle_case(
                    signature_field,
                    lambda r: _bundle(_signature_statement(r)).replace(
                        b'"payload": "',
                        b'"payload": "'
                        + base64.b64encode(
                            b'{"predicateType": "x", ' + json.dumps(_signature_statement(r))[1:].encode()
                        )
                        + b'", "ignored": "',
                    ),
                ),
                "invalid document",
            ),
            (
                "signature-predicate",
                bundle_case(signature_field, statement(signature_field, predicateType=PROVENANCE_PREDICATE)),
                "does not match the Assistant digest",
            ),
            (
                "signature-subject",
                bundle_case(signature_field, statement(signature_field, subject=[{"digest": {"sha256": "9" * 64}}])),
                "does not match the Assistant digest",
            ),
            (
                "signature-subject-shape",
                bundle_case(signature_field, statement(signature_field, subject={"digest": {}})),
                "does not match the Assistant digest",
            ),
            (
                "signature-subject-entry-shape",
                bundle_case(signature_field, statement(signature_field, subject=["subject", {"digest": "x"}])),
                "does not match the Assistant digest",
            ),
            (
                "provenance-predicate",
                bundle_case("provenance_reference", statement("provenance_reference", predicateType="x")),
                "does not match the Assistant digest",
            ),
        )
        for name, change, expected in cases:
            resolution, registry = self._trusted()
            change(resolution, registry)
            with self.subTest(name=name):
                self._refuse(resolution, registry, expected)

    def test_a_cosign_refusal_of_either_bundle_fails_verification(self) -> None:
        for failing in range(2):
            resolution, registry = self._trusted()
            calls: list[bytes] = []

            def cosign(
                bundle: bytes, _arguments: tuple[str, ...], calls: list[bytes] = calls, failing: int = failing
            ) -> None:
                calls.append(bundle)
                if len(calls) - 1 == failing:
                    raise ArtifactTrustError("Cosign verification failed")

            with self.subTest(failing=failing), self.assertRaisesRegex(ArtifactTrustError, "verification failed"):
                self._verify(resolution, registry, cosign)
            self.assertEqual(len(calls), failing + 1)

    def test_signed_provenance_must_bind_the_publication(self) -> None:
        for name, changes in (
            ("statement-type", {"_type": "https://in-toto.io/Statement/v1"}),
            ("predicate-shape", {"predicate": []}),
            ("predicate-empty", {"predicate": {}}),
            ("build-type", {"predicate": {"buildDefinition": {"buildType": "x"}, "runDetails": {}}}),
        ):
            resolution, registry = self._trusted()
            bundle = _bundle({**_provenance_statement(resolution), **changes})
            digest = registry.publish(resolution, PROVENANCE_PREDICATE, bundle)
            resolution["trust"]["provenance_reference"] = f"{TRUST_REPOSITORY}@{digest}"
            with self.subTest(name=name):
                self._refuse(resolution, registry, "provenance does not match")

        base, registry = self._trusted()
        for name, path, value in (
            ("source", ("source_digest",), "sha256:" + "9" * 64),
            # The signed provenance binds the language pack beside the manifest and contract (ADR-0091).
            ("language-pack", ("pack_digest",), "sha256:" + "9" * 64),
            ("version", ("assistant_version",), "9.9.9"),
        ):
            resolution = copy.deepcopy(base)
            resolution[path[0]] = value
            with self.subTest(name=name):
                self._refuse(resolution, registry, "provenance does not match")

    def test_resolution_authorities_are_checked_before_any_registry_read(self) -> None:
        for name, path, value in (
            ("identity", ("trust", "signer_identity"), "https://attacker.example/workflow"),
            ("foreign-repository", ("trust", "signature_reference"), "ghcr.io/attacker/signatures@sha256:" + "1" * 64),
            ("lookalike-repository", ("trust", "provenance_reference"), f"{TRUST_REPOSITORY}-x@sha256:" + "2" * 64),
            ("invalid-digest", ("trust", "provenance_reference"), f"{TRUST_REPOSITORY}@sha256:short"),
            ("invalid-subject", ("oci_digest",), "sha256:image"),
        ):
            resolution, _registry = self._trusted()
            target = resolution
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            registry = mock.Mock()
            with (
                self.subTest(name=name),
                mock.patch.object(artifact_trust, "_tunnelled_get", registry),
                self.assertRaises(ArtifactTrustError),
            ):
                self._verifier().verify(resolution)
            registry.assert_not_called()

    def test_unrecorded_bundle_and_token_failures_refuse(self) -> None:
        def unknown_signature(resolution: dict[str, object], registry: _Registry) -> None:
            resolution["trust"]["signature_reference"] = f"{TRUST_REPOSITORY}@sha256:" + "9" * 64

        def token(body: bytes, status: int = 200, location: str | None = None) -> object:
            def change(resolution: dict[str, object], registry: _Registry) -> None:
                registry.serve("ghcr.io", TOKEN_PATH, body, status=status, location=location)

            return change

        for name, change, expected in (
            ("unknown-signature-reference", unknown_signature, "refused the request"),
            ("missing-token", token(b"{}"), "token is unavailable"),
            ("token-not-an-object", token(b"[]"), "token is unavailable"),
            ("token-header-injection", token(b'{"token": "test\\r\\nX-Injected: 1"}'), "token is unavailable"),
            # Only a content-addressed read follows the storage redirect.
            ("token-redirect", token(b"", 307, f"https://{BLOB_HOST}/token"), "refused the request"),
        ):
            resolution, registry = self._trusted()
            change(resolution, registry)
            with self.subTest(name=name):
                self._refuse(resolution, registry, expected)

    def test_cosign_reads_the_bundle_from_a_private_file_with_only_the_release_proxy(self) -> None:
        observed: list[tuple[list[str], bytes, int]] = []
        api = mock.Mock()

        def exec_create(**kwargs: object) -> dict[str, str]:
            bundle_path = Path(kwargs["cmd"][-1])
            observed.append((kwargs["cmd"], bundle_path.read_bytes(), bundle_path.stat().st_mode & 0o777))
            return {"Id": f"execution-{len(observed)}"}

        api.exec_create.side_effect = exec_create
        api.exec_start.side_effect = lambda *_args, **_kwargs: iter(((b"", b"Verified OK\n"),))
        api.exec_inspect.return_value = {"ExitCode": 0}
        verifier = self._verifier(api)

        verifier._run_cosign(b"first-bundle", ("verify-blob-attestation", "--digestAlg", "sha256"))
        verifier._run_cosign(b"second-bundle", ("verify-blob-attestation",))

        first_command, first_bundle, first_mode = observed[0]
        self.assertEqual(
            first_command[:-1],
            [
                "/usr/bin/timeout",
                "--kill-after=5s",
                "90s",
                "/usr/local/bin/cosign",
                "verify-blob-attestation",
                "--digestAlg",
                "sha256",
                "--bundle",
            ],
        )
        self.assertEqual((first_bundle, first_mode), (b"first-bundle", 0o600))
        self.assertEqual(observed[1][1], b"second-bundle")
        environments = [call.kwargs["environment"] for call in api.exec_create.call_args_list]
        homes = [next(value for value in environment if value.startswith("HOME=")) for environment in environments]
        for environment, (command, _bundle, _mode) in zip(environments, observed, strict=True):
            self.assertEqual(
                {value.split("=", 1)[0] for value in environment},
                {
                    "HOME",
                    "PATH",
                    "TUF_ROOT",
                    "HTTPS_PROXY",
                    "https_proxy",
                    "HTTP_PROXY",
                    "http_proxy",
                    "NO_PROXY",
                    "no_proxy",
                },
            )
            self.assertIn(f"TUF_ROOT={self._trust_root}", environment)
            self.assertTrue(
                {f"HTTPS_PROXY={RELEASE_PROXY_URL}", f"HTTP_PROXY={RELEASE_PROXY_URL}", "NO_PROXY="} <= set(environment)
            )
            self.assertEqual(Path(command[-1]).parent, Path(next(v for v in environment if v.startswith("HOME="))[5:]))
        self.assertNotEqual(homes[0], homes[1])
        self.assertEqual(api.exec_create.call_args.kwargs["user"], "10001")
        self.assertEqual(self._trust_root.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(not Path(value.removeprefix("HOME=")).exists() for value in homes))

    def test_cosign_execution_is_bounded_and_failures_are_mapped(self) -> None:
        api = mock.Mock()
        api.exec_create.return_value = {"Id": "execution"}
        verifier = self._verifier(api)

        def run(stream: tuple[tuple[bytes | None, bytes | None], ...], exit_code: int | None = 0) -> None:
            api.exec_start.return_value = iter(stream)
            api.exec_inspect.return_value = {"ExitCode": exit_code}
            verifier._run_cosign(b"bundle", ("verify-blob-attestation",))

        run(((None, b"Verified OK\n"),))
        with self.assertRaisesRegex(ArtifactTrustError, "timed out"):
            run((), 124)
        with (
            mock.patch.object(artifact_trust.time, "monotonic", side_effect=(0, 91)),
            self.assertRaisesRegex(ArtifactTrustError, "timed out"),
        ):
            run(((b"still-running", None),))
        with (
            mock.patch.object(artifact_trust, "_MAX_OUTPUT_BYTES", 1),
            self.assertRaisesRegex(ArtifactTrustError, "output is too large"),
        ):
            run(((None, b"too-large"),))
        with self.assertRaisesRegex(ArtifactTrustError, "verification failed"):
            run((), 1)
        api.exec_create.return_value = {}
        with self.assertRaisesRegex(ArtifactTrustError, "verification is unavailable"):
            run(())

    def test_rejects_a_non_private_tuf_cache(self) -> None:
        self._trust_root.mkdir(mode=0o755)

        with self.assertRaisesRegex(RuntimeError, "not a private"):
            self._verifier()

    def test_registry_reads_are_tunnelled_bounded_and_content_addressed(self) -> None:
        document = b'{"mediaType":"application/vnd.oci.image.manifest.v1+json"}'

        def connection(status: int = 200, body: bytes = document, error: Exception | None = None) -> mock.Mock:
            fake = mock.Mock()
            fake.getresponse.return_value.status = status
            fake.getresponse.return_value.read.side_effect = lambda limit: body[:limit]
            fake.getresponse.return_value.getheader.return_value = "https://example.invalid/x"
            if error is not None:
                fake.request.side_effect = error
            return fake

        accepted = connection()
        with mock.patch.object(artifact_trust.http.client, "HTTPSConnection", return_value=accepted) as opened:
            value = artifact_trust._tunnelled_get("ghcr.io", "/v2/manifest", {"Accept": OCI_MANIFEST})
        self.assertEqual(value, (200, "https://example.invalid/x", document))
        self.assertEqual(opened.call_args.args, ("shimpz-assistant-release", 8888))
        self.assertIs(opened.call_args.kwargs["context"], artifact_trust._TLS_CONTEXT)
        accepted.set_tunnel.assert_called_once_with("ghcr.io", 443)
        accepted.request.assert_called_once_with("GET", "/v2/manifest", headers={"Accept": OCI_MANIFEST})
        accepted.getresponse.return_value.getheader.assert_called_once_with("Location")
        accepted.close.assert_called_once_with()

        for name, fake, expected in (
            ("unreachable", connection(error=OSError("refused")), "registry is unavailable"),
            ("protocol", connection(error=artifact_trust.http.client.HTTPException()), "registry is unavailable"),
            ("oversized", connection(body=b"x" * (256 * 1024 + 1)), "too large"),
        ):
            with (
                self.subTest(name=name),
                mock.patch.object(artifact_trust.http.client, "HTTPSConnection", return_value=fake),
                self.assertRaisesRegex(ArtifactTrustError, expected),
            ):
                artifact_trust._tunnelled_get("ghcr.io", "/v2/manifest", {"Accept": OCI_MANIFEST})
            fake.close.assert_called_once_with()

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


if __name__ == "__main__":
    unittest.main()
