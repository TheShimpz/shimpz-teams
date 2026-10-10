"""Independent Sigstore verification for one published Assistant artifact."""

import base64
import binascii
import hashlib
import http.client
import json
import os
import re
import ssl
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

import docker

from protocol.http.v1 import payload as http_payload

from . import registry_auth

SIGNER_IDENTITY = "https://github.com/TheShimpz/shimpz-developers/.github/workflows/build-assistant.yml@refs/heads/main"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_REGISTRY_HOST = "ghcr.io"
_TRUST_NAME = "theshimpz/shimpz-assistant-trust"
TRUST_REPOSITORY = f"{_REGISTRY_HOST}/{_TRUST_NAME}"
_RELEASE_PROXY_HOST = "shimpz-assistant-release"
_RELEASE_PROXY_PORT = 8888
RELEASE_PROXY_URL = f"http://{_RELEASE_PROXY_HOST}:{_RELEASE_PROXY_PORT}"
SIGNATURE_PREDICATE = "https://sigstore.dev/cosign/sign/v1"
PROVENANCE_PREDICATE = "https://slsa.dev/provenance/v1"
_BUNDLE_ARTIFACT_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
_PREDICATE_ANNOTATION = "dev.sigstore.bundle.predicateType"
_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_BEARER_TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_MAX_REGISTRY_BYTES = 256 * 1024
_REGISTRY_TIMEOUT_SECONDS = 10
_TLS_CONTEXT = ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")
_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_TIMEOUT_SECONDS = 90
_TIMEOUT_EXIT_CODES = frozenset({124, 137})
_RELEASE_PROXY_ENVIRONMENT = (
    f"HTTPS_PROXY={RELEASE_PROXY_URL}",
    f"https_proxy={RELEASE_PROXY_URL}",
    f"HTTP_PROXY={RELEASE_PROXY_URL}",
    f"http_proxy={RELEASE_PROXY_URL}",
    "NO_PROXY=",
    "no_proxy=",
)


class ArtifactTrustError(RuntimeError):
    """The artifact signature or signed provenance could not be proved."""


class ArtifactTrustVerifier:
    def __init__(
        self,
        docker_client: object,
        binary: str = "/usr/local/bin/cosign",
        *,
        container_id: str | None = None,
        credentials: registry_auth.AnonymousRegistryAccess,
        trust_root: Path,
    ) -> None:
        self._docker = docker_client
        self._binary = binary
        self._container_id = _self_container_id() if container_id is None else container_id
        self._credentials = credentials
        self._trust_root = _ensure_private_directory(trust_root)

    def verify(self, resolution: dict[str, Any]) -> None:
        if resolution["trust"]["signer_identity"] != SIGNER_IDENTITY:
            raise ArtifactTrustError("Assistant signer identity is not trusted")
        # Cosign falls back to the legacy signature tags when the digest has no bundle referrers, so the recorded
        # bundles must be proved present before Cosign runs.
        _verify_bundles(resolution)
        image = resolution["image_reference"]
        signature = self._cosign_json(
            "verify",
            "--certificate-identity",
            SIGNER_IDENTITY,
            "--certificate-oidc-issuer",
            OIDC_ISSUER,
            "--output",
            "json",
            image,
        )
        _verify_signature_payload(signature, resolution["oci_digest"])
        attestation = self._cosign_json(
            "verify-attestation",
            "--certificate-identity",
            SIGNER_IDENTITY,
            "--certificate-oidc-issuer",
            OIDC_ISSUER,
            "--type",
            "slsaprovenance1",
            "--output",
            "json",
            image,
        )
        _verify_provenance(attestation, resolution)

    def _cosign_json(self, *arguments: str) -> object:
        raw = self._run_cosign(arguments)
        try:
            return json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactTrustError("Cosign returned invalid verification evidence") from exc

    def _run_cosign(self, arguments: tuple[str, ...]) -> bytes:
        with (
            tempfile.TemporaryDirectory(prefix="shimpz-cosign-") as directory,
            self._credentials.docker_config() as docker_config,
        ):
            try:
                execution = self._docker.api.exec_create(
                    container=self._container_id,
                    cmd=[
                        "/usr/bin/timeout",
                        "--kill-after=5s",
                        f"{_TIMEOUT_SECONDS}s",
                        self._binary,
                        *arguments,
                    ],
                    stdout=True,
                    stderr=True,
                    stdin=False,
                    tty=False,
                    user="10001",
                    environment=[
                        f"HOME={directory}",
                        f"PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
                        f"COSIGN_REPOSITORY={TRUST_REPOSITORY}",
                        f"DOCKER_CONFIG={docker_config}",
                        f"TUF_ROOT={self._trust_root}",
                        *_RELEASE_PROXY_ENVIRONMENT,
                    ],
                )
                execution_id = execution["Id"]
                started = time.monotonic()
                output = bytearray()
                error_bytes = 0
                for stdout, stderr in self._docker.api.exec_start(
                    execution_id,
                    stream=True,
                    demux=True,
                ):
                    if time.monotonic() - started > _TIMEOUT_SECONDS:
                        raise ArtifactTrustError("Cosign verification timed out")
                    output.extend(stdout or b"")
                    error_bytes += len(stderr or b"")
                    if len(output) + error_bytes > _MAX_OUTPUT_BYTES:
                        raise ArtifactTrustError("Cosign verification output is too large")
                result = self._docker.api.exec_inspect(execution_id)
                if result.get("ExitCode") in _TIMEOUT_EXIT_CODES:
                    raise ArtifactTrustError("Cosign verification timed out")
                if result.get("ExitCode") != 0:
                    raise ArtifactTrustError("Cosign verification failed")
                return bytes(output)
            except (KeyError, TypeError, docker.errors.DockerException) as exc:
                raise ArtifactTrustError("Cosign verification is unavailable") from exc


