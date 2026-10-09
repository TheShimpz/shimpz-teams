"""Team-owned admission for unpublished Assistant images staged in Local Docker."""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from docker.errors import DockerException, ImageNotFound, NotFound

from assistant import details as assistant_details
from assistant import language as assistant_language
from assistant import manifest as assistant_manifest
from install import bindings
from local.install import source_package
from protocol.http.v1 import payload as http_payload

LOCAL_STAGE_LABEL = "org.shimpz.local.stage"
LOCAL_STAGE_VALUE = "assistant-v3"
ASSISTANT_LABEL = "org.shimpz.assistant.id"
SOURCE_LABEL = "org.shimpz.source.digest"
VERSION_LABEL = "org.shimpz.assistant.version"
NAME_LABEL = "org.shimpz.assistant.name"
SUMMARY_LABEL = "org.shimpz.assistant.summary"
DECLARED_CREATORS_LABEL = "org.shimpz.assistant.declared-creators"
BUILD_LABEL = "org.shimpz.local.build.digest"
ACTIONS_LABEL = "org.shimpz.assistant.actions"
INTEGRATIONS_LABEL = "org.shimpz.assistant.integrations"
# Each Assistant's current snapshot carries exactly this tag; a stage-labeled image without it is superseded.
LOCAL_SNAPSHOT_REPOSITORY = "shimpz-local"
LOCAL_SNAPSHOT_TAG = "staged"
SOURCE_PATH = "/opt/shimpz/.shimpz/source.package"
ICON_PATH = "/opt/shimpz/icon.png"
RUNTIME_USER = "10001:10001"
RUNTIME_ENTRYPOINT = ["/opt/shimpz/runtime/bin/python3.14", "-c", "import signal; signal.pause()"]
MAX_CANDIDATES = 50
_CREATED_RE = re.compile(r"^[0-9TZ:+.-]{20,64}$")
_PLATFORMS = {"amd64": "linux/amd64", "x86_64": "linux/amd64", "arm64": "linux/arm64", "aarch64": "linux/arm64"}
_RECORD_FIELDS = {
    "version",
    "assistant_id",
    "assistant_version",
    "name",
    "summary",
    "description",
    "links",
    "declared_creators",
    "image_id",
    "platform",
    "source_digest",
    "manifest_digest",
    "machine_contract_digest",
    "icon_digest",
    "pack_digest",
    "runtime",
    "allowed_hosts",
    "integrations",
    "stored_inputs",
    "machine_contract",
}


class LocalSnapshotError(bindings.DynamicAssistantError):
    """A staged Local Assistant image is unavailable or violates admission."""


class LocalSnapshotUnavailableError(LocalSnapshotError):
    """Docker could not complete a Local snapshot operation."""


class LocalSnapshotAbsentError(LocalSnapshotUnavailableError):
    """The exact Local snapshot image is no longer present."""


class InvalidLabeledSnapshotError(LocalSnapshotError):
    """A canonically identified stage-labeled image failed validation."""


@dataclass(frozen=True, slots=True)
class LocalSnapshotCandidate:
    assistant_id: str
    version: str
    name: str
    summary: str
    declared_creators: tuple[str, ...]
    actions: tuple[str, ...]
    integrations: tuple[str, ...]
    image_id: str
    platform: str
    created_at: str


@dataclass(frozen=True, slots=True)
class SnapshotPreview:
    """A staged image's validated icon and its Assistant page in every interface language, read without starting it."""

    icon: bytes
    # Interface language to the closed Assistant details object; its summary is the localized summary.
    details: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class AdmittedLocalSnapshot:
    record: dict[str, Any]
    icon: bytes


def canonical_reference(assistant_id: str) -> str:
    """Return the one tag that makes a staged image its Assistant's current snapshot."""
    return f"{LOCAL_SNAPSHOT_REPOSITORY}/{assistant_id}:{LOCAL_SNAPSHOT_TAG}"


def local_repo_digests_valid(repo_digests: object, assistant_id: str, image_id: str) -> bool:
    """Accept only a never-pulled snapshot: no digest, or the containerd store's digest of this exact local image."""
    return repo_digests in ([], [f"{LOCAL_SNAPSHOT_REPOSITORY}/{assistant_id}@{image_id}"])


