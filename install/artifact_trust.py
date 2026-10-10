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
import urllib.parse
from pathlib import Path
from typing import Any

import docker

from protocol.http.v1 import payload as http_payload

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
_IN_TOTO_PAYLOAD = "application/vnd.in-toto+json"
_PROVENANCE_STATEMENT = "https://in-toto.io/Statement/v0.1"
_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
# GHCR answers a blob read with a redirect to its storage CDN; only that exact host may serve bundle bytes.
_BLOB_HOST = "pkg-containers.githubusercontent.com"
_REDIRECT_STATUSES = frozenset({302, 307})
_BUNDLE_FILE = "bundle.sigstore.json"
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
        trust_root: Path,
    ) -> None:
        self._docker = docker_client
        self._binary = binary
        self._container_id = _self_container_id() if container_id is None else container_id
        self._trust_root = _ensure_private_directory(trust_root)

    def verify(self, resolution: dict[str, Any]) -> None:
        if resolution["trust"]["signer_identity"] != SIGNER_IDENTITY:
            raise ArtifactTrustError("Assistant signer identity is not trusted")
        oci_digest = resolution["oci_digest"]
        if http_payload.SOURCE_DIGEST_RE.fullmatch(oci_digest) is None:
            raise ArtifactTrustError("Assistant OCI digest is invalid")
        signature_digest = _trust_digest(resolution["trust"]["signature_reference"])
        provenance_digest = _trust_digest(resolution["trust"]["provenance_reference"])
        token = _pull_token()
        # Each recorded bundle is read once by content address and Cosign verifies exactly those local bytes, so no
        # mutable registry state (the referrers index included) can substitute another bundle after the read.
        signature = _recorded_bundle(token, signature_digest, oci_digest, SIGNATURE_PREDICATE)
        provenance = _recorded_bundle(token, provenance_digest, oci_digest, PROVENANCE_PREDICATE)
        _bound_statement(signature, oci_digest, SIGNATURE_PREDICATE)
        _verify_provenance(_bound_statement(provenance, oci_digest, PROVENANCE_PREDICATE), resolution)
        # The statements above are trusted only because Cosign now proves those exact bytes.
        self._cosign_verify(signature, oci_digest, SIGNATURE_PREDICATE)
        self._cosign_verify(provenance, oci_digest, PROVENANCE_PREDICATE)

    def _cosign_verify(self, bundle: bytes, oci_digest: str, predicate_type: str) -> None:
        """Prove one local bundle's signer, issuer, transparency evidence, predicate type and subject digest."""
        self._run_cosign(
            bundle,
            (
                "verify-blob-attestation",
                "--certificate-identity",
                SIGNER_IDENTITY,
                "--certificate-oidc-issuer",
                OIDC_ISSUER,
                "--type",
                predicate_type,
                "--digest",
                oci_digest.removeprefix("sha256:"),
                "--digestAlg",
                "sha256",
            ),
        )

    def _run_cosign(self, bundle: bytes, arguments: tuple[str, ...]) -> None:
        with tempfile.TemporaryDirectory(prefix="shimpz-cosign-") as directory:
            bundle_path = Path(directory, _BUNDLE_FILE)
            descriptor = os.open(bundle_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(bundle)
            try:
                execution = self._docker.api.exec_create(
                    container=self._container_id,
                    cmd=[
                        "/usr/bin/timeout",
                        "--kill-after=5s",
                        f"{_TIMEOUT_SECONDS}s",
                        self._binary,
                        *arguments,
                        "--bundle",
                        str(bundle_path),
                    ],
                    stdout=True,
                    stderr=True,
                    stdin=False,
                    tty=False,
                    user="10001",
                    environment=[
                        f"HOME={directory}",
                        f"PATH={os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
                        f"TUF_ROOT={self._trust_root}",
                        *_RELEASE_PROXY_ENVIRONMENT,
                    ],
                )
                self._await_cosign(execution["Id"])
            except (KeyError, TypeError, docker.errors.DockerException) as exc:
                raise ArtifactTrustError("Cosign verification is unavailable") from exc

    def _await_cosign(self, execution_id: str) -> None:
        started = time.monotonic()
        output_bytes = 0
        for stdout, stderr in self._docker.api.exec_start(execution_id, stream=True, demux=True):
            if time.monotonic() - started > _TIMEOUT_SECONDS:
                raise ArtifactTrustError("Cosign verification timed out")
            output_bytes += len(stdout or b"") + len(stderr or b"")
            if output_bytes > _MAX_OUTPUT_BYTES:
                raise ArtifactTrustError("Cosign verification output is too large")
        result = self._docker.api.exec_inspect(execution_id)
        if result.get("ExitCode") in _TIMEOUT_EXIT_CODES:
            raise ArtifactTrustError("Cosign verification timed out")
        if result.get("ExitCode") != 0:
            raise ArtifactTrustError("Cosign verification failed")


def _recorded_bundle(token: str, digest: str, oci_digest: str, predicate: str) -> bytes:
    """Read one recorded bundle manifest and its single bundle layer, each by its content address."""
    manifest = _json_document(
        _registry_read(f"/v2/{_TRUST_NAME}/manifests/{digest}", accept=_OCI_MANIFEST, token=token, digest=digest)
    )
    if not _bundle_matches(manifest, oci_digest, predicate):
        raise ArtifactTrustError("Sigstore bundle does not match the Assistant digest")
    layers = manifest.get("layers")
    if not isinstance(layers, list) or len(layers) != 1:
        raise ArtifactTrustError("Sigstore bundle manifest must carry exactly one bundle layer")
    layer = _descriptor_digest(layers[0])
    if layers[0].get("mediaType") != _BUNDLE_ARTIFACT_TYPE:
        raise ArtifactTrustError("Sigstore bundle manifest must carry exactly one bundle layer")
    return _registry_read(f"/v2/{_TRUST_NAME}/blobs/{layer}", accept=_BUNDLE_ARTIFACT_TYPE, token=token, digest=layer)


def _trust_digest(reference: str) -> str:
    digest = reference.removeprefix(f"{TRUST_REPOSITORY}@")
    if digest == reference or http_payload.SOURCE_DIGEST_RE.fullmatch(digest) is None:
        raise ArtifactTrustError("Sigstore bundle is outside the trust repository")
    return digest


def _pull_token() -> str:
    value = _json_document(
        _registry_read(
            f"/token?scope=repository:{_TRUST_NAME}:pull&service={_REGISTRY_HOST}",
            accept="application/json",
        )
    )
    token = value.get("token") if isinstance(value, dict) else None
    if not isinstance(token, str) or _BEARER_TOKEN_RE.fullmatch(token) is None:
        raise ArtifactTrustError("Sigstore bundle registry token is unavailable")
    return token


def _descriptor_digest(descriptor: object) -> str:
    """Return one OCI descriptor's digest, which must be a full SHA-256 content address."""
    digest = descriptor.get("digest") if isinstance(descriptor, dict) else None
    if not isinstance(digest, str) or http_payload.SOURCE_DIGEST_RE.fullmatch(digest) is None:
        raise ArtifactTrustError("Sigstore bundle descriptor is invalid")
    return digest


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


def _bundle_statement(bundle: bytes) -> dict[str, Any]:
    """Decode the in-toto statement of a v0.3 DSSE bundle, refusing duplicate keys any verifier could read apart."""
    document = _json_document(bundle)
    if not isinstance(document, dict) or document.get("mediaType") != _BUNDLE_ARTIFACT_TYPE:
        raise ArtifactTrustError("Sigstore bundle is not a DSSE in-toto bundle")
    envelope = document.get("dsseEnvelope")
    if (
        not isinstance(envelope, dict)
        or envelope.get("payloadType") != _IN_TOTO_PAYLOAD
        or not isinstance(envelope.get("payload"), str)
    ):
        raise ArtifactTrustError("Sigstore bundle is not a DSSE in-toto bundle")
    try:
        statement = _json_document(base64.b64decode(envelope["payload"], validate=True))
    except binascii.Error as exc:
        raise ArtifactTrustError("Sigstore bundle is not a DSSE in-toto bundle") from exc
    if not isinstance(statement, dict):
        raise ArtifactTrustError("Sigstore bundle is not a DSSE in-toto bundle")
    return statement


def _bound_statement(bundle: bytes, oci_digest: str, predicate_type: str) -> dict[str, Any]:
    statement = _bundle_statement(bundle)
    if statement.get("predicateType") != predicate_type or not _subject_matches(statement.get("subject"), oci_digest):
        raise ArtifactTrustError("Sigstore bundle does not match the Assistant digest")
    return statement


def _registry_read(path: str, *, accept: str, token: str | None = None, digest: str | None = None) -> bytes:
    """Read one bounded registry document through the exact-allowlisted release proxy."""
    headers = {"Accept": accept}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    status, location, raw = _tunnelled_get(_REGISTRY_HOST, path, headers)
    if digest is not None and status in _REDIRECT_STATUSES:
        # Only a content-addressed read follows the one storage redirect; its bytes are bound by the digest below,
        # and the registry bearer never leaves the registry host.
        status, _location, raw = _tunnelled_get(_BLOB_HOST, _blob_location(location), {"Accept": accept})
    if status != 200:
        raise ArtifactTrustError("Sigstore bundle registry refused the request")
    if digest is not None and f"sha256:{hashlib.sha256(raw).hexdigest()}" != digest:
        raise ArtifactTrustError("Sigstore bundle content does not match its digest")
    return raw


def _tunnelled_get(host: str, path: str, headers: dict[str, str]) -> tuple[int, str | None, bytes]:
    connection = http.client.HTTPSConnection(
        _RELEASE_PROXY_HOST,
        _RELEASE_PROXY_PORT,
        timeout=_REGISTRY_TIMEOUT_SECONDS,
        context=_TLS_CONTEXT,
    )
    connection.set_tunnel(host, 443)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        raw = response.read(_MAX_REGISTRY_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise ArtifactTrustError("Sigstore bundle registry is unavailable") from exc
    finally:
        connection.close()
    if len(raw) > _MAX_REGISTRY_BYTES:
        raise ArtifactTrustError("Sigstore bundle registry response is too large")
    return response.status, response.getheader("Location"), raw


def _blob_location(location: str | None) -> str:
    parts = urllib.parse.urlsplit(location or "")
    if parts.scheme != "https" or parts.netloc != _BLOB_HOST or not parts.path.startswith("/"):
        raise ArtifactTrustError("Sigstore bundle registry redirect is not trusted")
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


def _json_document(raw: bytes) -> object:
    try:
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise ArtifactTrustError("Sigstore bundle registry returned an invalid document") from exc


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate JSON object key")
    return value


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


def _verify_provenance(statement: dict[str, Any], resolution: dict[str, Any]) -> None:
    predicate = statement.get("predicate")
    if not (
        statement.get("_type") == _PROVENANCE_STATEMENT
        and isinstance(predicate, dict)
        and _predicate_matches(predicate, resolution)
    ):
        raise ArtifactTrustError("signed provenance does not match the Assistant publication")


def _subject_matches(subjects: object, oci_digest: str) -> bool:
    expected = oci_digest.removeprefix("sha256:")
    return isinstance(subjects, list) and any(
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
