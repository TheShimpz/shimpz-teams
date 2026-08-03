"""Local Team destruction and whole-Space reset lifecycle."""

from __future__ import annotations

from contextlib import ExitStack
from http import HTTPStatus

from docker.errors import DockerException

from inference import client as brain_runtime_client
from inference import config as inference_config
from local.assistant.egress import PROFILE
from local.errors import ApiProblemError as ApiProblem
from local.labels import (
    ASSISTANT_LABEL,
    IMAGE_LABEL,
    KIND_LABEL,
    MANAGED_LABEL,
    PROFILE_LABEL,
    SPACE_LABEL,
    TEAM_LABEL,
)
from local.validation import ASSISTANT_ID_RE as _ASSISTANT_ID
from local.validation import MAX_ASSISTANT_ID_LENGTH, MAX_TEAM_ID_LENGTH, validate_team_id
from local.validation import TEAM_ID_RE as _TEAM_ID
from local.validation import brain_thread_id as _brain_thread_id
from power import journal as power_journal
from storage import files as team_storage

_TEAM_RESIDUE_ABSENCE = frozenset(
    {
        "assistant_containers",
        "brain_checkpoints",
        "chat_continuations",
        "egress_policies",
        "inference_configuration",
        "integration_credentials",
        "power_checkpoints",
        "publication_bindings",
        "runtime_state",
        "team_networks",
        "team_storage",
    }
)


def _purge_power_generation(self, generation: str) -> None:
    try:
        self.power_state.purge(generation)
    except power_journal.PowerJournalError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team Power execution state could not be deleted",
            code="power-state-unavailable",
        ) from exc


def _team_assistant_containers(self, team_id: str) -> list:
    try:
        return self.client.containers.list(**self.assistant_lifecycle._assistant_filters(team_id))
    except DockerException as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Docker is unavailable",
            code="docker-unavailable",
        ) from exc


def _validate_destroy_containers(self, containers: list, team_id: str, network) -> None:
    for container in containers:
        assistant_id = container.labels.get(ASSISTANT_LABEL)
        spec = self.registry.get(team_id, assistant_id)
        if spec is None or network is None:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Team resources failed their ownership contract",
                code="ownership-conflict",
            )
        self.assistant_lifecycle._validate_container_isolation(container, team_id, spec, network.name)


def _delete_team_conversation(self, team_id: str, network) -> None:
    if network is None:
        return
    thread_id = _brain_thread_id(self.space_id, team_id, network.id)
    try:
        self.brain_runtime.delete_thread(thread_id)
    except brain_runtime_client.BrainRuntimeError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team conversation state could not be deleted",
            code="brain-runtime-failed",
        ) from exc
    self._purge_power_generation(network.id)


def _remove_team_assistants(self, team_id: str, containers: list) -> int:
    for container in containers:
        assistant_id = container.labels[ASSISTANT_LABEL]
        spec = self.registry.get(team_id, assistant_id)
        if spec is None:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Team resources failed their ownership contract",
                code="ownership-conflict",
            )
        try:
            container.remove(force=True)
        except DockerException as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker could not destroy the Team",
                code="docker-remove-failed",
            ) from exc
        self.assistant_lifecycle._blocked_power_workloads.discard(container.id)
        self.assistant_lifecycle._remove_assistant_policy_if_needed(team_id, assistant_id, spec)
        self.registry.delete(team_id, assistant_id)
    for bound_team_id, assistant_id in sorted(self.registry.identities()):
        if bound_team_id != team_id:
            continue
        spec = self.registry.get(team_id, assistant_id)
        if spec is None:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Team resources failed their ownership contract",
                code="ownership-conflict",
            )
        self.assistant_lifecycle._remove_assistant_policy_if_needed(team_id, assistant_id, spec)
        self.registry.delete(team_id, assistant_id)
    return len(containers)