def list_candidates(client, *, platform: str | None = None) -> tuple[LocalSnapshotCandidate, ...]:
    """Return only bounded current snapshots, never general daemon inventory.

    The reference filter reads Docker's name index, so discovery never scans every image's configuration.
    """
    try:
        summaries = client.api.images(filters={"reference": [f"{LOCAL_SNAPSHOT_REPOSITORY}/*:{LOCAL_SNAPSHOT_TAG}"]})
    except DockerException as exc:
        raise LocalSnapshotUnavailableError("Docker cannot enumerate Local Assistant snapshots") from exc
    if not isinstance(summaries, list) or len(summaries) > MAX_CANDIDATES:
        raise LocalSnapshotError("the Local Assistant snapshot inventory is invalid or too large")
    platform = _daemon_platform(client) if platform is None else platform
    candidates = []
    for summary in summaries:
        image = None
        try:
            image = _summary_image(client, summary)
            candidates.append(_candidate(image, platform))
        except LocalSnapshotUnavailableError:
            raise
        except LocalSnapshotError as exc:
            image_id = getattr(image, "id", None)
            if isinstance(image_id, str) and http_payload.SOURCE_DIGEST_RE.fullmatch(image_id) is not None:
                raise InvalidLabeledSnapshotError(
                    f"Local Assistant snapshot {image_id} carries the Local stage label but failed validation"
                ) from exc
            raise
    candidates = tuple(candidates)
    if len({candidate.image_id for candidate in candidates}) != len(candidates):
        raise LocalSnapshotError("the Local Assistant snapshot inventory contains duplicate images")
    return tuple(sorted(candidates, key=lambda value: (value.assistant_id, value.version, value.image_id)))


def admit(client, image_id: str) -> AdmittedLocalSnapshot:
    """Derive one closed local record from an exact staged image without starting it."""
    if not isinstance(image_id, str) or http_payload.SOURCE_DIGEST_RE.fullmatch(image_id) is None:
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
        record = _record(
            candidate,
            package,
            extracted[assistant_manifest.CONTRACT_PATH],
            extracted[assistant_language.PACK_PATH],
        )
    except LocalSnapshotError:
        raise
    except (source_package.SourcePackageError, assistant_manifest.ManifestError) as exc:
        raise LocalSnapshotError("the Local Assistant declaration is invalid") from exc
    validate_record(record)
    return AdmittedLocalSnapshot(record=record, icon=package.icon)


def require_candidate(
    client,
    image_id: str,
    *,
    platform: str | None = None,
) -> LocalSnapshotCandidate:
    """Return one exact staged candidate after validating its immutable identity."""
    if not isinstance(image_id, str) or http_payload.SOURCE_DIGEST_RE.fullmatch(image_id) is None:
        raise LocalSnapshotError("the Local Assistant image id is invalid")
    image = _exact_image(client, image_id)
    return _candidate(image, _daemon_platform(client) if platform is None else platform)


def preview(client, image_id: str, *, platform: str | None = None) -> SnapshotPreview:
    """Return the validated icon and localized Assistant page of an exact staged image without starting it.

    Every displayed text in every non-English interface language is read only from the image's own pack, admitted
    complete for the image's own catalog under Local's unsigned self-consistency trust (ADR-0060, ADR-0091); English is
    the catalog text itself, and no request message is ever read.
    """
    candidate = require_candidate(client, image_id, platform=platform)
    extracted = _extract_preview_files(client, image_id)
    try:
        manifest = extracted[assistant_manifest.MANIFEST_PATH]
        identity = assistant_manifest.parse_manifest_identity(manifest)
        creators = assistant_manifest.parse_manifest_creators(manifest)[:4]
        source_package.validate_icon(extracted[ICON_PATH])
        presentation = assistant_manifest.parse_manifest_presentation(manifest)
        details = _preview_details(identity, presentation, creators, manifest, extracted)
    except (source_package.SourcePackageError, assistant_manifest.ManifestError) as exc:
        raise LocalSnapshotError("the Local Assistant preview is invalid") from exc
    if (
        identity.assistant_id != candidate.assistant_id
        or identity.version != candidate.version
        or identity.name != candidate.name
        or identity.summary != candidate.summary
        or creators != candidate.declared_creators
    ):
        raise LocalSnapshotError("the Local Assistant preview does not match its image labels")
    return SnapshotPreview(icon=extracted[ICON_PATH], details=details)


