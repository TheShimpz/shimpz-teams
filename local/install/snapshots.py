"""Team-owned admission for unpublished Assistant images staged in Local Docker."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from docker.errors import DockerException, ImageNotFound

from assistant import manifest as assistant_manifest
from install import bindings
from local.install import source_package

LOCAL_STAGE_LABEL = "org.shimpz.local.stage"
LOCAL_STAGE_VALUE = "assistant-v1"
ASSISTANT_LABEL = "org.shimpz.assistant.id"
SOURCE_LABEL = "org.shimpz.source.digest"
VERSION_LABEL = "org.shimpz.assistant.version"
BUILD_LABEL = "org.shimpz.local.build.digest"
SOURCE_PATH = "/opt/shimpz/.shimpz/source.package"
ICON_PATH = "/opt/shimpz/icon.png"
RUNTIME_USER = "10001:10001"
RUNTIME_ENTRYPOINT = ["/opt/shimpz/runtime/bin/python3.14", "-c", "import signal; signal.pause()"]
MAX_CANDIDATES = 50
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CREATED_RE = re.compile(r"^[0-9TZ:+.-]{20,64}$")
_PLATFORMS = {"amd64": "linux/amd64", "x86_64": "linux/amd64", "arm64": "linux/arm64", "aarch64": "linux/arm64"}
_RECORD_FIELDS = {
    "version",
    "assistant_id",
    "assistant_version",
    "name",
    "summary",
    "image_id",
    "platform",
    "source_digest",
    "manifest_digest",
    "machine_contract_digest",
    "icon_digest",
    "runtime",
    "allowed_hosts",
    "integrations",
    "stored_inputs",
    "machine_contract",
}


class LocalSnapshotError(bindings.DynamicAssistantError):
    """A staged Local Assistant image is unavailable or violates admission."""


@dataclass(frozen=True, slots=True)
class LocalSnapshotCandidate:
    assistant_id: str
    version: str
    image_id: str
    platform: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AdmittedLocalSnapshot:
    record: dict[str, Any]
    icon: bytes


def list_candidates(client) -> tuple[LocalSnapshotCandidate, ...]:
    """Return only bounded stage-labeled images, never general daemon inventory."""
    try:
        images = client.images.list(
            all=True,
            filters={"label": [f"{LOCAL_STAGE_LABEL}={LOCAL_STAGE_VALUE}"]},
        )
    except DockerException as exc:
        raise LocalSnapshotError("Docker cannot enumerate Local Assistant snapshots") from exc
    if not isinstance(images, list) or len(images) > MAX_CANDIDATES:
        raise LocalSnapshotError("the Local Assistant snapshot inventory is invalid or too large")
    platform = _daemon_platform(client)
    candidates = tuple(_candidate(image, platform) for image in images)
    if len({candidate.image_id for candidate in candidates}) != len(candidates):
        raise LocalSnapshotError("the Local Assistant snapshot inventory contains duplicate images")
    return tuple(sorted(candidates, key=lambda value: (value.assistant_id, value.version, value.image_id)))


def admit(client, image_id: str) -> AdmittedLocalSnapshot:
    """Derive one closed local record from an exact staged image without starting it."""
    if not isinstance(image_id, str) or _IMAGE_ID_RE.fullmatch(image_id) is None:
        raise LocalSnapshotError("the Local Assistant image id is invalid")
    image = _exact_image(client, image_id)
    platform = _daemon_platform(client)
    candidate = _candidate(image, platform)
    extracted = _extract_files(client, image_id)
    try:
        package = source_package.admit(extracted[SOURCE_PATH])
        if package.digest != _labels(image)[SOURCE_LABEL]:
            raise LocalSnapshotError("the Local Assistant source package digest does not match its image")
        if package.manifest != extracted[assistant_manifest.MANIFEST_PATH] or package.icon != extracted[ICON_PATH]:
            raise LocalSnapshotError("the Local Assistant files do not match its source package")
        record = _record(candidate, package, extracted[assistant_manifest.CONTRACT_PATH])
    except LocalSnapshotError:
        raise
    except (source_package.SourcePackageError, assistant_manifest.ManifestError) as exc:
        raise LocalSnapshotError("the Local Assistant declaration is invalid") from exc
    validate_record(record)
    return AdmittedLocalSnapshot(record=record, icon=package.icon)


def validate_record(record: dict[str, Any]) -> None:
    """Validate the closed Team-owned local binding record after every durable read."""
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS or record.get("version") != 1:
        raise LocalSnapshotError("the local Assistant record has an unsupported shape")
    try:
        identity = assistant_manifest.canonical_manifest_identity(
            assistant_id=record["assistant_id"],
            version=record["assistant_version"],
            name=record["name"],
            summary=record["summary"],
        )
        declarations = _integration_declarations(record["integrations"])
        stored_inputs = _stored_input_declarations(record["stored_inputs"])
        contract = assistant_manifest.canonical_manifest_contract(
            allowed_hosts=record["allowed_hosts"],
            integration_declarations={declaration.id: list(declaration.scopes) for declaration in declarations},
            stored_input_declarations={
                declaration.id: {
                    "kind": declaration.kind,
                    "label": declaration.label,
                    "description": declaration.description,
                }
                for declaration in stored_inputs
            },
        )
        machine_contract = assistant_manifest.canonical_machine_contract(
            record["machine_contract"], declarations, stored_inputs
        )
    except (KeyError, TypeError, assistant_manifest.ManifestError) as exc:
        raise LocalSnapshotError("the local Assistant record is invalid") from exc
    _validate_record_primitives(record, identity, contract, declarations, stored_inputs, machine_contract)


def _candidate(image, platform: str) -> LocalSnapshotCandidate:
    try:
        image.reload()
    except DockerException as exc:
        raise LocalSnapshotError("Docker cannot inspect a Local Assistant snapshot") from exc
    attrs = image.attrs
    labels = _labels(image)
    image_id = image.id
    created = attrs.get("Created")
    if (
        not isinstance(image_id, str)
        or _IMAGE_ID_RE.fullmatch(image_id) is None
        or attrs.get("Id") != image_id
        or attrs.get("Architecture") != platform.rpartition("/")[2]
        or attrs.get("RepoDigests") != []
        or attrs.get("RepoTags") != []
        or not isinstance(created, str)
        or _CREATED_RE.fullmatch(created) is None
    ):
        raise LocalSnapshotError("the Local Assistant snapshot identity is invalid")
    config = attrs.get("Config")
    if (
        not isinstance(config, dict)
        or config.get("User") != RUNTIME_USER
        or config.get("Entrypoint") != RUNTIME_ENTRYPOINT
        or config.get("Cmd") not in (None, [])
    ):
        raise LocalSnapshotError("the Local Assistant snapshot runtime is invalid")
    try:
        identity = assistant_manifest.canonical_manifest_identity(
            assistant_id=labels[ASSISTANT_LABEL],
            version=labels[VERSION_LABEL],
            name=labels[ASSISTANT_LABEL],
            summary="Unpublished Local Assistant snapshot",
        )
    except (KeyError, assistant_manifest.ManifestError) as exc:
        raise LocalSnapshotError("the Local Assistant snapshot labels are invalid") from exc
    if (
        labels.get(LOCAL_STAGE_LABEL) != LOCAL_STAGE_VALUE
        or _IMAGE_ID_RE.fullmatch(str(labels.get(SOURCE_LABEL))) is None
        or _IMAGE_ID_RE.fullmatch(str(labels.get(BUILD_LABEL))) is None
    ):
        raise LocalSnapshotError("the Local Assistant snapshot labels are invalid")
    return LocalSnapshotCandidate(identity.assistant_id, identity.version, image_id, platform, created)


def _exact_image(client, image_id: str):
    try:
        image = client.images.get(image_id)
    except ImageNotFound as exc:
        raise LocalSnapshotError("the Local Assistant snapshot is no longer available") from exc
    except DockerException as exc:
        raise LocalSnapshotError("Docker cannot resolve the Local Assistant snapshot") from exc
    if image.id != image_id:
        raise LocalSnapshotError("Docker did not resolve the exact Local Assistant image id")
    return image


def _daemon_platform(client) -> str:
    try:
        info = client.info()
    except DockerException as exc:
        raise LocalSnapshotError("Docker cannot report its Local Assistant platform") from exc
    architecture = info.get("Architecture") if isinstance(info, dict) else None
    try:
        return _PLATFORMS[architecture]
    except (KeyError, TypeError) as exc:
        raise LocalSnapshotError("the Docker daemon architecture is unsupported") from exc


def _labels(image) -> dict[str, str]:
    attrs = image.attrs
    config = attrs.get("Config") if isinstance(attrs, dict) else None
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
    ):
        raise LocalSnapshotError("the Local Assistant snapshot labels are invalid")
    return labels


def _extract_files(client, image_id: str) -> dict[str, bytes]:
    container = None
    failure: Exception | None = None
    extracted: dict[str, bytes] | None = None
    try:
        container = client.containers.create(image=image_id, network_mode="none")
        extracted = {
            SOURCE_PATH: assistant_manifest.read_container_file(
                container,
                path=SOURCE_PATH,
                name="source.package",
                maximum=32 * 1024 * 1024,
            ),
            assistant_manifest.MANIFEST_PATH: assistant_manifest.read_container_file(
                container,
                path=assistant_manifest.MANIFEST_PATH,
                name="shimpz.toml",
                maximum=assistant_manifest.MAX_MANIFEST_BYTES,
            ),
            assistant_manifest.CONTRACT_PATH: assistant_manifest.read_container_file(
                container,
                path=assistant_manifest.CONTRACT_PATH,
                name="shimpz.contract.json",
                maximum=assistant_manifest.MAX_CONTRACT_BYTES,
            ),
            ICON_PATH: assistant_manifest.read_container_file(
                container,
                path=ICON_PATH,
                name="icon.png",
                maximum=1024 * 1024,
            ),
        }
    except (DockerException, assistant_manifest.ManifestError) as exc:
        failure = exc
    cleanup_failure = _remove_temporary_container(container)
    if cleanup_failure is not None:
        raise LocalSnapshotError("the Local Assistant admission container could not be removed") from cleanup_failure
    if failure is not None:
        raise LocalSnapshotError("the Local Assistant files could not be admitted") from failure
    if extracted is None:
        raise LocalSnapshotError("the Local Assistant files could not be admitted")
    return extracted


def _remove_temporary_container(container) -> Exception | None:
    if container is None:
        return None
    try:
        container.remove(force=True, v=False)
    except DockerException as exc:
        return exc
    return None


def _record(
    candidate: LocalSnapshotCandidate,
    package: source_package.SourcePackage,
    raw_contract: bytes,
) -> dict[str, Any]:
    identity = assistant_manifest.parse_manifest_identity(package.manifest)
    if (identity.assistant_id, identity.version) != (candidate.assistant_id, candidate.version):
        raise LocalSnapshotError("the Local Assistant manifest does not match its image labels")
    manifest_contract = assistant_manifest.parse_manifest_contract(package.manifest)
    machine_contract = assistant_manifest.parse_machine_contract(
        raw_contract,
        manifest_contract.integrations,
        manifest_contract.stored_inputs,
    )
    return {
        "version": 1,
        "assistant_id": identity.assistant_id,
        "assistant_version": identity.version,
        "name": identity.name,
        "summary": identity.summary,
        "image_id": candidate.image_id,
        "platform": candidate.platform,
        "source_digest": package.digest,
        "manifest_digest": _digest(package.manifest),
        "machine_contract_digest": _digest(raw_contract),
        "icon_digest": _digest(package.icon),
        "runtime": {"user": RUNTIME_USER, "entrypoint": RUNTIME_ENTRYPOINT},
        "allowed_hosts": list(manifest_contract.allowed_hosts),
        "integrations": [
            {"id": value.id, "provider": value.provider, "scopes": list(value.scopes)}
            for value in manifest_contract.integrations
        ],
        "stored_inputs": [
            {
                "id": value.id,
                "kind": value.kind,
                "label": value.label,
                "description": value.description,
            }
            for value in manifest_contract.stored_inputs
        ],
        "machine_contract": machine_contract,
    }


def _integration_declarations(value: object) -> tuple[assistant_manifest.IntegrationDeclaration, ...]:
    if not isinstance(value, list):
        raise LocalSnapshotError("the local Assistant Integrations are invalid")
    try:
        declarations = tuple(
            assistant_manifest.IntegrationDeclaration(
                id=item["id"],
                provider=item["provider"],
                scopes=tuple(item["scopes"]),
            )
            for item in value
            if isinstance(item, dict) and set(item) == {"id", "provider", "scopes"}
        )
    except (KeyError, TypeError) as exc:
        raise LocalSnapshotError("the local Assistant Integrations are invalid") from exc
    if len(declarations) != len(value):
        raise LocalSnapshotError("the local Assistant Integrations are invalid")
    return declarations


def _stored_input_declarations(value: object) -> tuple[assistant_manifest.StoredInputDeclaration, ...]:
    if not isinstance(value, list):
        raise LocalSnapshotError("the local Assistant Stored Inputs are invalid")
    try:
        declarations = tuple(
            assistant_manifest.StoredInputDeclaration(
                id=item["id"],
                kind=item["kind"],
                label=item["label"],
                description=item["description"],
            )
            for item in value
            if isinstance(item, dict) and set(item) == {"id", "kind", "label", "description"}
        )
    except (KeyError, TypeError) as exc:
        raise LocalSnapshotError("the local Assistant Stored Inputs are invalid") from exc
    if len(declarations) != len(value):
        raise LocalSnapshotError("the local Assistant Stored Inputs are invalid")
    return declarations


def _validate_record_primitives(
    record: dict[str, Any],
    identity: assistant_manifest.ManifestIdentity,
    contract: assistant_manifest.ManifestContract,
    declarations: tuple[assistant_manifest.IntegrationDeclaration, ...],
    stored_inputs: tuple[assistant_manifest.StoredInputDeclaration, ...],
    machine_contract: dict[str, Any],
) -> None:
    expected_runtime = {"user": RUNTIME_USER, "entrypoint": RUNTIME_ENTRYPOINT}
    digests = ("image_id", "source_digest", "manifest_digest", "machine_contract_digest", "icon_digest")
    if (
        record["assistant_id"] != identity.assistant_id
        or record["assistant_version"] != identity.version
        or record["name"] != identity.name
        or record["summary"] != identity.summary
        or record["platform"] not in set(_PLATFORMS.values())
        or record["runtime"] != expected_runtime
        or record["allowed_hosts"] != list(contract.allowed_hosts)
        or declarations != contract.integrations
        or stored_inputs != contract.stored_inputs
        or record["integrations"]
        != [{"id": value.id, "provider": value.provider, "scopes": list(value.scopes)} for value in declarations]
        or record["stored_inputs"]
        != [
            {
                "id": value.id,
                "kind": value.kind,
                "label": value.label,
                "description": value.description,
            }
            for value in stored_inputs
        ]
        or record["machine_contract"] != machine_contract
        or any(not isinstance(record[key], str) or _IMAGE_ID_RE.fullmatch(record[key]) is None for key in digests)
    ):
        raise LocalSnapshotError("the local Assistant record is invalid")


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"