def _verify_bundles(resolution: dict[str, Any]) -> None:
    """Prove both recorded bundles are the trust repository's bundle referrers of the Assistant digest."""
    oci_digest = resolution["oci_digest"]
    if http_payload.SOURCE_DIGEST_RE.fullmatch(oci_digest) is None:
        raise ArtifactTrustError("Assistant OCI digest is invalid")
    expected = (
        (_trust_digest(resolution["trust"]["signature_reference"]), SIGNATURE_PREDICATE),
        (_trust_digest(resolution["trust"]["provenance_reference"]), PROVENANCE_PREDICATE),
    )
    token = _pull_token()
    # GHCR serves no referrers API; Cosign lists each bundle in the referrers tag index of the subject digest.
    listed = _listed_bundles(
        _registry_json(
            f"/v2/{_TRUST_NAME}/manifests/sha256-{oci_digest.removeprefix('sha256:')}",
            accept=_OCI_INDEX,
            token=token,
        )
    )
    for digest, predicate in expected:
        if digest not in listed:
            raise ArtifactTrustError("Sigstore bundle is not listed for the Assistant digest")
        manifest = _registry_json(
            f"/v2/{_TRUST_NAME}/manifests/{digest}",
            accept=_OCI_MANIFEST,
            token=token,
            digest=digest,
        )
        if not _bundle_matches(manifest, oci_digest, predicate):
            raise ArtifactTrustError("Sigstore bundle does not match the Assistant digest")


def _trust_digest(reference: str) -> str:
    digest = reference.removeprefix(f"{TRUST_REPOSITORY}@")
    if digest == reference or http_payload.SOURCE_DIGEST_RE.fullmatch(digest) is None:
        raise ArtifactTrustError("Sigstore bundle is outside the trust repository")
    return digest


def _pull_token() -> str:
    value = _registry_json(
        f"/token?scope=repository:{_TRUST_NAME}:pull&service={_REGISTRY_HOST}",
        accept="application/json",
    )
    token = value.get("token") if isinstance(value, dict) else None
    if not isinstance(token, str) or _BEARER_TOKEN_RE.fullmatch(token) is None:
        raise ArtifactTrustError("Sigstore bundle registry token is unavailable")
    return token


def _listed_bundles(index: object) -> set[object]:
    entries = index.get("manifests") if isinstance(index, dict) and index.get("mediaType") == _OCI_INDEX else None
    if not isinstance(entries, list):
        raise ArtifactTrustError("Sigstore bundle index is invalid")
    return {
        entry.get("digest")
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("mediaType") == _OCI_MANIFEST
        and entry.get("artifactType") == _BUNDLE_ARTIFACT_TYPE
    }


def _bundle_matches(manifest: object, oci_digest: str, predicate: str) -> bool:
    if not isinstance(manifest, dict):
        return False
    annotations = manifest.get("annotations")
    subject = manifest.get("subject")
    return (
        manifest.get("mediaType") == _OCI_MANIFEST
        and manifest.get("artifactType") == _BUNDLE_ARTIFACT_TYPE
        and isinstance(annotations, dict)
        and annotations.get(_PREDICATE_ANNOTATION) == predicate
        and isinstance(subject, dict)
        and subject.get("digest") == oci_digest
    )