def _preview_details(
    identity: assistant_manifest.ManifestIdentity,
    presentation: assistant_manifest.ManifestPresentation,
    creators: tuple[str, ...],
    manifest: bytes,
    extracted: dict[str, bytes],
) -> Mapping[str, Mapping[str, object]]:
    contract = assistant_manifest.parse_manifest_contract(manifest)
    machine_contract = assistant_manifest.parse_machine_contract(
        extracted[assistant_manifest.CONTRACT_PATH],
        contract.integrations,
        contract.stored_inputs,
        summary=identity.summary,
        description=presentation.description,
        allowed_hosts=contract.allowed_hosts,
    )
    raw_pack = extracted[assistant_language.PACK_PATH]
    pack = assistant_language.admit_pack(raw_pack, machine_contract["messages"], _digest(raw_pack))
    page = assistant_details.AssistantPage(
        assistant_id=identity.assistant_id,
        version=identity.version,
        name=identity.name,
        creators=creators,
        summary=identity.summary,
        description=presentation.description,
        links=presentation.links,
        machine_contract=machine_contract,
        integrations={declaration.id: declaration.provider for declaration in contract.integrations},
        stored_inputs={declaration.id: declaration for declaration in contract.stored_inputs},
    )
    return MappingProxyType(
        {locale: MappingProxyType(page.localized(locale, pack)) for locale in sorted(http_payload.CHAT_LOCALES)}
    )


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
        presentation = assistant_manifest.canonical_manifest_presentation(
            description=record["description"], links=record["links"]
        )
        # The first four self-declared Creator handles: unverified Local presentation, never identity authority.
        assistant_manifest.canonical_manifest_creators(record["declared_creators"], maximum=4)
        declarations = _integration_declarations(record["integrations"])
        stored_inputs = _stored_input_declarations(record["stored_inputs"])
        contract = assistant_manifest.canonical_manifest_contract(
            allowed_hosts=record["allowed_hosts"],
            integration_declarations={declaration.id: list(declaration.scopes) for declaration in declarations},
            stored_input_declarations={declaration.id: declaration.metadata() for declaration in stored_inputs},
        )
        machine_contract = assistant_manifest.canonical_machine_contract(
            record["machine_contract"],
            declarations,
            stored_inputs,
            summary=identity.summary,
            description=presentation.description,
            allowed_hosts=contract.allowed_hosts,
        )
    except (KeyError, TypeError, assistant_manifest.ManifestError) as exc:
        raise LocalSnapshotError("the local Assistant record is invalid") from exc
    _validate_record_primitives(record, identity, contract, declarations, stored_inputs, machine_contract)


def _candidate(image, platform: str) -> LocalSnapshotCandidate:
    attrs = image.attrs
    labels = _labels(image)
    image_id = image.id
    created = attrs.get("Created")
    if (
        not isinstance(image_id, str)
        or http_payload.SOURCE_DIGEST_RE.fullmatch(image_id) is None
        or attrs.get("Id") != image_id
        or attrs.get("Architecture") != platform.rpartition("/")[2]
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
            name=labels[NAME_LABEL],
            summary=labels[SUMMARY_LABEL],
        )
        declared_creators = assistant_manifest.canonical_manifest_creators(
            labels[DECLARED_CREATORS_LABEL].split(","),
            maximum=4,
        )
        actions = _capability_ids(labels[ACTIONS_LABEL], maximum=128, required=True)
        integrations = _capability_ids(labels[INTEGRATIONS_LABEL], maximum=16, required=False)
    except (KeyError, assistant_manifest.ManifestError) as exc:
        raise LocalSnapshotError("the Local Assistant snapshot labels are invalid") from exc
    if not local_repo_digests_valid(attrs.get("RepoDigests"), identity.assistant_id, image_id):
        raise LocalSnapshotError("the Local Assistant snapshot identity is invalid")
    if attrs.get("RepoTags") != [canonical_reference(identity.assistant_id)]:
        raise LocalSnapshotError("the Local Assistant snapshot is not its Assistant's current snapshot")
    if (
        labels.get(LOCAL_STAGE_LABEL) != LOCAL_STAGE_VALUE
        or http_payload.SOURCE_DIGEST_RE.fullmatch(str(labels.get(SOURCE_LABEL))) is None
        or http_payload.SOURCE_DIGEST_RE.fullmatch(str(labels.get(BUILD_LABEL))) is None
    ):
        raise LocalSnapshotError("the Local Assistant snapshot labels are invalid")
    return LocalSnapshotCandidate(
        identity.assistant_id,
        identity.version,
        identity.name,
        identity.summary,
        declared_creators,
        actions,
        integrations,
        image_id,
        platform,
        created,
    )


