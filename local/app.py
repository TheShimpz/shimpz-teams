"""Minimal Docker controller for one locally owned Shimpz Space.

This is intentionally separate from the hosted Team controller.  An empty Team is
one labeled internal network; its only runnable resources are installed,
digest-pinned published Assistants with declared Action contracts.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sys
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import NoReturn

import docker
from docker.errors import APIError, DockerException

from action import challenges as action_challenges
from action import execution as action_execution
from action import human as action_human
from action import journal as action_journal
from assistant import genesis as assistant_genesis
from assistant import manifest as assistant_manifest
from assistant.spec import validate_action_payload
from inference import client as brain_runtime_client
from inference import config as inference_config
from inference import token as brain_runtime_token_store
from install import artifact_trust, bindings, icons, registry_auth
from install import update as assistant_update
from integrations import broker as integration_broker
from integrations import challenges as integration_challenges
from integrations import pkce as integration_pkce
from integrations import service as integration_service
from integrations import store as integration_store
from local import audit as local_audit
from local import labels as local_labels
from local import lifecycle as local_team_lifecycle
from local import token as local_token_store
from local.assistant import api as local_assistant_api
from local.assistant import egress as local_egress
from local.assistant import lifecycle as local_assistant_lifecycle
from local.assistant import resources as local_assistant_resources
from local.assistant import rpc as local_assistant_rpc
from local.assistant.egress import PROFILE
from local.chat import api as local_chat_api
from local.chat import capabilities as local_chat_capabilities
from local.chat import continuation_store as local_chat_continuation_store
from local.chat import execution as local_chat_execution
from local.chat import human as local_chat_human
from local.chat import pause as local_chat_pause
from local.chat import private as local_chat_private
from local.chat import resume as local_chat_resume
from local.chat import segment as local_chat_segment
from local.chat import state as local_chat_state
from local.errors import ApiProblemError as ApiProblem
from local.http.server import REQUEST_TIMEOUT_SECONDS, BoundedServer, Handler
from local.install import automatic as local_automatic_updates
from local.install import developers as local_developers
from local.install import service as local_install_service
from local.install.registry import PublicationRegistry
from local.labels import IMAGE_LABEL as _LOCAL_IMAGE_LABEL
from local.labels import (
    KIND_LABEL,
    MANAGED_LABEL,
    PROFILE_LABEL,
    SPACE_LABEL,
    TEAM_LABEL,
    TEAM_NAME_LABEL,
)
from local.validation import brain_thread_id as _local_brain_thread_id
from local.validation import (
    half_cpu_set,
    validate_space_id,
    validate_team_id,
    validate_team_name,
)
from storage import files as team_storage

IMAGE_LABEL = _LOCAL_IMAGE_LABEL
ASSISTANT_LABEL = local_labels.ASSISTANT_LABEL
_brain_thread_id = _local_brain_thread_id

log = logging.getLogger("shimpz-team-local")

LISTEN_PORT = 7077
STORAGE_ROOT = Path("/var/lib/shimpz-local/storage")
INFERENCE_ROOT = Path("/var/lib/shimpz-local/inference")
LOCAL_ACTION_JOURNAL_PATH = Path(
    os.environ.get(
        "SHIMPZ_LOCAL_ACTION_JOURNAL_PATH",
        "/var/lib/shimpz-local/action-journal/journal.sqlite3",
    )
)
LOCAL_CHAT_CONTINUATIONS_STATE_PATH = Path(
    os.environ.get(
        "SHIMPZ_LOCAL_CHAT_CONTINUATIONS_STATE_PATH",
        str(local_chat_continuation_store.STATE_PATH),
    )
)
LOCAL_CHAT_CONTINUATIONS_KEY_PATH = Path(
    os.environ.get(
        "SHIMPZ_LOCAL_CHAT_CONTINUATIONS_KEY_PATH",
        str(local_chat_continuation_store.KEY_PATH),
    )
)
LOCAL_PUBLICATION_BINDINGS_PATH = Path("/var/lib/shimpz-local/publications/bindings.json")
LOCAL_PUBLICATION_ICONS_PATH = Path("/var/lib/shimpz-local/publications/icons")
LOCAL_ASSISTANT_UPDATES_PATH = Path("/var/lib/shimpz-local/publications/updates")
LOCAL_ASSISTANT_RESIDUES_PATH = Path("/var/lib/shimpz-local/publications/residues")
LOCAL_COSIGN_TRUST_ROOT = Path("/var/lib/shimpz-local/cosign")


@dataclass(frozen=True)
class AssistantLifecycleDependencies:
    """Explicit external dependencies for Assistant lifecycle operations."""

    client: object | None = None
    space_id: str | None = None
    registry: object | None = None
    cpuset_cpus: str | None = None
    lock_for: object | None = None
    invoke: object | None = None
    list_assistants: object | None = None
    developers: object | None = None
    artifact_trust: object | None = None
    updates: object | None = None
    residues: object | None = None
    icons: object | None = None


@dataclass(frozen=True)
class ChatTurnDependencies:
    """Explicit external dependencies for local chat-turn operations."""

    space_id: str | None = None
    registry: object | None = None
    storage: object | None = None
    inference_store: object | None = None
    brain_runtime: object | None = None
    action_state: object | None = None
    assistant_integrations: object | None = None
    integration_challenges: object | None = None
    human_challenges: object | None = None
    oauth_pkce: object | None = None
    oauth_service: object | None = None
    chat_continuations: object | None = None
    lock_for: object | None = None
    raise_storage_problem: object | None = None


class AssistantLifecycle:
    """Own Assistant admission, resources, RPC, and egress lifecycle."""

    def __init__(self, dependencies: AssistantLifecycleDependencies) -> None:
        self.client = dependencies.client
        self.space_id = dependencies.space_id
        self.registry = dependencies.registry
        self.cpuset_cpus = dependencies.cpuset_cpus
        self._lock = dependencies.lock_for
        self.invoke = dependencies.invoke
        self.list_assistants = dependencies.list_assistants
        self.developers = dependencies.developers
        self.artifact_trust = dependencies.artifact_trust
        self.updates = dependencies.updates
        self.residues = dependencies.residues
        self.icons = dependencies.icons
        self._assistant_genesis_cache = assistant_genesis.GenesisCache()
        self._assistant_allowed_hosts_cache = assistant_manifest.ManifestContractCache()
        self._assistant_machine_contract_cache = assistant_manifest.MachineContractCache()
        self._blocked_action_workloads: set[str] = set()

    _rollback_assistant_install = local_assistant_lifecycle._rollback_assistant_install
    _create_assistant_container = local_assistant_lifecycle._create_assistant_container
    _replace_unready_assistant = local_assistant_lifecycle._replace_unready_assistant
    _replace_outdated_assistant = local_assistant_lifecycle._replace_outdated_assistant
    _restore_previous_assistant = local_assistant_lifecycle._restore_previous_assistant
    _remove_retired_image = local_assistant_lifecycle._remove_retired_image
    _binding_uses_image = local_assistant_lifecycle._binding_uses_image
    _delete_retired_image = local_assistant_lifecycle._delete_retired_image
    _retired_image_id = staticmethod(local_assistant_lifecycle._retired_image_id)
    _clear_update = local_assistant_lifecycle._clear_update
    sweep_residues = local_assistant_lifecycle.sweep_residues
    _queue_residue = local_assistant_lifecycle._queue_residue
    install_assistant = local_assistant_lifecycle.install_assistant
    update_assistant = local_assistant_lifecycle.update_assistant
    _recover_update_target = local_assistant_lifecycle._recover_update_target
    recover_updates = local_assistant_lifecycle.recover_updates
    resume_assistants = local_assistant_lifecycle.resume_assistants
    uninstall_assistant = local_assistant_lifecycle.uninstall_assistant

    _assistant_filters = local_assistant_resources._assistant_filters
    _assistant_container = local_assistant_resources._assistant_container
    _assistant_ids = local_assistant_resources._assistant_ids
    _resolve = local_assistant_resources._resolve
    _image_labels_valid = staticmethod(local_assistant_resources._image_labels_valid)
    _trusted_image = local_assistant_resources._trusted_image
    _assistant_labels = local_assistant_resources._assistant_labels
    _validate_container_profile = local_assistant_resources._validate_container_profile
    _validate_container_egress_environment = local_assistant_resources._validate_container_egress_environment
    _validate_container_egress = local_assistant_resources._validate_container_egress
    _validate_container_isolation = local_assistant_resources._validate_container_isolation
    _validate_container_security = local_assistant_resources._validate_container_security
    _has_current_assistant_artifact = staticmethod(local_assistant_resources._has_current_assistant_artifact)
    _validate_current_assistant_artifact = local_assistant_resources._validate_current_assistant_artifact
    _validate_container = local_assistant_resources._validate_container
    _active_assistant_genesis = local_chat_state._active_assistant_genesis
    _admit_assistant_allowed_hosts = local_chat_state._admit_assistant_allowed_hosts

    _close_exec_stream = staticmethod(local_assistant_rpc._close_exec_stream)
    _fail_stop_action = local_assistant_rpc._fail_stop_action
    _action_not_running = staticmethod(local_assistant_rpc._action_not_running)
    _rpc = local_assistant_rpc._rpc
    _wait_ready = local_assistant_rpc._wait_ready

    _base_labels = local_egress._base_labels
    _network_name = local_egress._network_name
    _container_name = local_egress._container_name
    _egress_policy_identity = local_egress._egress_policy_identity
    _egress_token = local_egress._egress_token
    _proxy_environment = staticmethod(local_egress._proxy_environment)
    _reserve_assistant_egress_environment = local_egress._reserve_assistant_egress_environment
    _write_egress_policy = local_egress._write_egress_policy
    _validate_egress_policy = local_egress._validate_egress_policy
    _read_admitted_egress_policy = local_egress._read_admitted_egress_policy
    _remove_egress_policy = local_egress._remove_egress_policy
    _egress_proxy = local_egress._egress_proxy
    _connect_egress_proxy = local_egress._connect_egress_proxy
    _reconcile_egress_proxy_attachment = local_egress._reconcile_egress_proxy_attachment
    _disconnect_egress_proxy = local_egress._disconnect_egress_proxy
    _disconnect_egress_proxy_if_attached = local_egress._disconnect_egress_proxy_if_attached
    _managed_team_networks = local_egress._managed_team_networks
    _team_requires_egress_proxy = local_egress._team_requires_egress_proxy
    _reconcile_egress_proxy_attachments = local_egress._reconcile_egress_proxy_attachments
    _team_has_egress_assistant = local_egress._team_has_egress_assistant
    _release_assistant_egress = local_egress._release_assistant_egress
    _remove_assistant_policy_if_needed = local_egress._remove_assistant_policy_if_needed
    _activate_assistant_egress = local_egress._activate_assistant_egress
    _labels_include = staticmethod(local_egress._labels_include)
    _validate_network = local_egress._validate_network
    _network = local_egress._network


class ChatTurnService:
    """Own local chat turns, continuations, challenges, and private state."""

    def __init__(self, dependencies: ChatTurnDependencies) -> None:
        self.space_id = dependencies.space_id
        self.registry = dependencies.registry
        self.storage = dependencies.storage
        self.inference_store = dependencies.inference_store
        self.brain_runtime = dependencies.brain_runtime
        self.action_state = dependencies.action_state
        self.assistant_integrations = dependencies.assistant_integrations
        self.integration_challenges = dependencies.integration_challenges
        self.human_challenges = dependencies.human_challenges or action_challenges.HumanChallengeStore()
        self.oauth_pkce = dependencies.oauth_pkce
        self.oauth_service = dependencies.oauth_service
        self.chat_continuations = dependencies.chat_continuations
        self._lock = dependencies.lock_for
        self._raise_storage_problem = dependencies.raise_storage_problem
        self._active_chat_guard = threading.Lock()
        self._chat_locks: dict[str, threading.Lock] = {}
        self._active_chat_tokens: dict[str, str] = {}
        self._active_action_containers: dict[str, tuple[str, object]] = {}
        self._cancelled_chat_tokens: set[str] = set()

    def _chat_lock(self, team_id: str) -> threading.Lock:
        with self._active_chat_guard:
            return self._chat_locks.setdefault(team_id, threading.Lock())

    def _chat_cancelled(self, token: str) -> bool:
        with self._active_chat_guard:
            return token in self._cancelled_chat_tokens

    def _commit_chat_terminal(self, team_id: str, token: str) -> bool:
        """Commit a reply only when Stop did not win this service-owned turn."""
        with self._active_chat_guard:
            if token in self._cancelled_chat_tokens or self._active_chat_tokens.get(team_id) != token:
                return False
            self._active_chat_tokens.pop(team_id, None)
            return True

    def _cancel_chat_for_destroy(self, team_id: str) -> None:
        """Prevent another Action and synchronously stop one already executing."""
        with self._active_chat_guard:
            token = self._active_chat_tokens.get(team_id)
            if token is not None:
                self._cancelled_chat_tokens.add(token)
            active = self._active_action_containers.get(team_id)
            active_action = active[1] if token is not None and active is not None and active[0] == token else None
        if active_action is not None:
            self.assistant_lifecycle._fail_stop_action(active_action)

    @contextmanager
    def _exclusive_chat_turn(self, team_id: str):
        lock = self._chat_lock(team_id)
        if not lock.acquire(blocking=False):
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Team already has an active chat turn",
                code="chat-active",
            )
        token = secrets.token_hex(16)
        with self._active_chat_guard:
            self._active_chat_tokens[team_id] = token
        try:
            yield token
        finally:
            with self._active_chat_guard:
                if self._active_chat_tokens.get(team_id) == token:
                    self._active_chat_tokens.pop(team_id, None)
                active = self._active_action_containers.get(team_id)
                if active is not None and active[0] == token:
                    self._active_action_containers.pop(team_id, None)
                self._cancelled_chat_tokens.discard(token)
            lock.release()

    _pending_chat_continuation = local_chat_api._pending_chat_continuation
    _segment_response = local_chat_api._segment_response
    chat = local_chat_api.chat
    action_labels = local_chat_capabilities.action_labels
    _action_label_snapshot = local_chat_capabilities._action_label_snapshot
    resume_chat_integrations = local_chat_api.resume_chat_integrations
    resume_chat_human = local_chat_human.resume_chat_human
    pending_chat_human = local_chat_human.pending_chat_human
    _expire_human_challenges = local_chat_human._expire_human_challenges

    _invoke_chat_action = local_chat_execution._invoke_chat_action
    _chat_identity = staticmethod(local_chat_execution._chat_identity)
    _raise_chat_problem = staticmethod(local_chat_execution._raise_chat_problem)
    _validate_chat_action = staticmethod(local_chat_execution._validate_chat_action)
    _require_chat_private_inputs = local_chat_execution._require_chat_private_inputs
    _validate_chat_context = local_chat_execution._validate_chat_context

    _commit_suspension = local_chat_pause._commit_suspension
    _integration_response = local_chat_pause._integration_response
    _human_response = local_chat_pause._human_response
    _purge_human_generation = local_chat_pause._purge_human_generation
    _purge_human_pending = local_chat_pause._purge_human_pending
    _terminal_human_failure = local_chat_pause._terminal_human_failure
    _pause_integration = local_chat_pause._pause_integration
    _pause_human = local_chat_pause._pause_human

    _action_integration_generations = local_chat_private._action_integration_generations
    _refresh_oauth_integration = local_chat_private._refresh_oauth_integration
    _resolve_action_integrations = local_chat_private._resolve_action_integrations
    _require_action_rpc_envelope = local_chat_private._require_action_rpc_envelope
    _raise_integration_problem = staticmethod(local_chat_private._raise_integration_problem)
    list_assistant_integrations = local_chat_private.list_assistant_integrations
    start_assistant_integration_authorization = local_chat_private.start_assistant_integration_authorization
    _current_integration_declaration = local_chat_private._current_integration_declaration
    complete_cloudflare_oauth_callback = local_chat_private.complete_cloudflare_oauth_callback
    cancel_assistant_integration_authorization = local_chat_private.cancel_assistant_integration_authorization
    disconnect_assistant_integration = local_chat_private.disconnect_assistant_integration
    pending_chat_integrations = local_chat_private.pending_chat_integrations

    stop_chat = local_chat_resume.stop_chat

    _run_chat_segment = local_chat_segment._run_chat_segment
    _run_chat_segment_with_metadata = local_chat_segment._run_chat_segment_with_metadata

    _chat_file_metadata = local_chat_state._chat_file_metadata
    _chat_setup = local_chat_state._chat_setup

    def _active_assistant_genesis(self, active):
        return self.assistant_lifecycle._active_assistant_genesis(active)

    def _admit_assistant_allowed_hosts(self, container, spec):
        return self.assistant_lifecycle._admit_assistant_allowed_hosts(container, spec)

    _active_chat_assistants = local_chat_state._active_chat_assistants
    _delete_assistant_integration_state = local_chat_state._delete_assistant_integration_state
    _delete_team_integration_state = local_chat_state._delete_team_integration_state
    _delete_all_integration_state = local_chat_state._delete_all_integration_state
    _retain_declared_assistant_integration_state = local_chat_state._retain_declared_assistant_integration_state
    _raise_chat_continuation_problem = staticmethod(local_chat_state._raise_chat_continuation_problem)
    _persist_chat_continuation = local_chat_state._persist_chat_continuation
    _restore_chat_continuation = local_chat_state._restore_chat_continuation
    _purge_expired_human_continuation = local_chat_state._purge_expired_human_continuation
    _restore_all_chat_continuations = local_chat_state._restore_all_chat_continuations
    _delete_chat_continuation = local_chat_state._delete_chat_continuation
    _clear_chat_continuations = local_chat_state._clear_chat_continuations


def _account_egress_transport() -> integration_broker.FixedBrokerTransport:
    proxy_host = os.environ.get("SHIMPZ_OAUTH_BROKER_PROXY_HOST")
    capability_file = os.environ.get("SHIMPZ_OAUTH_BROKER_PROXY_CAPABILITY_FILE")
    if proxy_host is None or capability_file is None:
        raise RuntimeError("Local Account egress configuration is unavailable")
    return integration_broker.FixedBrokerTransport(
        proxy_host=proxy_host,
        proxy_capability_file=capability_file,
    )


@dataclass(frozen=True, slots=True)
class LocalControllerDependencies:
    inference_store: inference_config.InferenceConfigStore | None = None
    brain_runtime: brain_runtime_client.BrainRuntimeClient | None = None
    action_state: action_journal.ActionJournal | None = None
    assistant_integrations: integration_store.OAuthIntegrationStore | None = None
    integration_challenges: integration_challenges.IntegrationChallengeStore | None = None
    human_challenges: action_challenges.HumanChallengeStore | None = None
    oauth_pkce: integration_pkce.OAuthPKCEChallengeStore | None = None
    oauth_broker: integration_broker.OAuthBrokerClient | None = None
    oauth_service: integration_service.BrokeredOAuthIntegrationService | None = None
    chat_continuations: local_chat_continuation_store.EncryptedContinuationStore | None = None
    developers: local_developers.DevelopersClient | None = None
    artifact_trust: artifact_trust.ArtifactTrustVerifier | None = None
    assistant_updates: assistant_update.AssistantUpdateStore | None = None
    assistant_residues: assistant_update.AssistantResidueStore | None = None
    assistant_icons: icons.AssistantIconStore | None = None


class LocalController:
    list_assistants = local_assistant_api.list_assistants
    assistant_icon = local_assistant_api.assistant_icon
    install_publication = local_install_service.install_publication
    _install_bound_publication = local_install_service._install_bound_publication

    _purge_action_generation = local_team_lifecycle._purge_action_generation
    _team_assistant_containers = local_team_lifecycle._team_assistant_containers
    _validate_destroy_containers = local_team_lifecycle._validate_destroy_containers
    _delete_team_conversation = local_team_lifecycle._delete_team_conversation
    _remove_team_assistants = local_team_lifecycle._remove_team_assistants
    _delete_team_persistence = local_team_lifecycle._delete_team_persistence
    _delete_team_private_state = local_team_lifecycle._delete_team_private_state
    _remove_team_network = local_team_lifecycle._remove_team_network
    _clear_team_runtime_state = local_team_lifecycle._clear_team_runtime_state
    destroy_team = local_team_lifecycle.destroy_team
    _validate_reset_container = local_team_lifecycle._validate_reset_container
    _reset_inventory = local_team_lifecycle._reset_inventory
    _reset_assistant_identities = local_team_lifecycle._reset_assistant_identities
    _remove_space_resources = local_team_lifecycle._remove_space_resources
    reset_space = local_team_lifecycle.reset_space

    def __init__(
        self,
        client: docker.DockerClient,
        space_id: str,
        registry: PublicationRegistry,
        storage: team_storage.TeamStorage,
        dependencies: LocalControllerDependencies | None = None,
    ) -> None:
        dependencies = dependencies or LocalControllerDependencies()
        self.client = client
        self.space_id = validate_space_id(space_id)
        self.registry = registry
        self.storage = storage
        self.inference_store = dependencies.inference_store or inference_config.InferenceConfigStore(INFERENCE_ROOT)
        self.brain_runtime = dependencies.brain_runtime or brain_runtime_client.BrainRuntimeClient()
        self.action_state = (
            dependencies.action_state
            if dependencies.action_state is not None
            else action_journal.ActionJournal(LOCAL_ACTION_JOURNAL_PATH)
        )
        self.assistant_integrations = dependencies.assistant_integrations or integration_store.OAuthIntegrationStore()
        self.integration_challenges = (
            dependencies.integration_challenges or integration_challenges.IntegrationChallengeStore()
        )
        self.human_challenges = dependencies.human_challenges or action_challenges.HumanChallengeStore()
        self.oauth_pkce = dependencies.oauth_pkce or integration_pkce.OAuthPKCEChallengeStore()
        self.oauth_broker = dependencies.oauth_broker or integration_broker.OAuthBrokerClient(
            transport=_account_egress_transport(),
        )
        self.oauth_service = dependencies.oauth_service or integration_service.BrokeredOAuthIntegrationService(
            challenge=self.oauth_pkce,
            store=self.assistant_integrations,
            broker=self.oauth_broker,
        )
        self.chat_continuations = (
            dependencies.chat_continuations
            or local_chat_continuation_store.EncryptedContinuationStore(
                LOCAL_CHAT_CONTINUATIONS_STATE_PATH,
                LOCAL_CHAT_CONTINUATIONS_KEY_PATH,
            )
        )
        if (
            dependencies.developers is None
            or dependencies.artifact_trust is None
            or dependencies.assistant_updates is None
            or dependencies.assistant_residues is None
            or dependencies.assistant_icons is None
        ):
            raise RuntimeError("Local publication installation dependencies are unavailable")
        self.developers = dependencies.developers
        self.artifact_trust = dependencies.artifact_trust
        self.assistant_updates = dependencies.assistant_updates
        self.assistant_residues = dependencies.assistant_residues
        self.assistant_icons = dependencies.assistant_icons
        self._locks = tuple(threading.RLock() for _ in range(64))
        daemon_info = self._require_default_seccomp()
        self.cpuset_cpus = half_cpu_set(daemon_info.get("NCPU"))
        self._wire_collaborators()
        self.assistant_lifecycle._reconcile_egress_proxy_attachments()
        self.assistant_lifecycle.recover_updates()
        self.assistant_lifecycle.resume_assistants()
        self.chat_turn_service._restore_all_chat_continuations()

    def _wire_collaborators(self) -> None:
        assistant_lifecycle = AssistantLifecycle(
            AssistantLifecycleDependencies(
                client=getattr(self, "client", None),
                space_id=getattr(self, "space_id", None),
                registry=getattr(self, "registry", None),
                cpuset_cpus=getattr(self, "cpuset_cpus", None),
                lock_for=self._lock,
                invoke=self.invoke,
                list_assistants=self.list_assistants,
                developers=getattr(self, "developers", None),
                artifact_trust=getattr(self, "artifact_trust", None),
                updates=getattr(self, "assistant_updates", None),
                residues=getattr(self, "assistant_residues", None),
                icons=getattr(self, "assistant_icons", None),
            )
        )
        chat_turn_service = ChatTurnService(
            ChatTurnDependencies(
                space_id=getattr(self, "space_id", None),
                registry=getattr(self, "registry", None),
                storage=getattr(self, "storage", None),
                inference_store=getattr(self, "inference_store", None),
                brain_runtime=getattr(self, "brain_runtime", None),
                action_state=getattr(self, "action_state", None),
                assistant_integrations=getattr(self, "assistant_integrations", None),
                integration_challenges=getattr(self, "integration_challenges", None),
                human_challenges=getattr(self, "human_challenges", None),
                oauth_pkce=getattr(self, "oauth_pkce", None),
                oauth_service=getattr(self, "oauth_service", None),
                chat_continuations=getattr(self, "chat_continuations", None),
                lock_for=self._lock,
                raise_storage_problem=self._raise_storage_problem,
            )
        )
        assistant_lifecycle.chat_turn_service = chat_turn_service
        chat_turn_service.assistant_lifecycle = assistant_lifecycle
        self.assistant_lifecycle = assistant_lifecycle
        self.chat_turn_service = chat_turn_service

    def _require_default_seccomp(self) -> dict:
        try:
            info = self.client.info()
            options = info.get("SecurityOptions", [])
        except DockerException as exc:
            raise RuntimeError("the Docker daemon is unavailable") from exc
        if not any(isinstance(option, str) and option.startswith("name=seccomp") for option in options):
            raise RuntimeError("the Docker daemon default seccomp profile is required")
        return info

    def _lock(self, team_id: str) -> threading.RLock:
        slot = hashlib.sha256(team_id.encode("ascii")).digest()[0] % len(self._locks)
        return self._locks[slot]

    def list_teams(self) -> dict[str, list[dict[str, str]]]:
        filters = {
            "label": [
                f"{MANAGED_LABEL}=1",
                f"{PROFILE_LABEL}={PROFILE}",
                f"{SPACE_LABEL}={self.space_id}",
                f"{KIND_LABEL}=team",
            ]
        }
        teams: list[dict[str, str]] = []
        try:
            networks = self.client.networks.list(filters=filters)
        except DockerException as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker is unavailable",
                code="docker-unavailable",
            ) from exc
        for network in networks:
            labels = network.attrs.get("Labels") or {}
            team_id = labels.get(TEAM_LABEL)
            if not isinstance(team_id, str):
                raise ApiProblem(HTTPStatus.CONFLICT, "Team resource ownership conflict", code="ownership-conflict")
            validate_team_id(team_id)
            team_name = self.assistant_lifecycle._validate_network(network, team_id)
            teams.append({"team_id": team_id, "team_name": team_name, "status": "running"})
        teams.sort(key=lambda item: item["team_id"])
        return {"teams": teams}

    def create_team(self, team_id: str, team_name: str) -> dict[str, object]:
        team_id = validate_team_id(team_id)
        team_name = validate_team_name(team_name)
        with self._lock(team_id):
            existing = self.assistant_lifecycle._network(team_id, required=False)
            if existing is not None:
                existing_name = self.assistant_lifecycle._validate_network(existing, team_id)
                if existing_name != team_name:
                    raise ApiProblem(
                        HTTPStatus.CONFLICT,
                        "Team id already belongs to a different name",
                        code="team-name-conflict",
                    )
                return {"team_id": team_id, "team_name": team_name, "status": "running", "created": False}
            try:
                # A Team identity starts empty even after a daemon crash removed its network
                # before the previous lifecycle could clean the dedicated storage volume.
                self.storage.destroy(team_id)
            except team_storage.StorageError as exc:
                self._raise_storage_problem(exc)
            try:
                self.inference_store.delete(team_id)
            except inference_config.InferenceConfigError as exc:
                self._raise_inference_problem(exc)
            try:
                labels = self.assistant_lifecycle._base_labels(team_id, "team")
                labels[TEAM_NAME_LABEL] = team_name
                network = self.client.networks.create(
                    self.assistant_lifecycle._network_name(team_id),
                    driver="bridge",
                    internal=True,
                    attachable=False,
                    check_duplicate=True,
                    labels=labels,
                )
            except APIError as exc:
                # A concurrent idempotent creator is safe only when the resulting
                # resource proves the exact ownership/profile labels.
                network = self.assistant_lifecycle._network(team_id, required=False)
                if network is None:
                    raise ApiProblem(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "Docker could not create the Team",
                        code="docker-create-failed",
                    ) from exc
                existing_name = self.assistant_lifecycle._validate_network(network, team_id)
                if existing_name != team_name:
                    raise ApiProblem(
                        HTTPStatus.CONFLICT,
                        "Team id already belongs to a different name",
                        code="team-name-conflict",
                    ) from exc
                return {"team_id": team_id, "team_name": team_name, "status": "running", "created": False}
            self.assistant_lifecycle._validate_network(network, team_id)
            return {"team_id": team_id, "team_name": team_name, "status": "running", "created": True}

    @staticmethod
    def _raise_storage_problem(exc: team_storage.StorageError) -> NoReturn:
        if isinstance(exc, team_storage.StorageQuotaError):
            raise ApiProblem(
                HTTPStatus.INSUFFICIENT_STORAGE,
                str(exc),
                code="storage-quota-exceeded",
            ) from exc
        if isinstance(exc, team_storage.StorageNotFoundError):
            raise ApiProblem(HTTPStatus.NOT_FOUND, "file not found", code="file-not-found") from exc
        if isinstance(exc, team_storage.StorageInputError):
            raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc), code="invalid-file") from exc
        raise ApiProblem(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "Team storage failed its safety checks",
            code="storage-safety-failed",
        ) from exc

    @staticmethod
    def _raise_inference_problem(exc: inference_config.InferenceConfigError) -> NoReturn:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team model provider metadata is unavailable",
            code="inference-store-failed",
        ) from exc

    def inference_status(self, team_id: str) -> dict[str, str]:
        team_id = validate_team_id(team_id)
        with self._lock(team_id):
            self.assistant_lifecycle._network(team_id)
            try:
                config = self.inference_store.load(team_id)
            except inference_config.InferenceConfigError as exc:
                raise ApiProblem(
                    HTTPStatus.CONFLICT,
                    "Team model provider is not configured",
                    code="inference-not-configured",
                ) from exc
        return {"team_id": team_id, "provider": config.provider, "model": config.model}

    def configure_inference(self, team_id: str, body: object) -> dict[str, str]:
        team_id = validate_team_id(team_id)
        if not isinstance(body, dict) or set(body) != {"provider", "model"}:
            raise ApiProblem(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "inference requires only provider and model",
                code="invalid-body",
            )
        try:
            config = inference_config.normalize(body["provider"], body["model"])
        except inference_config.InferenceConfigError as exc:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, str(exc), code="invalid-inference") from exc
        with self._lock(team_id):
            self.assistant_lifecycle._network(team_id)
            try:
                self.inference_store.save(team_id, config)
            except inference_config.InferenceConfigError as exc:
                self._raise_inference_problem(exc)
        return {"team_id": team_id, "provider": config.provider, "model": config.model}

    def put_file(
        self,
        team_id: str,
        filename: object,
        content: bytes,
        media_type: object,
    ) -> dict[str, object]:
        team_id = validate_team_id(team_id)
        with self._lock(team_id):
            self.assistant_lifecycle._network(team_id)
            try:
                stored = self.storage.put(team_id, filename, content, media_type)
            except team_storage.StorageError as exc:
                self._raise_storage_problem(exc)
        return {"team_id": team_id, "file": stored}

    def list_files(self, team_id: str) -> dict[str, object]:
        team_id = validate_team_id(team_id)
        with self._lock(team_id):
            self.assistant_lifecycle._network(team_id)
            try:
                listing = self.storage.list(team_id)
            except team_storage.StorageError as exc:
                self._raise_storage_problem(exc)
        return {"team_id": team_id, **listing}

    def delete_file(self, team_id: str, file_id: object) -> dict[str, object]:
        team_id = validate_team_id(team_id)
        with self._lock(team_id):
            self.assistant_lifecycle._network(team_id)
            try:
                result = self.storage.delete(team_id, file_id)
            except team_storage.StorageError as exc:
                self._raise_storage_problem(exc)
        return {"team_id": team_id, **result}

    def list_registry(self) -> dict[str, list[dict[str, object]]]:
        return {
            "assistants": [
                {
                    "id": spec.assistant_id,
                    "title": spec.name,
                    "summary": spec.summary,
                    "actions": sorted(spec.actions),
                }
                for spec in self.registry.catalog()
            ]
        }

    def health(self) -> dict[str, str]:
        try:
            if self.client.ping() is not True:
                raise DockerException("unexpected Docker ping response")
        except DockerException as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker is unavailable",
                code="docker-unavailable",
            ) from exc
        return {"status": "ok"}

    def invoke(
        self,
        team_id: str,
        assistant_id: str,
        action: str,
        payload: object,
        responses: tuple[Mapping[str, object], ...] = (),
        protected_values: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        team_id = validate_team_id(team_id)
        spec = self.assistant_lifecycle._resolve(team_id, assistant_id)
        action_spec = spec.actions.get(action)
        if action_spec is None:
            raise ApiProblem(
                action_execution.UNDECLARED_ACTION_STATUS, "Action is not declared", code="action-not-declared"
            )
        try:
            safe_payload = validate_action_payload(action_spec, "input", payload)
        except ValueError as exc:
            raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc), code="invalid-action-input") from exc
        with self._lock(team_id):
            network = self.assistant_lifecycle._network(team_id)
            container = self.assistant_lifecycle._assistant_container(team_id, assistant_id)
            self.assistant_lifecycle._validate_container(container, team_id, spec, network.name)
            if container.id in self.assistant_lifecycle._blocked_action_workloads:
                raise ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "Assistant Action execution is blocked until this Assistant is reinstalled",
                    code="assistant-action-blocked",
                )
            container.reload()
            if container.status != "running":
                raise ApiProblem(HTTPStatus.CONFLICT, "Assistant is not running", code="assistant-not-running")
            with self.chat_turn_service._active_chat_guard:
                active = self.chat_turn_service._active_action_containers.get(team_id)
                frozen_container = active[1] if active is not None else None
            if frozen_container is not None and frozen_container.id != container.id:
                raise ApiProblem(
                    HTTPStatus.CONFLICT,
                    "Team capabilities changed; retry",
                    code="team-context-changed",
                )
            integration_values = self.chat_turn_service._resolve_action_integrations(team_id, spec, action)
            local_audit.record_request(
                "assistant-action",
                result="ok",
                team_id=team_id,
                assistant=assistant_id,
                detail=f"started:{action}",
            )
            rpc_payload = {
                "input": safe_payload,
                "integrations": action_execution.integration_access_tokens(integration_values),
            }
            if responses:
                rpc_payload["responses"] = responses
        try:
            raw_result = self.assistant_lifecycle._rpc(
                container,
                action,
                rpc_payload,
            )
        except ApiProblem:
            local_audit.record_request(
                "assistant-action",
                result="error",
                team_id=team_id,
                assistant=assistant_id,
                detail=f"failed:{action}",
            )
            raise
        try:
            projected = action_execution.project_rpc_result(
                raw_result,
                integration_values,
                lambda value: validate_action_payload(action_spec, "output", value),
                action_spec.human_requests,
                protected_values,
                authorization_requested=any(
                    response.get("kind") in action_human.AUTHORIZATION_KINDS for response in responses
                ),
            )
        except action_execution.RpcSecretExposureError:
            local_audit.record_request(
                "assistant-action",
                result="error",
                team_id=team_id,
                assistant=assistant_id,
                detail=f"secret-exposure:{action}",
            )
            raise ApiProblem(
                HTTPStatus.BAD_GATEWAY,
                "the Assistant returned an unsafe result",
                code="assistant-secret-exposure",
            ) from None
        except action_execution.RpcInvalidResultError as exc:
            local_audit.record_request(
                "assistant-action",
                result="error",
                team_id=team_id,
                assistant=assistant_id,
                detail=f"invalid-output:{action}",
            )
            raise ApiProblem(
                HTTPStatus.BAD_GATEWAY,
                "the Assistant returned an invalid result",
                code="invalid-action-output",
            ) from exc
        local_audit.record_request(
            "assistant-action",
            result="ok",
            team_id=team_id,
            assistant=assistant_id,
            detail=f"completed:{action}",
        )
        return {"assistant": assistant_id, "action": action, "result": projected}


def main() -> int:
    try:
        space_id = os.environ["SHIMPZ_SPACE_ID"]
        token = local_token_store.ensure_token()
        brain_runtime_token_store.ensure()
        client = docker.from_env(timeout=REQUEST_TIMEOUT_SECONDS)
        registry = PublicationRegistry(bindings.DynamicAssistantStore(LOCAL_PUBLICATION_BINDINGS_PATH))
        storage = team_storage.TeamStorage(STORAGE_ROOT)
        controller = LocalController(
            client,
            space_id,
            registry,
            storage,
            LocalControllerDependencies(
                developers=local_developers.DevelopersClient(),
                artifact_trust=artifact_trust.ArtifactTrustVerifier(
                    client,
                    binary="/opt/venv/bin/cosign",
                    credentials=registry_auth.AnonymousRegistryAccess(),
                    trust_root=LOCAL_COSIGN_TRUST_ROOT,
                ),
                assistant_updates=assistant_update.AssistantUpdateStore(LOCAL_ASSISTANT_UPDATES_PATH),
                assistant_residues=assistant_update.AssistantResidueStore(LOCAL_ASSISTANT_RESIDUES_PATH),
                assistant_icons=icons.AssistantIconStore(LOCAL_PUBLICATION_ICONS_PATH),
            ),
        )
        server = BoundedServer(("0.0.0.0", LISTEN_PORT), Handler, controller, token)
        updater = local_automatic_updates.AutomaticAssistantUpdater(
            controller,
            record=_record_automatic_update,
        )
    except (KeyError, RuntimeError, DockerException) as exc:
        print(f"team-local: startup failed: {exc}", file=sys.stderr, flush=True)
        return 1
    local_audit.record(
        "startup",
        result="ok",
        principal=local_audit.AuditPrincipal("team-local", "machine"),
    )
    try:
        updater.start()
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        updater.close()
        server.server_close()
        client.close()
        local_audit.close()
    return 0


def _record_automatic_update(
    team_id: str | None,
    assistant_id: str | None,
    result: str,
    detail: str,
) -> None:
    local_audit.record(
        "assistant-update",
        result=result,
        principal=local_audit.AuditPrincipal("team-local", "machine"),
        team_id=team_id,
        assistant=assistant_id,
        detail=detail,
    )


if __name__ == "__main__":
    raise SystemExit(main())
