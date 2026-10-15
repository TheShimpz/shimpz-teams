"""Local Assistant resolution, image trust, and isolation validation."""

from collections.abc import Mapping
from http import HTTPStatus

from docker.errors import DockerException, ImageNotFound, NotFound

from assistant import manifest as assistant_manifest
from install.bindings import DynamicAssistantBinding
from local import labels as local_labels
from local.assistant import isolation as local_container_policy
from local.errors import ApiProblemError as ApiProblem
from local.errors import assistant_isolation_drift, assistant_registry_drift, docker_unavailable
from local.install import snapshots as local_snapshots
from local.install.runtime import AssistantSpec
from local.labels import (
    ASSISTANT_LABEL,
    IMAGE_LABEL,
)


def assistant_filters(self, team_id: str) -> dict[str, list[str] | bool]:
    return {
        "all": True,
        "filters": {
            "label": local_labels.label_filter(local_labels.ownership_labels(self.space_id, "assistant", team_id))
        },
    }


def assistant_container(self, team_id: str, assistant_id: str, *, required: bool = True):
    name = self._container_name(team_id, assistant_id)
    try:
        container = self.client.containers.get(name)
    except NotFound:
        if required:
            raise ApiProblem(
                HTTPStatus.NOT_FOUND,
                "Assistant is not installed",
                code="assistant-not-found",
            ) from None
        return None
    return container


def bound_containers(
    client, registry, filters: Mapping[str, object], team_id: str
) -> list[tuple[object, DynamicAssistantBinding]]:
    """The Team's Assistant containers ``filters`` select, each with the binding its Assistant label names.

    The Team's bindings are read once, and only when it has a container. A container whose label names no binding of
    this Team is registry drift, never an Assistant to trust.
    """
    try:
        containers = client.containers.list(**filters)
    except DockerException as exc:
        raise docker_unavailable() from exc
    bindings_by_id = (
        {binding.assistant_id: binding for binding in registry.team_bindings(team_id)} if containers else {}
    )
    bound: list[tuple[object, DynamicAssistantBinding]] = []
    for container in containers:
        labels = container.labels
        assistant_id = labels.get(ASSISTANT_LABEL) if isinstance(labels, dict) else None
        binding = bindings_by_id.get(assistant_id) if isinstance(assistant_id, str) else None
        if binding is None:
            raise assistant_registry_drift()
        bound.append((container, binding))
    return bound


def assistant_specs(self, team_id: str, *, running_only: bool = False) -> tuple[AssistantSpec, ...]:
    """Return Team specs after ownership, label, and uniqueness gates; no deep container or artifact admission."""
    self._network(team_id)
    seen: set[str] = set()
    specs: list[AssistantSpec] = []
    for container, binding in bound_containers(self.client, self.registry, self._assistant_filters(team_id), team_id):
        labels, assistant_id = container.labels, binding.assistant_id
        # Convert before accepting container labels so every matched runtime contract is validated.
        spec = self.registry.spec(binding)
        expected_labels = self._base_labels(team_id, "assistant")
        expected_labels[ASSISTANT_LABEL] = assistant_id
        if (
            container.name != self._container_name(team_id, assistant_id)
            or not self._labels_include(labels, expected_labels)
            or not isinstance(container.status, str)
            or assistant_id in seen
        ):
            raise assistant_isolation_drift()
        seen.add(assistant_id)
        if not running_only or container.status == "running":
            specs.append(spec)
    return tuple(sorted(specs, key=lambda item: item.assistant_id))


def resolve(self, team_id: str, assistant_id: str) -> AssistantSpec:
    spec = self.registry.get(team_id, assistant_id)
    if spec is None:
        # Resolution is intentionally completed before any image lookup/pull.
        raise ApiProblem(HTTPStatus.NOT_FOUND, "Assistant is not allowlisted", code="assistant-not-allowlisted")
    return spec


def image_labels_valid(image, spec: AssistantSpec) -> bool:
    labels = (image.attrs.get("Config") or {}).get("Labels") or {}
    return all(labels.get(key) == value for key, value in spec.required_image_labels)


def trusted_image(self, spec: AssistantSpec):
    if spec.provenance != "published":
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "the Assistant does not have published image provenance",
            code="image-provenance-mismatch",
        )
    try:
        image = self.client.images.get(spec.image)
    except ImageNotFound:
        try:
            image = self.client.images.pull(spec.image)
        except DockerException as exc:
            raise ApiProblem(
                HTTPStatus.BAD_GATEWAY,
                "the trusted Assistant image could not be pulled",
                code="image-pull-failed",
            ) from exc
    except DockerException as exc:
        raise docker_unavailable() from exc
    repo_digests = image.attrs.get("RepoDigests") or []
    labels = (image.attrs.get("Config") or {}).get("Labels") or {}
    if (
        spec.image not in repo_digests
        or not self._image_labels_valid(image, spec)
        or labels.get(local_snapshots.LOCAL_STAGE_LABEL) == local_snapshots.LOCAL_STAGE_VALUE
    ):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "the Assistant image does not match its trusted contract",
            code="image-contract-mismatch",
        )
    return image