def _capability_ids(value: str, *, maximum: int, required: bool) -> tuple[str, ...]:
    values = tuple(value.split(",")) if value else ()
    if (
        (required and not values)
        or len(values) > maximum
        or values != tuple(sorted(set(values)))
        or any(http_payload.canonical_identifier(item) is None for item in values)
    ):
        raise LocalSnapshotError("the Local Assistant capability labels are invalid")
    return values


def _exact_image(client, image_id: str):
    try:
        image = client.images.get(image_id)
    except ImageNotFound as exc:
        raise LocalSnapshotAbsentError("the Local Assistant snapshot is no longer available") from exc
    except DockerException as exc:
        raise LocalSnapshotUnavailableError("Docker cannot resolve the Local Assistant snapshot") from exc
    if image.id != image_id:
        raise LocalSnapshotError("Docker did not resolve the exact Local Assistant image id")
    return image


def _summary_image(client, summary):
    image_id = summary.get("Id") if isinstance(summary, dict) else None
    if not isinstance(image_id, str) or http_payload.SOURCE_DIGEST_RE.fullmatch(image_id) is None:
        raise LocalSnapshotError("the Local Assistant snapshot identity is invalid")
    try:
        return client.images.get(image_id)
    except ImageNotFound as exc:
        raise LocalSnapshotUnavailableError("the Local Assistant snapshot is no longer available") from exc
    except DockerException as exc:
        raise LocalSnapshotUnavailableError("Docker cannot resolve the Local Assistant snapshot") from exc


def _daemon_platform(client) -> str:
    try:
        info = client.info()
    except DockerException as exc:
        raise LocalSnapshotUnavailableError("Docker cannot report its Local Assistant platform") from exc
    return platform_from_info(info)


def platform_from_info(info) -> str:
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


# The package members an install preview reads; installing also extracts the source package before them.
_PREVIEW_MEMBERS = (
    (assistant_manifest.MANIFEST_PATH, "shimpz.toml", assistant_manifest.MAX_MANIFEST_BYTES),
    (assistant_manifest.CONTRACT_PATH, "shimpz.contract.json", assistant_manifest.MAX_CONTRACT_BYTES),
    (assistant_language.PACK_PATH, "shimpz.pack.json", assistant_language.MAX_PACK_BYTES),
    (ICON_PATH, "icon.png", 1024 * 1024),
)


def _extract_files(client, image_id: str) -> dict[str, bytes]:
    return _extract_paths(client, image_id, ((SOURCE_PATH, "source.package", 32 * 1024 * 1024), *_PREVIEW_MEMBERS))


def _extract_preview_files(client, image_id: str) -> dict[str, bytes]:
    return _extract_paths(client, image_id, _PREVIEW_MEMBERS)


def _extract_paths(
    client,
    image_id: str,
    paths: tuple[tuple[str, str, int], ...],
) -> dict[str, bytes]:
    container = None
    failure: Exception | None = None
    extracted: dict[str, bytes] = {}
    try:
        container = client.containers.create(image=image_id, network_mode="none")
        extracted = {
            path: assistant_manifest.read_container_file(
                container,
                path=path,
                name=name,
                maximum=maximum,
            )
            for path, name, maximum in paths
        }
    except (DockerException, assistant_manifest.ManifestError) as exc:
        failure = exc
    cleanup_failure = _remove_temporary_container(container)
    if cleanup_failure is not None:
        raise LocalSnapshotUnavailableError(
            "the Local Assistant admission container could not be removed"
        ) from cleanup_failure
    if failure is not None:
        error = LocalSnapshotUnavailableError if _unavailable(failure) else LocalSnapshotError
        raise error("the Local Assistant files could not be admitted") from failure
    return extracted


