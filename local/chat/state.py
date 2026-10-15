"""Local chat setup and persisted state operations."""

import logging
import math
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import NoReturn

from docker.errors import DockerException

from action import challenges as action_challenges
from action import stored_input as action_stored_input
from assistant import genesis as assistant_genesis
from assistant import language as assistant_language
from assistant import manifest as assistant_manifest
from inference import config as inference_config
from integrations import challenges as integration_challenges
from integrations import store as integration_store
from local.assistant import resources as local_assistant_resources
from local.chat import continuation as local_chat_continuations
from local.chat import continuation_store as local_chat_continuation_store
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.chat.types import PendingLocalChat as _PendingLocalChat
from local.errors import ApiProblemError as ApiProblem
from local.errors import (
    assistant_action_blocked,
    assistant_manifest_invalid,
    assistant_manifest_unavailable,
    inference_not_configured,
    inference_provider_mismatch,
    ownership_conflict,
    selected_file_not_found,
    stored_input_unavailable,
)
from local.install.runtime import AssistantSpec
from protocol.http.v1 import payload as http_payload
from storage import files as team_storage

log = logging.getLogger("shimpz-team-local")
MAX_CHAT_FILES = http_payload.MAX_CHAT_FILES


def chat_file_metadata(
    self,
    team_id: str,
    file_ids: object,
    metadata_connection=None,
) -> list[dict[str, object]]:
    if not isinstance(file_ids, list) or len(file_ids) > MAX_CHAT_FILES:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"files must contain at most {MAX_CHAT_FILES} opaque ids",
            code="invalid-files",
        )
    try:
        return self.storage.metadata(team_id, file_ids, metadata_connection)
    except team_storage.StorageNotFoundError as exc:
        raise selected_file_not_found() from exc
    except team_storage.StorageInputError as exc:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc), code="invalid-files") from exc
    except team_storage.StorageError as exc:
        self._raise_storage_problem(exc)


def team_assistants(self, team_id: str, *, scan: bool = True) -> tuple[str, str, dict[str, _ActiveAssistant]]:
    """The Team's name, network id, and running Assistants by id, read under its lifecycle lock.

    An Assistant absent from the result is proven not running for the Team; a Team that cannot be read raises.
    """
    with self._lock(team_id):
        network = self.assistant_lifecycle._network(team_id)
        team_name = self.assistant_lifecycle._validate_network(network, team_id, refresh=False)
        network_id = getattr(network, "id", None)
        if not isinstance(network_id, str) or not network_id:
            raise ownership_conflict()
        active_assistants = self._active_chat_assistants(team_id, network.name) if scan else ()
    return team_name, network_id, {active.spec.assistant_id: active for active in active_assistants}


def turn_inference(self, team_id: str, provider: str) -> inference_config.InferenceConfig:
    """The Team's model configuration, refused when it is absent or bound to another provider than the turn's."""
    try:
        config = self.inference_store.load(team_id)
    except inference_config.InferenceConfigError as exc:
        raise inference_not_configured() from exc
    if config.provider != provider:
        raise inference_provider_mismatch()
    return config


def chat_setup(
    self,
    team_id: str,
    file_ids: object,
    provider: str,
    assistant_ids: tuple[str, ...],
    metadata_connection=None,
    *,
    scan_empty_scope: bool = True,
) -> tuple[
    str,
    str,
    tuple[_ActiveAssistant, ...],
    list[dict[str, object]],
    inference_config.InferenceConfig,
]:
    with self._lock(team_id):
        team_name, network_id, active_by_id = team_assistants(
            self, team_id, scan=bool(scan_empty_scope or assistant_ids)
        )
        try:
            assistants = tuple(active_by_id[assistant_id] for assistant_id in assistant_ids)
        except KeyError:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "a selected Assistant is unavailable",
                code="assistant-unavailable",
            ) from None
        files = self._chat_file_metadata(team_id, file_ids, metadata_connection)
        config = turn_inference(self, team_id, provider)
    return team_name, network_id, assistants, files, config


def active_assistant_genesis(self, active: _ActiveAssistant) -> str:
    container = active.container
    if container is None:
        try:
            container = self.client.containers.get(active.container_id)
        except DockerException as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "installed Assistant Genesis could not be verified",
                code="assistant-genesis-unavailable",
            ) from exc
    if getattr(container, "id", None) != active.container_id:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "installed Assistant Genesis failed its identity contract",
            code="assistant-genesis-drift",
        )
    try:
        return self._assistant_genesis_cache.get(container)
    except assistant_genesis.GenesisError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "installed Assistant Genesis failed its contract",
            code="assistant-genesis-invalid",
        ) from exc


