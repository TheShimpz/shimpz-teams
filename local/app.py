"""Minimal Docker controller for one locally owned Shimpz Space.

This is intentionally separate from the hosted Team controller.  An empty Team is
one labeled internal network; its only runnable resources are installed,
digest-pinned published Assistants with declared Action contracts.
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import sys
import threading
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import NoReturn

import docker
from docker.errors import DockerException

from action import challenges as action_challenges
from action import execution as action_execution
from action import journal as action_journal
from action import stored_input as action_stored_input
from assistant import genesis as assistant_genesis
from assistant import manifest as assistant_manifest
from assistant.spec import validate_action_payload
from core.container import network as network_policy
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
from local import inference as local_inference
from local import labels as local_labels
from local import lifecycle as local_team_lifecycle
from local import names as local_names
from local import token as local_token_store
from local.assistant import api as local_assistant_api
from local.assistant import egress as local_egress
from local.assistant import lifecycle as local_assistant_lifecycle
from local.assistant import resources as local_assistant_resources
from local.assistant import rpc as local_assistant_rpc
from local.chat import continuation_store as local_chat_continuation_store
from local.chat import execution as local_chat_execution
from local.chat import state as local_chat_state
from local.chat.service import ChatTurnService
from local.composition import AssistantLifecycleDependencies, ChatTurnDependencies
from local.errors import ApiProblemError as ApiProblem
from local.http.server import REQUEST_TIMEOUT_SECONDS, BoundedServer, Handler
from local.install import automatic as local_automatic_updates
from local.install import collector as local_snapshot_collector
from local.install import developers as local_developers
from local.install import inventory as local_snapshot_inventory
from local.install import preview as local_snapshot_preview
from local.install import service as local_install_service
from local.install import snapshots as local_snapshots
from local.install.registry import AssistantRegistry
from local.labels import (
    IMAGE_LABEL as _LOCAL_IMAGE_LABEL,
)
from local.routine import lifecycle as local_routine_lifecycle
from local.routine import proposal as local_routine_proposal
from local.routine import store as local_routine_store
from local.routine import watchdog as local_routine_watchdog
from local.validation import brain_thread_id as _local_brain_thread_id
from local.validation import (
    half_cpu_set,
    validate_space_id,
    validate_team_id,
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


class AssistantLifecycle:
    """Own Assistant admission, resources, RPC, and egress lifecycle."""

    def __init__(self, dependencies: AssistantLifecycleDependencies) -> None:
        self.client = dependencies.client
        self.space_id = dependencies.space_id
        self.registry = dependencies.registry
        self.cpuset_cpus = dependencies.cpuset_cpus
        self._lock = dependencies.lock_for
        self.invoke = dependencies.invoke
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
    _queue_published_residue = local_assistant_lifecycle._queue_published_residue
    _commit_replacement = local_assistant_lifecycle._commit_replacement
    _run_against_local_team_chat = local_assistant_lifecycle._run_against_local_team_chat
    _install_assistant_unguarded = local_assistant_lifecycle._install_assistant_unguarded
    _uninstall_assistant_unguarded = local_assistant_lifecycle._uninstall_assistant_unguarded
    install_assistant = local_assistant_lifecycle.install_assistant
    update_assistant = local_assistant_lifecycle.update_assistant
    _recover_update_target = local_assistant_lifecycle._recover_update_target
    recover_updates = local_assistant_lifecycle.recover_updates
    resume_assistants = local_assistant_lifecycle.resume_assistants
    uninstall_assistant = local_assistant_lifecycle.uninstall_assistant
    install_fresh_local = local_assistant_lifecycle.install_fresh_local
    replace_published_with_local = local_assistant_lifecycle.replace_published_with_local

    _assistant_filters = local_assistant_resources._assistant_filters
    _assistant_container = local_assistant_resources._assistant_container
    _assistant_specs = local_assistant_resources._assistant_specs
    _resolve = local_assistant_resources._resolve
    _image_labels_valid = staticmethod(local_assistant_resources._image_labels_valid)
    _trusted_image = local_assistant_resources._trusted_image
    _staged_image = local_assistant_resources._staged_image
    _assistant_image = local_assistant_resources._assistant_image
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
    assistant_stored_inputs: action_stored_input.StoredInputStore | None = None
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
    routine_store: local_routine_store.RoutineStore | None = None
    team_names: local_names.TeamNameStore | None = None


class LocalController:
    _raise_inference_problem = staticmethod(local_inference._raise_inference_problem)
    inference_status = local_inference.inference_status
    configure_inference = local_inference.configure_inference
    list_assistants = local_assistant_api.list_assistants
    assistant_icon = local_assistant_api.assistant_icon
    install_publication = local_install_service.install_publication
    _install_bound_publication = local_install_service._install_bound_publication
    list_local_snapshots = local_install_service.list_local_snapshots
    local_snapshot_icon = local_install_service.local_snapshot_icon
    install_local_snapshot = local_install_service.install_local_snapshot
    install_fresh_local_snapshot = local_install_service.install_fresh_local_snapshot

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
    _delete_team_routines = local_routine_lifecycle.delete_team_routines
    _delete_all_routines = local_routine_lifecycle.delete_all_routines
    list_teams = local_names.list_teams
    create_team = local_names.create_team
    rename_team = local_names.rename_team

    def __init__(
        self,
        client: docker.DockerClient,
        space_id: str,
        registry: AssistantRegistry,
        storage: team_storage.TeamStorage,
        dependencies: LocalControllerDependencies | None = None,
    ) -> None:
        dependencies = dependencies or LocalControllerDependencies()
        self.client = client
        self.space_id = validate_space_id(space_id)
        self.registry = registry
        self.storage = storage
        self.inference_store = dependencies.inference_store or inference_config.InferenceConfigStore(INFERENCE_ROOT)
        self.team_names = dependencies.team_names or local_names.TeamNameStore(INFERENCE_ROOT)
        self.routine_store = dependencies.routine_store or local_routine_store.RoutineStore()
        self.routine_proposals = local_routine_proposal.ProposalBook()
        self.brain_runtime = dependencies.brain_runtime or brain_runtime_client.BrainRuntimeClient()
        self.action_state = (
            dependencies.action_state
            if dependencies.action_state is not None
            else action_journal.ActionJournal(LOCAL_ACTION_JOURNAL_PATH)
        )
        self.assistant_integrations = dependencies.assistant_integrations or integration_store.OAuthIntegrationStore()
        self.assistant_stored_inputs = dependencies.assistant_stored_inputs or action_stored_input.StoredInputStore()
        self.integration_challenges = (
            dependencies.integration_challenges or integration_challenges.IntegrationChallengeStore()
        )
        self.human_challenges = dependencies.human_challenges or action_challenges.HumanChallengeStore()
        self.routine_human_challenges = action_challenges.HumanChallengeStore()
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
        # The Space-wide Team namespace (ADR-0088): always taken before any Team lock.
        self._names_lock = threading.RLock()
        daemon_info = self._require_default_seccomp()
        self.cpuset_cpus = half_cpu_set(daemon_info.get("NCPU"))
        try:
            local_platform = local_snapshots.platform_from_info(daemon_info)
        except local_snapshots.LocalSnapshotError as exc:
            raise RuntimeError("the Docker daemon architecture is unsupported") from exc
        self.local_snapshot_inventory = local_snapshot_inventory.LocalSnapshotInventory(client, local_platform)
        self.local_snapshot_previews = local_snapshot_preview.LocalSnapshotPreviewCache(client, local_platform)
        self.local_snapshot_collector = local_snapshot_collector.SupersededSnapshotCollector(client, registry)
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
                team_names=getattr(self, "team_names", None),
                brain_runtime=getattr(self, "brain_runtime", None),
                action_state=getattr(self, "action_state", None),
                assistant_integrations=getattr(self, "assistant_integrations", None),
                assistant_stored_inputs=getattr(self, "assistant_stored_inputs", None),
                integration_challenges=getattr(self, "integration_challenges", None),
                human_challenges=getattr(self, "human_challenges", None),
                routine_human_challenges=getattr(self, "routine_human_challenges", None),
                oauth_pkce=getattr(self, "oauth_pkce", None),
                oauth_service=getattr(self, "oauth_service", None),
                chat_continuations=getattr(self, "chat_continuations", None),
                lock_for=self._lock,
                raise_storage_problem=self._raise_storage_problem,
                routine_store=getattr(self, "routine_store", None),
                routine_proposals=getattr(self, "routine_proposals", None),
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
        evidence: action_execution.ActionInvocationEvidence | None = None,
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
            private = action_execution.resolve_invocation_evidence(
                evidence,
                lambda: self.chat_turn_service._resolve_action_integrations(team_id, spec, action),
                lambda: (
                    self.chat_turn_service._resolve_action_stored_inputs(team_id, spec, action)
                    if action_spec.stored_inputs
                    else {}
                ),
            )
            local_audit.record_request(
                "assistant-action",
                result="ok",
                team_id=team_id,
                assistant=assistant_id,
                detail=f"started:{action}",
            )
            rpc_payload = {
                "input": safe_payload,
                "integrations": action_execution.integration_access_tokens(private.integrations),
                "stored_inputs": private.stored_inputs,
            }
            if private.transcript.responses:
                rpc_payload["responses"] = private.transcript.payloads()
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
            projected = local_chat_execution.project_action_result(
                raw_result,
                action_spec,
                private,
                validate_action_payload,
            )
        except action_execution.StoredInputRejectedError as exc:
            local_chat_execution.clear_rejected_stored_input(
                self.assistant_stored_inputs,
                team_id,
                assistant_id,
                action,
                exc.stored_input,
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
        try:
            local_chat_execution.seal_stored_inputs(
                self.assistant_stored_inputs,
                team_id,
                assistant_id,
                spec,
                action_spec,
                private,
            )
        except (KeyError, action_stored_input.StoredInputStoreError) as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant Stored Input could not be saved",
                code="assistant-stored-input-state-unavailable",
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
        network_policy.require_image_reference(
            network_policy.ASSISTANT_EGRESS_IMAGE,
            setting="SHIMPZ_ASSISTANT_EGRESS_IMAGE",
        )
        token = local_token_store.ensure_token()
        brain_runtime_token_store.ensure()
        client = docker.from_env(timeout=REQUEST_TIMEOUT_SECONDS)
        registry = AssistantRegistry(
            bindings.DynamicAssistantStore(
                LOCAL_PUBLICATION_BINDINGS_PATH,
                local_record_validator=local_snapshots.validate_record,
            )
        )
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
                assistant_updates=assistant_update.AssistantUpdateStore(
                    LOCAL_ASSISTANT_UPDATES_PATH,
                    local_record_validator=local_snapshots.validate_record,
                ),
                assistant_residues=assistant_update.AssistantResidueStore(LOCAL_ASSISTANT_RESIDUES_PATH),
                assistant_icons=icons.AssistantIconStore(LOCAL_PUBLICATION_ICONS_PATH),
            ),
        )
        server = BoundedServer(("0.0.0.0", LISTEN_PORT), Handler, controller, token)
        controller.local_snapshot_inventory.warm()
        updater = local_automatic_updates.AutomaticAssistantUpdater(
            controller,
            record=_record_automatic_update,
            activity=server.activity,
        )
    except (KeyError, RuntimeError, DockerException) as exc:
        print(f"team-local: startup failed: {exc}", file=sys.stderr, flush=True)
        return 1
    local_audit.record(
        "startup",
        result="ok",
        principal=local_audit.AuditPrincipal("team-local", "machine"),
    )
    # Nothing runs a Routine segment after a restart: recover every leased run before serving (ADR-0086).
    local_routine_watchdog.check(controller.chat_turn_service, startup=True)
    watchdog = local_routine_watchdog.RoutineWatchdog(controller.chat_turn_service)
    # Docker stops PID 1 with SIGTERM, which is otherwise ignored; route it into the same graceful shutdown.
    signal.signal(signal.SIGTERM, _terminate)
    try:
        updater.start()
        watchdog.start()
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        watchdog.close()
        updater.close()
        server.server_close()
        client.close()
        local_audit.close()
    return 0


def _terminate(_signum: int, _frame: object) -> NoReturn:
    raise KeyboardInterrupt


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