def _unavailable(failure: Exception) -> bool:
    """Whether Docker failed to answer, as opposed to the image lacking a file its stage contract requires.

    Docker's 404 for an absent path makes the image inadmissible until it is staged again; any other daemon, transport,
    or archive-stream failure is transient and stays retryable.
    """
    if isinstance(failure, assistant_manifest.ManifestUnavailableError):
        return not isinstance(failure.__cause__, NotFound)
    return isinstance(failure, DockerException)


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
    raw_pack: bytes,
) -> dict[str, Any]:
    identity = assistant_manifest.parse_manifest_identity(package.manifest)
    if (identity.assistant_id, identity.version) != (candidate.assistant_id, candidate.version):
        raise LocalSnapshotError("the Local Assistant manifest does not match its image labels")
    manifest_contract = assistant_manifest.parse_manifest_contract(package.manifest)
    presentation = assistant_manifest.parse_manifest_presentation(package.manifest)
    if assistant_manifest.parse_manifest_creators(package.manifest)[:4] != candidate.declared_creators:
        raise LocalSnapshotError("the Local Assistant manifest does not match its image labels")
    machine_contract = assistant_manifest.parse_machine_contract(
        raw_contract,
        manifest_contract.integrations,
        manifest_contract.stored_inputs,
        summary=identity.summary,
        description=presentation.description,
        allowed_hosts=manifest_contract.allowed_hosts,
    )
    if candidate.actions != tuple(
        action["id"] for action in machine_contract["actions"]
    ) or candidate.integrations != tuple(value.provider for value in manifest_contract.integrations):
        raise LocalSnapshotError("the Local Assistant capability labels do not match its contract")
    # Local admission is unsigned self-consistency (ADR-0060): the pack must be complete and valid for this exact
    # catalog, and the record binds its digest so the started image must carry these exact bytes (ADR-0091).
    pack_digest = assistant_language.admit_pack(raw_pack, machine_contract["messages"], _digest(raw_pack)).pack_digest
    return {
        "version": 1,
        "assistant_id": identity.assistant_id,
        "assistant_version": identity.version,
        "name": identity.name,
        "summary": identity.summary,
        "description": presentation.description,
        "links": dict(presentation.links),
        "declared_creators": list(candidate.declared_creators),
        "image_id": candidate.image_id,
        "platform": candidate.platform,
        "source_digest": package.digest,
        "manifest_digest": _digest(package.manifest),
        "machine_contract_digest": _digest(raw_contract),
        "icon_digest": _digest(package.icon),
        "pack_digest": pack_digest,
        "runtime": {"user": RUNTIME_USER, "entrypoint": RUNTIME_ENTRYPOINT},
        "allowed_hosts": list(manifest_contract.allowed_hosts),
        "integrations": [
            {"id": value.id, "provider": value.provider, "scopes": list(value.scopes)}
            for value in manifest_contract.integrations
        ],
        "stored_inputs": [value.document() for value in manifest_contract.stored_inputs],
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


_STORED_INPUT_REQUIRED = frozenset({"id", "kind", "label", "description", "help_url", "host"})
_STORED_INPUT_FIELDS = frozenset(
    {"id", "kind", "label", "description", "help_url", "host", "header", "query", "scheme", "hmac"}
)


def _stored_input_declarations(value: object) -> tuple[assistant_manifest.StoredInputDeclaration, ...]:
    if not isinstance(value, list):
        raise LocalSnapshotError("the local Assistant Stored Inputs are invalid")
    # Only the closed field set reaches the dataclass, so construction itself cannot fail.
    declarations = tuple(
        assistant_manifest.StoredInputDeclaration(**item)
        for item in value
        if isinstance(item, dict) and _STORED_INPUT_REQUIRED <= set(item) <= _STORED_INPUT_FIELDS
    )
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
    digests = (
        "image_id",
        "source_digest",
        "manifest_digest",
        "machine_contract_digest",
        "icon_digest",
        "pack_digest",
    )
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
        or record["stored_inputs"] != [value.document() for value in stored_inputs]
        or record["machine_contract"] != machine_contract
        or any(
            not isinstance(record[key], str) or http_payload.SOURCE_DIGEST_RE.fullmatch(record[key]) is None
            for key in digests
        )
    ):
        raise LocalSnapshotError("the local Assistant record is invalid")


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"