def assistant_language_pack(self, active: _ActiveAssistant) -> assistant_language.LanguagePack:
    """The verified language pack of the active container generation and its reviewed binding (ADR-0091)."""
    container = active.container
    if getattr(container, "id", None) != active.container_id:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "installed Assistant language pack failed its identity contract",
            code="assistant-language-drift",
        )
    try:
        return self._assistant_language_cache.get(container, active.spec.machine_contract, active.spec.pack_digest)
    except assistant_manifest.ManifestUnavailableError as exc:
        raise assistant_manifest_unavailable() from exc
    except assistant_manifest.ManifestError as exc:
        raise assistant_manifest_invalid() from exc


def admit_assistant_allowed_hosts(self, container, spec: AssistantSpec) -> tuple[str, ...]:
    try:
        reviewed = assistant_manifest.reviewed_manifest_contract(
            allowed_hosts=spec.allowed_hosts,
            integrations=spec.integrations,
            stored_inputs=spec.stored_inputs,
        )
        declared = self._assistant_allowed_hosts_cache.get(container, reviewed)
        self._assistant_machine_contract_cache.get(
            container, declared, spec.machine_contract, summary=spec.summary, description=spec.description
        )
        self._assistant_language_cache.get(container, spec.machine_contract, spec.pack_digest)
    except assistant_manifest.ManifestUnavailableError as exc:
        log.warning("Assistant manifest admission unavailable: %s", exc)
        raise assistant_manifest_unavailable() from exc
    except assistant_manifest.ManifestError as exc:
        log.warning("Assistant manifest admission failed: %s", exc)
        raise assistant_manifest_invalid() from exc
    else:
        return declared.allowed_hosts


def active_chat_assistants(self, team_id: str, network_name: str) -> tuple[_ActiveAssistant, ...]:
    active: list[_ActiveAssistant] = []
    lifecycle = self.assistant_lifecycle
    filters = lifecycle._assistant_filters(team_id)
    for container, binding in local_assistant_resources.bound_containers(
        lifecycle.client, self.registry, filters, team_id
    ):
        spec = self.registry.spec(binding)
        self.assistant_lifecycle._validate_container(container, team_id, spec, network_name, refresh=False)
        if container.id in self.assistant_lifecycle._blocked_action_workloads:
            raise assistant_action_blocked()
        if container.status == "running":
            active.append(_ActiveAssistant(spec=spec, container_id=container.id, container=container))
    active.sort(key=lambda item: item.spec.assistant_id)
    return tuple(active)


def _integration_state[T](self, change: Callable[[integration_store.OAuthIntegrationStore], T]) -> T:
    """Apply ``change`` to the Integration token store, reporting its failure as the Integration problem."""
    try:
        return change(self.assistant_integrations)
    except integration_store.OAuthIntegrationStoreError as exc:
        self._raise_integration_problem(exc)


def _stored_input_state[T](self, change: Callable[[action_stored_input.StoredInputStore], T]) -> T:
    """Apply ``change`` to the Stored Input store, reporting its failure as Stored Input state unavailable."""
    try:
        return change(self.assistant_stored_inputs)
    except action_stored_input.StoredInputStoreError as exc:
        raise stored_input_unavailable() from exc


def delete_assistant_private_state(self, team_id: str, assistant_id: str) -> None:
    """Delete one Assistant's Integration tokens, then its Stored Inputs."""
    _integration_state(self, lambda store: store.delete_assistant(team_id, assistant_id))
    _stored_input_state(self, lambda store: store.delete_assistant(team_id, assistant_id))


def delete_team_private_state(self, team_id: str) -> None:
    """Delete one Team's Integration tokens, then its Stored Inputs."""
    _integration_state(self, lambda store: store.delete_team(team_id))
    _stored_input_state(self, lambda store: store.delete_team(team_id))


def delete_all_private_state(self) -> None:
    """Delete every Integration token, then every Stored Input, during an owned Space reset."""
    _integration_state(self, lambda store: store.delete_all())
    _stored_input_state(self, lambda store: store.delete_all())


def retain_declared_private_state(self, team_id: str, spec: AssistantSpec) -> None:
    """Keep only the Integrations and Stored Inputs the Assistant's current release declares.

    Pruning a token ends the Team's Integration challenge and its paused chat, which may have waited on that token.
    """
    integrations = tuple(sorted(spec.integrations))
    if _integration_state(self, lambda store: store.retain_declared(team_id, spec.assistant_id, integrations)):
        self.integration_challenges.cancel_team(team_id)
        self._delete_chat_continuation(team_id)
    stored_inputs = tuple(sorted(spec.stored_inputs))
    _stored_input_state(self, lambda store: store.retain_declared(team_id, spec.assistant_id, stored_inputs))