def _delete_team_persistence(self, team_id: str) -> bool:
    try:
        storage_removed = self.storage.destroy(team_id)
    except team_storage.StorageError as exc:
        self._raise_storage_problem(exc)
    try:
        self.inference_store.delete(team_id)
    except inference_config.InferenceConfigError as exc:
        self._raise_inference_problem(exc)
    return storage_removed


def _delete_team_private_state(self, team_id: str) -> None:
    self.chat_turn_service._delete_team_integration_state(team_id)


def _remove_team_network(self, network) -> bool:
    if network is None:
        return False
    self.assistant_lifecycle._disconnect_egress_proxy_if_attached(network)
    try:
        network.remove()
    except DockerException as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Docker could not destroy the Team",
            code="docker-remove-failed",
        ) from exc
    return True


def _clear_team_runtime_state(self, team_id: str) -> None:
    with self.chat_turn_service._active_chat_guard:
        token = self.chat_turn_service._active_chat_tokens.pop(team_id, None)
        self.chat_turn_service._active_power_containers.pop(team_id, None)
        if token is not None:
            self.chat_turn_service._cancelled_chat_tokens.discard(token)


def destroy_team(self, team_id: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    self.chat_turn_service._delete_chat_continuation(team_id)
    residue_absent = {"chat_continuations"}
    self.chat_turn_service._cancel_chat_for_destroy(team_id)

    chat_lock = self.chat_turn_service._chat_lock(team_id)
    if not chat_lock.acquire(timeout=30):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "active Team chat did not stop in time",
            code="chat-active",
        )
    try:
        with self._lock(team_id):
            network = self.assistant_lifecycle._network(team_id, required=False)
            containers = self._team_assistant_containers(team_id)
            self._validate_destroy_containers(containers, team_id, network)
            self._delete_team_conversation(team_id, network)
            residue_absent.update(("brain_checkpoints", "power_checkpoints"))
            removed = self._remove_team_assistants(team_id, containers)
            residue_absent.update(("assistant_containers", "egress_policies", "publication_bindings"))
            storage_removed = self._delete_team_persistence(team_id)
            residue_absent.update(("inference_configuration", "team_storage"))
            destroyed = self._remove_team_network(network)
            residue_absent.add("team_networks")
            self._delete_team_private_state(team_id)
            residue_absent.add("integration_credentials")
            self._clear_team_runtime_state(team_id)
            residue_absent.add("runtime_state")
            if residue_absent != _TEAM_RESIDUE_ABSENCE:
                raise ApiProblem(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "Team teardown proof is incomplete",
                    code="teardown-incomplete",
                )
            return {
                "team_id": team_id,
                "destroyed": destroyed,
                "assistants_removed": removed,
                "storage_removed": storage_removed,
                "residue_absent": sorted(residue_absent),
            }
    finally:
        chat_lock.release()


def _validate_reset_container(self, container) -> None:
    container.reload()
    labels = container.attrs.get("Config", {}).get("Labels") or {}
    team_id = labels.get(TEAM_LABEL)
    assistant_id = labels.get(ASSISTANT_LABEL)
    if (
        not isinstance(team_id, str)
        or len(team_id) > MAX_TEAM_ID_LENGTH
        or _TEAM_ID.fullmatch(team_id) is None
        or not isinstance(assistant_id, str)
        or len(assistant_id) > MAX_ASSISTANT_ID_LENGTH
        or _ASSISTANT_ID.fullmatch(assistant_id) is None
        or not isinstance(labels.get(IMAGE_LABEL), str)
        or not self.assistant_lifecycle._labels_include(
            labels, self.assistant_lifecycle._base_labels(team_id, "assistant")
        )
        or container.name != self.assistant_lifecycle._container_name(team_id, assistant_id)
    ):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "a labeled Space resource failed its ownership contract",
            code="ownership-conflict",
        )