def _registry_json(path: str, *, accept: str, token: str | None = None, digest: str | None = None) -> object:
    """Read one bounded registry document through the exact-allowlisted release proxy."""
    connection = http.client.HTTPSConnection(
        _RELEASE_PROXY_HOST,
        _RELEASE_PROXY_PORT,
        timeout=_REGISTRY_TIMEOUT_SECONDS,
        context=_TLS_CONTEXT,
    )
    connection.set_tunnel(_REGISTRY_HOST, 443)
    headers = {"Accept": accept}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        raw = response.read(_MAX_REGISTRY_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise ArtifactTrustError("Sigstore bundle registry is unavailable") from exc
    finally:
        connection.close()
    if response.status != 200:
        raise ArtifactTrustError("Sigstore bundle registry refused the request")
    if len(raw) > _MAX_REGISTRY_BYTES:
        raise ArtifactTrustError("Sigstore bundle registry response is too large")
    if digest is not None and f"sha256:{hashlib.sha256(raw).hexdigest()}" != digest:
        raise ArtifactTrustError("Sigstore bundle manifest does not match its digest")
    try:
        return json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactTrustError("Sigstore bundle registry returned an invalid document") from exc


def _ensure_private_directory(path: Path) -> Path:
    try:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("Cosign trust cache is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise RuntimeError("Cosign trust cache is not a private controller directory")
    return path


def _self_container_id() -> str:
    try:
        value = Path("/etc/hostname").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Team Controller container identity is unavailable") from exc
    if not 12 <= len(value) <= 64 or any(character not in "0123456789abcdef" for character in value):
        raise RuntimeError("Team Controller container identity is invalid")
    return value


def _verify_signature_payload(value: object, oci_digest: str) -> None:
    # Verification lists every bundle it proved, the provenance attestation included; only a signature claim counts.
    records = value if isinstance(value, list) else []
    if not any(_signature_claim(record) == (oci_digest, SIGNATURE_PREDICATE) for record in records):
        raise ArtifactTrustError("Cosign signature does not bind the Assistant digest")


def _signature_claim(record: object) -> tuple[object, object] | None:
    critical = record.get("critical") if isinstance(record, dict) else None
    if not isinstance(critical, dict):
        return None
    image = critical.get("image")
    if not isinstance(image, dict):
        return None
    return image.get("docker-manifest-digest"), critical.get("type")


def _verify_provenance(value: object, resolution: dict[str, Any]) -> None:
    envelopes = (value,) if isinstance(value, dict) else ()
    if not any(_provenance_matches(envelope, resolution) for envelope in envelopes):
        raise ArtifactTrustError("signed provenance does not match the Assistant publication")


def _provenance_matches(envelope: object, resolution: dict[str, Any]) -> bool:
    if not isinstance(envelope, dict) or envelope.get("payloadType") != "application/vnd.in-toto+json":
        return False
    payload = envelope.get("payload")
    if not isinstance(payload, str):
        return False
    statement = _decode_statement(payload)
    if statement is None:
        return False
    subjects = statement.get("subject")
    predicate = statement.get("predicate")
    return (
        isinstance(subjects, list)
        and isinstance(predicate, dict)
        and _subject_matches(subjects, resolution["oci_digest"])
        and statement.get("_type") == "https://in-toto.io/Statement/v0.1"
        and statement.get("predicateType") == "https://slsa.dev/provenance/v1"
        and _predicate_matches(predicate, resolution)
    )


def _decode_statement(payload: str) -> dict[str, Any] | None:
    try:
        statement = json.loads(base64.b64decode(payload, validate=True))
    except ValueError, binascii.Error, UnicodeError, json.JSONDecodeError:
        return None
    return statement if isinstance(statement, dict) else None


def _subject_matches(subjects: list[object], oci_digest: str) -> bool:
    expected = oci_digest.removeprefix("sha256:")
    return any(
        isinstance(subject, dict)
        and isinstance(subject.get("digest"), dict)
        and subject["digest"].get("sha256") == expected
        for subject in subjects
    )


def _predicate_matches(predicate: dict[str, Any], resolution: dict[str, Any]) -> bool:
    build_definition = predicate.get("buildDefinition")
    run_details = predicate.get("runDetails")
    if not isinstance(build_definition, dict) or not isinstance(run_details, dict):
        return False
    external = build_definition.get("externalParameters")
    builder = run_details.get("builder")
    expected_external = {
        "assistant_id": resolution["assistant_id"],
        "version": resolution["assistant_version"],
        "source_digest": resolution["source_digest"],
        "manifest_digest": resolution["manifest_digest"],
        "machine_contract_digest": resolution["machine_contract_digest"],
        "pack_digest": resolution["pack_digest"],
    }
    return (
        build_definition.get("buildType") == "https://shimpz.com/build-types/assistant/v1"
        and external == expected_external
        and isinstance(builder, dict)
        and builder.get("id") == SIGNER_IDENTITY
    )