def raise_chat_continuation_problem(exc: Exception) -> NoReturn:
    raise ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "Team chat continuation state is unavailable",
        code="chat-state-unavailable",
    ) from exc


def persist_chat_continuation(
    self,
    kind: str,
    challenge: object,
    requirements: tuple[object, ...],
    pending: _PendingLocalChat,
) -> None:
    challenge_id = getattr(challenge, "id", None)
    expires_at = getattr(challenge, "expires_at", None)
    remaining_seconds = math.ceil(expires_at - time.monotonic()) if isinstance(expires_at, float) else 0
    if not isinstance(challenge_id, str) or not 1 <= remaining_seconds <= local_chat_continuation_store.MAX_TTL_SECONDS:
        self._raise_chat_continuation_problem(
            local_chat_continuation_store.ContinuationStoreError("challenge lifetime is invalid")
        )
    try:
        bindings, payload = local_chat_continuations.encode(kind, requirements, pending)
        self.chat_continuations.put(
            getattr(challenge, "team_id", None),
            kind,
            challenge_id,
            int(time.time()) + remaining_seconds,
            bindings,
            payload,
        )
    except (
        local_chat_continuation_store.ContinuationStoreError,
        local_chat_continuations.ContinuationCodecError,
    ) as exc:
        self._raise_chat_continuation_problem(exc)


def restore_chat_continuation(
    self,
    stored: local_chat_continuation_store.StoredContinuation,
) -> None:
    remaining_seconds = stored.expires_at - int(time.time())
    if remaining_seconds <= 0:
        return
    try:
        decoded = local_chat_continuations.decode(stored)
        if decoded.kind == "integrations":
            self.integration_challenges.restore(
                stored.team_id,
                stored.challenge_id,
                remaining_seconds,
                decoded.requirements,
                decoded.pending,
            )
        elif decoded.kind == "human":
            if len(decoded.requirements) != 1:
                raise local_chat_continuations.ContinuationCodecError("human continuation is malformed")
            self.human_challenges.restore(
                stored.team_id,
                stored.challenge_id,
                remaining_seconds,
                decoded.requirements[0],
                decoded.pending,
            )
        else:
            raise local_chat_continuations.ContinuationCodecError("continuation kind is malformed")
    except (
        local_chat_continuation_store.ContinuationStoreError,
        local_chat_continuations.ContinuationCodecError,
        integration_challenges.IntegrationChallengeError,
        action_challenges.HumanChallengeError,
    ) as exc:
        self._raise_chat_continuation_problem(exc)


def purge_expired_human_continuation(
    self,
    stored: local_chat_continuation_store.StoredContinuation,
) -> None:
    if stored.kind != "human":
        return
    try:
        decoded = local_chat_continuations.decode(stored)
        if decoded.kind != "human":
            raise local_chat_continuations.ContinuationCodecError("expired human continuation kind changed")
    except local_chat_continuations.ContinuationCodecError as exc:
        self._raise_chat_continuation_problem(exc)
    self._purge_human_pending(decoded.pending)


def restore_all_chat_continuations(self) -> None:
    try:
        expired = self.chat_continuations.drain_expired()
        stored = self.chat_continuations.active()
    except local_chat_continuation_store.ContinuationStoreError as exc:
        self._raise_chat_continuation_problem(exc)
    for continuation in expired:
        self._purge_expired_human_continuation(continuation)
    for continuation in stored:
        self._restore_chat_continuation(continuation)


def delete_chat_continuation(
    self,
    team_id: str,
    challenge_id: str | None = None,
) -> bool:
    try:
        return self.chat_continuations.delete(team_id, challenge_id)
    except local_chat_continuation_store.ContinuationStoreError as exc:
        self._raise_chat_continuation_problem(exc)


def delete_withdrawn_continuation(self, team_id: str, challenge: object) -> bool:
    """Delete the continuation of one withdrawn challenge; another challenge's continuation belongs to a newer turn."""
    try:
        return self.chat_continuations.delete(team_id, challenge.id)
    except local_chat_continuation_store.ContinuationNotFoundError:
        return False
    except local_chat_continuation_store.ContinuationStoreError as exc:
        self._raise_chat_continuation_problem(exc)


def clear_chat_continuations(self) -> int:
    try:
        return self.chat_continuations.clear()
    except local_chat_continuation_store.ContinuationStoreError as exc:
        self._raise_chat_continuation_problem(exc)