def staged_image(self, spec: AssistantSpec):
    if spec.provenance != "local":
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "the Assistant does not have Local image provenance",
            code="image-provenance-mismatch",
        )
    try:
        image = self.client.images.get(spec.image)
    except ImageNotFound as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "the staged Local Assistant image is no longer available",
            code="local-image-missing",
        ) from exc
    except DockerException as exc:
        raise docker_unavailable() from exc
    attrs = image.attrs
    architecture = spec.platform.rpartition("/")[2] if isinstance(spec.platform, str) else None
    if (
        image.id != spec.image
        or attrs.get("Id") != spec.image
        or attrs.get("Architecture") != architecture
        or not local_snapshots.local_repo_digests_valid(attrs.get("RepoDigests"), spec.assistant_id, spec.image)
        # A bound snapshot keeps running after a newer stage supersedes it and takes the Assistant's tag.
        or attrs.get("RepoTags") not in ([], [local_snapshots.canonical_reference(spec.assistant_id)])
        or not self._image_labels_valid(image, spec)
    ):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "the staged Local Assistant image does not match its binding",
            code="local-image-contract-mismatch",
        )
    return image


def assistant_image(self, spec: AssistantSpec):
    if spec.provenance == "published":
        return self._trusted_image(spec)
    if spec.provenance == "local":
        return self._staged_image(spec)
    raise ApiProblem(
        HTTPStatus.CONFLICT,
        "the Assistant image provenance is invalid",
        code="image-provenance-mismatch",
    )


def assistant_labels(self, team_id: str, spec: AssistantSpec) -> dict[str, str]:
    labels = self._base_labels(team_id, "assistant")
    labels.update({ASSISTANT_LABEL: spec.assistant_id, IMAGE_LABEL: spec.image})
    return labels


def validate_container_profile(
    self,
    container,
    team_id: str,
    spec: AssistantSpec,
    network_name: str,
    *,
    refresh: bool = True,
) -> tuple[dict, dict[str, str]]:
    if refresh:
        container.reload()
    expected_labels = self._assistant_labels(team_id, spec)
    expected_labels.pop(IMAGE_LABEL)
    admitted = local_container_policy.inspect_profile(
        container.attrs,
        container.name,
        expected_labels,
        self._container_name(team_id, spec.assistant_id),
        local_container_policy.ImageIdentity(spec.image, spec.provenance),
        network_name,
        self.cpuset_cpus,
    )
    if admitted is None:
        raise assistant_isolation_drift()
    return admitted


def validate_container_egress_environment(
    self,
    team_id: str,
    spec: AssistantSpec,
    environment: dict[str, str],
) -> tuple[str, ...]:
    try:
        reviewed_hosts = assistant_manifest.canonical_allowed_hosts(spec.allowed_hosts)
    except assistant_manifest.ManifestError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "the reviewed Assistant allowed_hosts contract is invalid",
            code="assistant-registry-drift",
        ) from exc
    if not local_container_policy.egress_environment_valid(environment):
        raise assistant_isolation_drift()
    if reviewed_hosts:
        # Team's own provider-call route for this Assistant must hold exactly the reviewed hosts (ADR-0106).
        self._validate_egress_policy(team_id, spec, reviewed_hosts)
    return reviewed_hosts


def validate_container_isolation(
    self,
    container,
    team_id: str,
    spec: AssistantSpec,
    network_name: str,
    *,
    refresh: bool = True,
) -> dict:
    config, environment = self._validate_container_profile(
        container,
        team_id,
        spec,
        network_name,
        refresh=refresh,
    )
    self._validate_container_egress_environment(team_id, spec, environment)
    return config


def validate_container_security(
    self,
    container,
    team_id: str,
    spec: AssistantSpec,
    network_name: str,
    *,
    refresh: bool = True,
) -> dict:
    config = self._validate_container_isolation(container, team_id, spec, network_name, refresh=refresh)
    self._admit_assistant_allowed_hosts(container, spec)
    return config


def has_current_assistant_artifact(config: dict, spec: AssistantSpec) -> bool:
    labels = config.get("Labels") or {}
    return config.get("Image") == spec.image and labels.get(IMAGE_LABEL) == spec.image


def validate_current_assistant_artifact(self, config: dict, spec: AssistantSpec) -> None:
    if not self._has_current_assistant_artifact(config, spec):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "the installed Assistant must be updated",
            code="assistant-update-required",
        )


def validate_container(
    self,
    container,
    team_id: str,
    spec: AssistantSpec,
    network_name: str,
    *,
    refresh: bool = True,
) -> None:
    config = self._validate_container_security(container, team_id, spec, network_name, refresh=refresh)
    self._validate_current_assistant_artifact(config, spec)
