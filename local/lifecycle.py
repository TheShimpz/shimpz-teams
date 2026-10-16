"""Local Team destruction and the whole-Space reset lifecycle."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from http import HTTPStatus

from docker.errors import DockerException

from action import journal as action_journal
from inference import client as brain_runtime_client
from inference import config as inference_config
from local import labels as local_labels
from local import names as local_names
from local import prepare as local_prepare
from local.errors import (
    DOCKER_ERRORS,
    action_state_unavailable,
    chat_stop_timeout,
    conversation_state_unavailable,
    docker_unavailable,
    file_preview_active,
    space_reset_failed,
    space_resource_ownership_conflict,
    team_destroy_failed,
    team_resources_ownership_conflict,
)
from local.errors import ApiProblemError as ApiProblem
from local.labels import (
    ASSISTANT_LABEL,
    IMAGE_LABEL,
    TEAM_LABEL,
)
from local.validation import brain_thread_id as _brain_thread_id
from local.validation import validate_team_id
from protocol.http.v1 import payload as http_payload
from storage import files as team_storage

_TEAM_RESIDUE_ABSENCE = frozenset(
    {
        "assistant_containers",
        "brain_checkpoints",
        "chat_continuations",
        "egress_policies",
        "inference_configuration",
        "integration_credentials",
        "stored_inputs",
        "action_checkpoints",
        "preparation_helpers",
        "publication_bindings",
        "routines",
        "runtime_state",
        "team_networks",
        "team_names",
        "team_storage",
    }
)


def purge_action_generation(self, generation: str) -> None:
    try:
        self.action_state.purge(generation)
    except action_journal.ActionJournalError as exc:
        raise action_state_unavailable() from exc


def team_assistant_containers(self, team_id: str) -> list:
    try:
        return self.client.containers.list(**self.assistant_lifecycle._assistant_filters(team_id))
    except DockerException as exc:
        raise docker_unavailable() from exc


def validate_destroy_containers(self, containers: list, team_id: str, network) -> None:
    for container in containers:
        assistant_id = container.labels.get(ASSISTANT_LABEL)
        spec = self.registry.get(team_id, assistant_id)
        if spec is None or network is None:
            raise team_resources_ownership_conflict()
        self.assistant_lifecycle._validate_container_profile(container, team_id, spec, network.name)


def delete_team_conversation(self, team_id: str, network) -> None:
    if network is None:
        return
    thread_id = _brain_thread_id(self.space_id, team_id, network.id)
    try:
        self.brain_runtime.delete_thread(thread_id)
    except brain_runtime_client.BrainRuntimeError as exc:
        raise conversation_state_unavailable() from exc
    self._purge_action_generation(network.id)


def _remove_team_helpers(self, team_id: str) -> None:
    try:
        local_prepare.remove_helpers(self.client, self.space_id, team_id)
    except DOCKER_ERRORS as exc:
        raise team_destroy_failed() from exc


def remove_team_assistants(self, team_id: str, containers: list) -> int:
    for container in containers:
        assistant_id = container.labels[ASSISTANT_LABEL]
        spec = self.registry.get(team_id, assistant_id)
        if spec is None:
            raise team_resources_ownership_conflict()
        binding = self.registry.binding(team_id, assistant_id)
        try:
            self.assistant_lifecycle._remove_assistant_container(container, binding)
        except DockerException as exc:
            raise team_destroy_failed() from exc
        if spec.allowed_hosts:
            self.assistant_lifecycle._remove_egress_policy(team_id, assistant_id)
        self.assistant_lifecycle._retire_binding(team_id, assistant_id, binding)
    for bound_team_id, assistant_id in sorted(self.registry.identities()):
        if bound_team_id != team_id:
            continue
        spec = self.registry.get(team_id, assistant_id)
        if spec is None:
            raise team_resources_ownership_conflict()
        if spec.allowed_hosts:
            self.assistant_lifecycle._remove_egress_policy(team_id, assistant_id)
        self.assistant_lifecycle._retire_binding(team_id, assistant_id, self.registry.binding(team_id, assistant_id))
    self.assistant_lifecycle.sweep_residues()
    return len(containers)


def delete_team_persistence(self, team_id: str) -> bool:
    try:
        storage_removed = self.storage.destroy(team_id)
    except team_storage.StorageError as exc:
        self._raise_storage_problem(exc)
    try:
        self.inference_store.delete(team_id)
    except inference_config.InferenceConfigError as exc:
        self._raise_inference_problem(exc)
    return storage_removed


def delete_team_private_state(self, team_id: str) -> None:
    self.chat_turn_service._delete_team_private_state(team_id)


def remove_team_network(self, network) -> bool:
    if network is None:
        return False
    try:
        network.remove()
    except DockerException as exc:
        raise team_destroy_failed() from exc
    return True


def clear_team_runtime_state(self, team_id: str) -> None:
    with self.chat_turn_service._active_chat_guard:
        token = self.chat_turn_service._active_chat_tokens.pop(team_id, None)
        self.chat_turn_service._active_action_containers.pop(team_id, None)
        if token is not None:
            self.chat_turn_service._cancelled_chat_tokens.discard(token)


@contextmanager
def previews_closed(team_id: str | None = None) -> Iterator[None]:
    """Hold one Team's file previews, or the whole Space's, closed after every one under way has ended."""
    with ExitStack() as closed:
        try:
            closed.enter_context(local_prepare.PREVIEWS.closed(team_id))
        except local_prepare.PreviewDrainError as exc:
            raise file_preview_active() from exc
        yield


def destroy_team(self, team_id: str, expected_name: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    expected_name = local_names.canonical_name(expected_name)
    # The namespace lock keeps the confirmed name current for the whole teardown; chat never takes it (ADR-0088).
    with self._names_lock:
        with self._lock(team_id):
            network = self.assistant_lifecycle._network(team_id, required=False)
            if network is not None and local_names.display_name(self, team_id, network) != expected_name:
                raise ApiProblem(
                    HTTPStatus.CONFLICT,
                    "Team name confirmation does not match",
                    code="team-name-mismatch",
                )
        with previews_closed(team_id):
            return _destroy_confirmed_team(self, team_id)


def _destroy_confirmed_team(self, team_id: str) -> dict[str, object]:
    self.chat_turn_service._cancel_chat_for_destroy(team_id)

    chat_lock = self.chat_turn_service._chat_lock(team_id)
    if not chat_lock.acquire(timeout=30):
        raise chat_stop_timeout()
    try:
        with self._lock(team_id):
            # Only with the turn drained and relocalization excluded do its pauses end, so none is recreated.
            self.chat_turn_service._end_paused_turns(team_id)
            residue_absent = {"chat_continuations"}
            network = self.assistant_lifecycle._network(team_id, required=False)
            containers = self._team_assistant_containers(team_id)
            self._validate_destroy_containers(containers, team_id, network)
            # A continuation that already expired is still this Team's; it never outlives the Team.
            self.chat_turn_service.expire_human_challenges(team_id)
            self._delete_team_conversation(team_id, network)
            residue_absent.update(("brain_checkpoints", "action_checkpoints"))
            _remove_team_helpers(self, team_id)
            residue_absent.add("preparation_helpers")
            self._delete_team_routines(team_id)
            residue_absent.add("routines")
            removed = self._remove_team_assistants(team_id, containers)
            residue_absent.update(("assistant_containers", "egress_policies", "publication_bindings"))
            storage_removed = self._delete_team_persistence(team_id)
            residue_absent.update(("inference_configuration", "team_storage"))
            destroyed = self._remove_team_network(network)
            residue_absent.add("team_networks")
            # Only once the network is gone: a failed removal must keep the surviving Team's current name.
            self.team_names.delete(team_id)
            residue_absent.add("team_names")
            self._delete_team_private_state(team_id)
            residue_absent.update(("integration_credentials", "stored_inputs"))
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


def validate_reset_container(self, container) -> None:
    container.reload()
    labels = container.attrs.get("Config", {}).get("Labels") or {}
    team_id = labels.get(TEAM_LABEL)
    assistant_id = labels.get(ASSISTANT_LABEL)
    if (
        http_payload.canonical_team_id(team_id) is None
        or http_payload.canonical_assistant_id(assistant_id) is None
        or not isinstance(labels.get(IMAGE_LABEL), str)
        or not self.assistant_lifecycle._labels_include(
            labels, self.assistant_lifecycle._base_labels(team_id, "assistant")
        )
        or container.name != self.assistant_lifecycle._container_name(team_id, assistant_id)
    ):
        raise space_resource_ownership_conflict()


def reset_inventory(self) -> tuple[list, list]:
    containers = self.client.containers.list(
        all=True,
        filters={"label": local_labels.label_filter(local_labels.ownership_labels(self.space_id, "assistant"))},
    )
    networks = self.client.networks.list(
        filters={"label": local_labels.label_filter(local_labels.ownership_labels(self.space_id, "team"))}
    )
    for container in containers:
        self._validate_reset_container(container)
    return containers, networks


def reset_assistant_identities(self, containers: list, networks: list) -> set[tuple[str, str]]:
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
            raise space_resource_ownership_conflict()
        validate_team_id(team_id)
        self.assistant_lifecycle._validate_network(network, team_id)
        owned_team_ids.add(team_id)
    owned_assistants.update(self.registry.identities())
    return owned_assistants


def remove_space_resources(
    self,
    containers: list,
    networks: list,
    owned_assistants: set[tuple[str, str]],
) -> tuple[bool, set[str]]:
    absent = {"integration_credentials", "stored_inputs"}
    self.chat_turn_service._delete_all_private_state()
    for network in networks:
        team_id = network.attrs["Labels"][TEAM_LABEL]
        self._delete_team_conversation(team_id, network)
    absent.update(("brain_checkpoints", "action_checkpoints"))
    self._delete_all_routines()
    absent.add("routines")
    for container in containers:
        labels = container.attrs["Config"]["Labels"]
        binding = self.registry.binding(labels[TEAM_LABEL], labels[ASSISTANT_LABEL])
        self.assistant_lifecycle._remove_assistant_container(container, binding)
    absent.add("assistant_containers")
    for team_id, assistant_id in sorted(owned_assistants):
        self.assistant_lifecycle._remove_egress_policy(team_id, assistant_id)
        self.assistant_lifecycle._retire_binding(team_id, assistant_id, self.registry.binding(team_id, assistant_id))
    self.assistant_lifecycle.sweep_residues()
    absent.update(("egress_policies", "publication_bindings"))
    storage_removed = self.storage.destroy_all()
    absent.add("team_storage")
    # Every owned file goes, even for a Team whose network a crash already removed.
    self.inference_store.delete_all()
    absent.add("inference_configuration")
    for network in networks:
        network.remove()
    absent.add("team_networks")
    self.team_names.delete_all()
    absent.add("team_names")
    team_ids = {team_id for team_id, _assistant_id in owned_assistants}
    team_ids.update(network.attrs["Labels"][TEAM_LABEL] for network in networks)
    for team_id in team_ids:
        self._clear_team_runtime_state(team_id)
    absent.add("runtime_state")
    return storage_removed, absent


def reset_space(self) -> dict[str, object]:
    """Remove every exactly owned workload/network without accepting resource ids."""
    with ExitStack() as locks:
        locks.enter_context(self._names_lock)
        # Every running turn stops and drains first, and none registers until the reset ends.
        locks.enter_context(self.chat_turn_service._drained_chat())
        # No file preview runs or starts a helper until the reset ends.
        locks.enter_context(previews_closed())
        for lock in self._locks:
            locks.enter_context(lock)
        # Only with turns and every Team-locked writer excluded do the paused turns end, so none is recreated.
        self.chat_turn_service._end_paused_turns()
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
            raise space_reset_failed() from exc
        residue_absent.add("chat_continuations")
        try:
            local_prepare.remove_helpers(self.client, self.space_id)
        except DOCKER_ERRORS as exc:
            raise space_reset_failed() from exc
        residue_absent.add("preparation_helpers")
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