def _reset_inventory(self) -> tuple[list, list]:
    base_labels = [
        f"{MANAGED_LABEL}=1",
        f"{PROFILE_LABEL}={PROFILE}",
        f"{SPACE_LABEL}={self.space_id}",
    ]
    containers = self.client.containers.list(
        all=True,
        filters={"label": [*base_labels, f"{KIND_LABEL}=assistant"]},
    )
    networks = self.client.networks.list(filters={"label": [*base_labels, f"{KIND_LABEL}=team"]})
    for container in containers:
        self._validate_reset_container(container)
    return containers, networks


def _reset_assistant_identities(self, containers: list, networks: list) -> set[tuple[str, str]]:
    owned_assistants = {
        (
            container.attrs["Config"]["Labels"][TEAM_LABEL],
            container.attrs["Config"]["Labels"][ASSISTANT_LABEL],
        )
        for container in containers
    }
    owned_team_ids: set[str] = set()
    for network in networks:
        labels = network.attrs.get("Labels") or {}
        team_id = labels.get(TEAM_LABEL)
        if not isinstance(team_id, str):
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "a labeled Space resource failed its ownership contract",
                code="ownership-conflict",
            )
        validate_team_id(team_id)
        self.assistant_lifecycle._validate_network(network, team_id)
        owned_team_ids.add(team_id)
    owned_assistants.update(self.registry.identities())
    return owned_assistants


def _remove_space_resources(
    self,
    containers: list,
    networks: list,
    owned_assistants: set[tuple[str, str]],
) -> tuple[bool, set[str]]:
    absent = {"integration_credentials"}
    self.chat_turn_service._delete_all_integration_state()
    for network in networks:
        team_id = network.attrs["Labels"][TEAM_LABEL]
        self._delete_team_conversation(team_id, network)
    absent.update(("brain_checkpoints", "power_checkpoints"))
    for container in containers:
        container.remove(force=True)
        self.assistant_lifecycle._blocked_power_workloads.discard(container.id)
    absent.add("assistant_containers")
    for team_id, assistant_id in sorted(owned_assistants):
        self.assistant_lifecycle._remove_egress_policy(team_id, assistant_id)
        self.registry.delete(team_id, assistant_id)
    absent.update(("egress_policies", "publication_bindings"))
    for network in networks:
        self.assistant_lifecycle._disconnect_egress_proxy_if_attached(network)
    storage_removed = self.storage.destroy_all()
    absent.add("team_storage")
    for network in networks:
        team_id = network.attrs["Labels"][TEAM_LABEL]
        self.inference_store.delete(team_id)
    absent.add("inference_configuration")
    for network in networks:
        network.remove()
    absent.add("team_networks")
    team_ids = {team_id for team_id, _assistant_id in owned_assistants}
    team_ids.update(network.attrs["Labels"][TEAM_LABEL] for network in networks)
    for team_id in team_ids:
        self._clear_team_runtime_state(team_id)
    absent.add("runtime_state")
    return storage_removed, absent


def reset_space(self) -> dict[str, object]:
    """Remove every exactly owned workload/network without accepting resource ids."""
    self.chat_turn_service._clear_chat_continuations()
    with ExitStack() as locks:
        for lock in self._locks:
            locks.enter_context(lock)
        try:
            containers, networks = self._reset_inventory()
            owned_assistants = self._reset_assistant_identities(containers, networks)
            storage_removed, residue_absent = self._remove_space_resources(
                containers,
                networks,
                owned_assistants,
            )
        except ApiProblem:
            raise
        except team_storage.StorageError as exc:
            self._raise_storage_problem(exc)
        except inference_config.InferenceConfigError as exc:
            self._raise_inference_problem(exc)
        except DockerException as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker could not reset the Space",
                code="docker-reset-failed",
            ) from exc
        residue_absent.add("chat_continuations")
        if residue_absent != _TEAM_RESIDUE_ABSENCE:
            raise ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Space reset proof is incomplete",
                code="teardown-incomplete",
            )
        return {
            "reset": True,
            "assistants_removed": len(containers),
            "teams_removed": len(networks),
            "storage_removed": storage_removed,
            "residue_absent": sorted(residue_absent),
        }
