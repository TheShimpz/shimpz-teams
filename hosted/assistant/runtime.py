"""Hosted Assistant contracts, RPC, private state, and Action execution."""

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import NoReturn

import docker
import docker.errors

from action import dispatch as action_dispatch
from action import execution as action_execution
from action import failure as action_failure
from action import files as action_files
from action import human as action_human
from action import journal as action_journal
from action import provider as action_provider
from action import stored_input as action_stored_input
from assistant import manifest as assistant_manifest
from assistant import spec as assistant_registry
from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from core.container import network as network_policy
from egress import policy as egress_policy
from hosted import audit
from hosted import container as container_spec
from hosted import state as runtime_state
from hosted.assistant import lifecycle as assistant_lifecycle
from hosted.team import resources as hosted_resources
from inference import client as brain_runtime_client
from inference import integration_secrets as integration_secrets_client
from inference import usage as brain_usage
from integrations import flow as integration_flow
from integrations import store as integration_store
from protocol.http.v1 import payload as http_payload
from storage import files as team_storage

# ── Controller-owned Assistant chat ─────────────────────────────────────────────────────────────
MAX_INBOX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILE_BODY_BYTES = MAX_INBOX_FILE_BYTES
MAX_CHAT_FILES = http_payload.MAX_CHAT_FILES
MAX_CHAT_ASSISTANTS = 16
CHAT_PAUSED_STATUSES = chat_turn_engine.CHAT_PAUSED_STATUSES


@dataclass(frozen=True, slots=True)
class _ActiveAssistant:
    assistant_id: str
    contract: assistant_registry.AssistantContract
    container: object
    image: str = ""
    version: str = ""
    summary: str = ""


@dataclass(frozen=True, slots=True)
class _HostedAssistantSpec:
    """Small adapter for the closed integration contract."""

    assistant_id: str
    version: str
    name: str
    summary: str
    actions: dict[str, object]
    integrations: dict[str, assistant_registry.IntegrationSpec]
    stored_inputs: dict[str, assistant_registry.StoredInputSpec]


@dataclass(frozen=True, slots=True)
class _HostedActionSpec:
    integrations: tuple[str, ...]
    stored_inputs: tuple[str, ...]
    summary: str
    human_requests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _HostedAssistantBinding:
    spec: _HostedAssistantSpec


@dataclass(frozen=True, slots=True)
class _PendingHostedChat:
    """Process-local state for one private-input-gated Hosted Team turn."""

    continuation: chat_orchestrator.ChatContinuation
    assistant_ids: tuple[str, ...]
    file_ids: tuple[str, ...]
    owner: str
    identity: tuple[object, ...]
    transcripts: tuple[action_human.ActionTranscript, ...] = ()
    requests_used: int = 0
    # The interface language the turn's start pinned (ADR-0091).
    locale: str | None = None
    # What the paused turn consumed so far (ADR-0082).
    usage: brain_usage.TurnUsage | None = None
    # The fingerprint of the Action batch a human request paused, which ending the turn removes exactly.
    paused_batch: str | None = None


def _hosted_integration_spec(active: _ActiveAssistant) -> _HostedAssistantSpec:
    return _HostedAssistantSpec(
        assistant_id=active.assistant_id,
        version=active.version,
        name=active.contract.name,
        summary=active.summary,
        actions={
            action_id: _HostedActionSpec(
                tuple(getattr(action, "integrations", ())),
                tuple(getattr(action, "stored_inputs", ())),
                str(getattr(action, "summary", "")),
                tuple(getattr(action, "human_requests", ())),
            )
            for action_id, action in active.contract.actions.items()
        },
        integrations=getattr(active.contract, "integrations", {}),
        stored_inputs=getattr(active.contract, "stored_inputs", {}),
    )


def _integration_bindings(
    bindings: dict[str, _ActiveAssistant],
) -> dict[str, _HostedAssistantBinding]:
    return {
        assistant_id: _HostedAssistantBinding(_hosted_integration_spec(active))
        for assistant_id, active in bindings.items()
    }


def _hosted_action_identity(active: _ActiveAssistant) -> tuple[object, object]:
    config = getattr(active.container, "attrs", {}).get("Config", {})
    image = config.get("Image") if isinstance(config, dict) else None
    if not isinstance(image, str) or not image:
        image = active.image
    return active.container.id, image


def _close_exec_stream(stream) -> None:
    action_execution.close_exec_stream(stream)


def _installed_assistant(
    team_id: str,
    assistant_id: object,
    inspect_memo: dict[str, object] | None = None,
    candidate=None,
    dynamic_bindings: dict[str, object] | None = None,
    egress_store=None,
):
    assistant_id, spec = assistant_lifecycle._resolve_team_assistant(team_id, assistant_id, dynamic_bindings)
    contract = spec.contract
    container = candidate
    if container is None:
        container = hosted_resources._get_container(container_spec.team_assistant_container_name(team_id, assistant_id))
    if container is None:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, f"Assistant {assistant_id!r} is not installed in this Team")
    with runtime_state._active_chat_guard:
        if (team_id, container.id) in runtime_state._blocked_action_workloads:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant Action execution is blocked until this Assistant is reinstalled",
            )
    if (
        not network_policy.assistant_identity_valid(container.attrs, team_id, assistant_id)
        or str(container.attrs.get("Config", {}).get("Image", "")) != spec.image
    ):
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "installed Assistant failed its identity contract")
    hosted_resources._require_running_team_isolation(
        container,
        inspect_memo,
        refreshed=True,
        workload_spec=spec,
    )
    allowed_hosts = assistant_lifecycle._require_assistant_allowed_hosts(spec, container)
    current_egress_store = egress_store if egress_store is not None else assistant_lifecycle._egress_store()
    token = assistant_lifecycle._validate_admitted_egress(team_id, assistant_id, allowed_hosts, current_egress_store)
    assistant_lifecycle._validate_assistant_proxy_environment(container, token, allowed_hosts, current_egress_store)
    return assistant_id, contract, container


def _active_team_assistants(team_id: str) -> tuple[_ActiveAssistant, ...]:
    active: list[_ActiveAssistant] = []
    seen: set[str] = set()
    inspect_memo: dict[str, object] = {}
    try:
        installed = assistant_lifecycle._team_assistant_containers(team_id)
    except docker.errors.DockerException as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE, "installed Assistants could not be listed"
        ) from exc
    candidate_ids = tuple(
        assistant_id
        for candidate in installed
        if isinstance((assistant_id := (candidate.labels or {}).get("team.assistant")), str)
    )
    dynamic_bindings = assistant_lifecycle._dynamic_binding_snapshot(team_id, candidate_ids)
    egress_store = assistant_lifecycle._egress_store() if candidate_ids else None
    for candidate in installed:
        assistant_id = (candidate.labels or {}).get("team.assistant")
        if not isinstance(assistant_id, str):
            continue
        try:
            _resolved_id, spec = assistant_lifecycle._resolve_team_assistant(team_id, assistant_id, dynamic_bindings)
        except assistant_registry.AssistantSpecError:
            continue
        try:
            candidate.reload()
        except docker.errors.DockerException as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE, "installed Assistant could not be inspected"
            ) from exc
        if candidate.status != "running":
            continue
        if assistant_id in seen:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "duplicate installed Assistant identity")
        current_id, contract, container = _installed_assistant(
            team_id,
            assistant_id,
            inspect_memo,
            candidate,
            dynamic_bindings,
            egress_store,
        )
        seen.add(current_id)
        version = dynamic_bindings[current_id].resolution.get("assistant_version")
        if not isinstance(version, str) or not version:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "installed Assistant has no valid version")
        active.append(_ActiveAssistant(current_id, contract, container, spec.image, version, spec.summary))
    active.sort(key=lambda item: item.assistant_id)
    return tuple(active)


def _chat_assistant_ids(value: object) -> tuple[str, ...]:
    """Return one explicit, bounded Assistant scope; empty means Brain-only."""
    if not isinstance(value, list) or len(value) > MAX_CHAT_ASSISTANTS:
        raise runtime_state.ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            f"assistant_ids must contain at most {MAX_CHAT_ASSISTANTS} ids",
        )
    try:
        assistant_ids = tuple(assistant_registry.validate_assistant_id(item) for item in value)
    except assistant_registry.AssistantSpecError:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "assistant_ids contains an invalid id") from None
    if len(set(assistant_ids)) != len(assistant_ids):
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "assistant_ids must not contain duplicate ids")
    return tuple(sorted(assistant_ids))


def _select_team_assistants(
    active: tuple[_ActiveAssistant, ...],
    assistant_ids: tuple[str, ...],
) -> tuple[_ActiveAssistant, ...]:
    active_by_id = {assistant.assistant_id: assistant for assistant in active}
    try:
        return tuple(active_by_id[assistant_id] for assistant_id in assistant_ids)
    except KeyError:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "a selected Assistant is unavailable") from None


def _register_active_action(team_id: str, token: str, container) -> None:
    # Both refusals come before any RPC, so each carries Team's own pre-dispatch refusal: the journal then settles the
    # attempt as never run instead of uncertain.
    with runtime_state._active_chat_guard:
        if runtime_state._active_chat_tokens.get(team_id) != token or token in runtime_state._cancelled_chat_tokens:
            refused = action_dispatch.DispatchRefusedError("the turn was stopped before its Action could run")
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "brain turn stopped") from refused
        if team_id in runtime_state._active_action_container_ids:
            refused = action_dispatch.DispatchRefusedError("another Action of the Team was running")
            message = "Team already has an active Assistant Action"
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, message) from refused
        runtime_state._active_action_container_ids[team_id] = (token, container.id)


def _release_active_action(team_id: str, token: str, container_id: str) -> None:
    with runtime_state._active_chat_guard:
        if runtime_state._active_action_container_ids.get(team_id) == (token, container_id):
            runtime_state._active_action_container_ids.pop(team_id, None)


def _register_optional_action(team_id: str, token: str | None, container) -> None:
    if token is not None:
        _register_active_action(team_id, token, container)


def _release_optional_action(team_id: str, token: str | None, container_id: str) -> None:
    if token is not None:
        _release_active_action(team_id, token, container_id)


def _raise_if_rpc_cancelled(token: str | None, exc: BaseException | None = None) -> None:
    if token is not None and runtime_state._token_cancelled(token):
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "brain turn stopped") from exc


def _fail_stop_action(team_id: str, container) -> None:
    """Prove an ambiguous Assistant RPC can no longer execute before returning an error."""
    try:
        hosted_resources._fail_stop_team(container, timeout=3)
    except runtime_state.ApiError as exc:
        with runtime_state._active_chat_guard:
            runtime_state._blocked_action_workloads.add((team_id, container.id))
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant Action termination could not be proved; reinstall the Assistant",
        ) from exc


@dataclass(frozen=True, slots=True)
class AssistantRpcRequest:
    team_id: str
    container: object
    action_id: str
    payload: dict
    token: str | None
    # Team's answer to this attempt's provider calls (ADR-0106).
    broker: action_execution.ProviderBroker | None = None


def _assistant_rpc_exchange(request: AssistantRpcRequest) -> object:
    try:
        encoded = action_execution.encode_rpc_invocation(
            request.payload["input"],
            request.payload["stored_inputs"],
            request.payload["operation_id"],
            request.payload.get("responses", ()),
            request.payload.get("files", {}),
        )
    except (KeyError, ValueError) as exc:
        raise runtime_state.ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Action input is too large") from exc
    try:
        deadline = action_files.rpc_deadline(request.payload.get("files", {}))
    except action_files.FileDeliveryError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT, "the attached file is unavailable for this Action; attach it again"
        ) from exc
    return _exchange_registered(request, encoded, deadline)


def _exchange_registered(request: AssistantRpcRequest, encoded: bytes, deadline: float | None) -> object:
    team_id = request.team_id
    container = request.container
    token = request.token
    _register_optional_action(team_id, token, container)

    def close_stream(stream: object) -> None:
        with contextlib.suppress(Exception):
            _close_exec_stream(stream)

    try:
        try:
            return action_execution.rpc_exchange(
                container.id,
                [action_execution.ACTION_COMMAND, request.action_id],
                encoded,
                action_execution.RpcExchangeStrategy(
                    api=runtime_state._docker.api,
                    user=action_execution.ASSISTANT_RPC_USER,
                    workdir=container_spec.CONTAINER_TMP,
                    timeout=action_execution.RPC_TIMEOUT_SECONDS,
                    maximum=action_execution.MAX_RPC_RESPONSE_BYTES,
                    transport_errors=(docker.errors.DockerException,),
                    fail_stop=lambda: _fail_stop_action(team_id, container),
                    cancelled=lambda exc: _raise_if_rpc_cancelled(token, exc),
                    close_stream=close_stream,
                    deadline=deadline,
                    broker=request.broker,
                ),
            )
        except action_execution.RpcExchangeError as exc:
            message = action_execution.rpc_failure_message(exc.kind)[0]
            status = action_execution.rpc_failure_status(exc.kind)
            raise runtime_state.ApiError(status, message) from exc
    finally:
        _release_optional_action(team_id, token, container.id)


def _assistant_rpc(
    team_id: str,
    token: str,
    container,
    action_id: str,
    payload: dict,
    broker: action_execution.ProviderBroker | None = None,
) -> object:
    return _assistant_rpc_exchange(
        AssistantRpcRequest(
            team_id=team_id,
            container=container,
            action_id=action_id,
            payload=payload,
            token=token,
            broker=broker,
        )
    )


def _action_integration_generations(
    team_id: str,
    active: _ActiveAssistant,
    action_id: str,
) -> tuple[tuple[str, int], ...]:
    try:
        return action_execution.integration_generations(
            active.contract.actions,
            getattr(active.contract, "integrations", {}),
            action_id,
            lambda declarations: runtime_state._assistant_integrations.metadata(
                team_id,
                active.assistant_id,
                declarations,
            ),
        )
    except integration_store.OAuthIntegrationStoreError as exc:
        raise action_journal.ActionJournalConflictError("Action integration state is unavailable") from exc


def _resolve_action_stored_inputs(
    team_id: str,
    active: _ActiveAssistant,
    action_id: str,
) -> dict[str, action_stored_input.StoredInputValue]:
    try:
        return action_execution.resolve_action_stored_inputs(
            active.contract.actions,
            active.contract.stored_inputs,
            action_id,
            lambda stored_input_id, declaration: runtime_state._assistant_stored_inputs.resolve(
                team_id,
                active.assistant_id,
                stored_input_id,
                declaration.kind,
            ),
        )
    except action_stored_input.StoredInputStoreError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant Stored Input state is unavailable",
        ) from exc


def _action_stored_input_generations(
    team_id: str,
    active: _ActiveAssistant,
    request: brain_runtime_client.ActionRequest,
    origins: frozenset[str] | None = None,
) -> tuple[tuple[str, int], ...]:
    try:
        return action_execution.stored_input_generations(
            active.contract.actions,
            active.contract.stored_inputs,
            request.action,
            origins if origins is not None else frozenset({action_execution.stored_input_origin(request)}),
            lambda stored_input_id, declaration: runtime_state._assistant_stored_inputs.resolve(
                team_id,
                active.assistant_id,
                stored_input_id,
                declaration.kind,
            ),
        )
    except action_stored_input.StoredInputStoreError as exc:
        raise action_journal.ActionJournalConflictError("Action Stored Input state is unavailable") from exc


def _refresh_oauth_integration(
    _provider: str,
    _scopes: tuple[str, ...],
    _refresh_token: str,
    _broker_lease: str | None,
) -> object:
    """Hosted holds no OAuth client: an expired grant can only be authorized again."""
    raise integration_store.OAuthIntegrationReauthorizationError("OAuth integration requires reauthorization")


def _resolve_action_integrations(
    team_id: str,
    active: _ActiveAssistant,
    action_id: str,
) -> dict[str, dict[str, str]]:
    try:
        return integration_flow.resolve_action_integrations(
            team_id,
            _hosted_integration_spec(active),
            action_id,
            runtime_state._assistant_integrations,
            _refresh_oauth_integration,
        )
    except integration_flow.IntegrationFlowError as exc:
        raise runtime_state.ApiError(
            action_execution.INTEGRATION_PRECONDITION_STATUS, "Assistant integration is unavailable"
        ) from exc


def _require_hosted_action_rpc_envelope(
    team_id: str,
    bindings: dict[str, _ActiveAssistant],
    request: brain_runtime_client.ActionRequest,
    selected: dict[str, action_files.ActionFile] | None = None,
) -> Mapping[str, Mapping[str, object]]:
    active = bindings.get(request.assistant_id)
    if active is None:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Brain requested an unavailable Assistant")
    action_spec = active.contract.actions.get(request.action)
    try:
        file = action_files.action_file(
            () if action_spec is None else action_spec.input_files, request.input, selected or {}
        )
    except action_files.FileDeliveryError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT, "the attached file is unavailable for this Action; attach it again"
        ) from exc
    try:
        return action_execution.require_rpc_envelope(
            active,
            request,
            lambda binding, action_id: _resolve_action_integrations(team_id, binding, action_id),
            lambda binding, action_id: _resolve_action_stored_inputs(team_id, binding, action_id),
            file,
        )
    except ValueError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "Assistant Action input is too large",
        ) from exc


def _assistant_integration_inventory(
    team_id: str,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    with runtime_state._lock_for(team_id):
        hosted_resources._require_current_authorization(team_id, lease, require_isolation=False)
        try:
            payload = integration_flow.inventory_payload(
                team_id,
                _installed_assistant_specs(team_id),
                runtime_state._assistant_integrations,
            )
        except integration_store.OAuthIntegrationStoreError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Assistant integration state is unavailable"
            ) from exc
        except integration_flow.IntegrationFlowError as exc:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Assistant integration contract is unavailable") from exc
    return {"team_id": team_id, **payload}


def _assistant_stored_input_inventory(
    team_id: str,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    with runtime_state._lock_for(team_id):
        hosted_resources._require_current_authorization(team_id, lease, require_isolation=False)
        try:
            return runtime_state._assistant_stored_inputs.inventory(
                team_id,
                _installed_assistant_specs(team_id),
            )
        except action_stored_input.StoredInputStoreError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant Stored Input state is unavailable",
            ) from exc


def _installed_assistant_specs(team_id: str) -> tuple[_HostedAssistantSpec, ...]:
    specs: list[_HostedAssistantSpec] = []
    seen: set[str] = set()
    try:
        containers = assistant_lifecycle._team_assistant_containers(team_id)
    except docker.errors.DockerException as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE, "installed Assistants could not be listed"
        ) from exc
    for container in containers:
        assistant_id = (container.labels or {}).get("team.assistant")
        if not isinstance(assistant_id, str):
            continue
        try:
            _resolved_id, assistant_spec = assistant_lifecycle._resolve_team_assistant(team_id, assistant_id)
        except assistant_registry.AssistantSpecError:
            continue
        if assistant_id in seen:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "duplicate installed Assistant identity")
        seen.add(assistant_id)
        specs.append(
            _hosted_integration_spec(
                _ActiveAssistant(
                    assistant_id,
                    assistant_spec.contract,
                    container,
                    assistant_spec.image,
                    assistant_spec.version,
                    assistant_spec.summary,
                )
            )
        )
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class ActionInvocationRequest:
    team_id: str
    token: str
    assistant_id: str
    contract: assistant_registry.AssistantContract
    container: object
    action: object
    payload: object
    inspect_memo: dict[str, object] | None = None
    validated_assistant: _ActiveAssistant | None = None
    evidence: action_execution.ActionInvocationEvidence | None = None


def _project_hosted_action_result(
    request: ActionInvocationRequest,
    raw_result: object,
    private: action_execution.ResolvedInvocationEvidence,
) -> object:
    action = str(request.action)
    action_spec = request.contract.actions[action]
    try:
        return action_execution.project_rpc_result(
            raw_result,
            private.integrations,
            lambda value: _validate_action_payload(request.contract, action, value, output=True),
            action_execution.RpcResultPolicy(
                human_requests=tuple(action_spec.human_requests),
                authorization_requested=any(
                    response.kind in action_human.AUTHORIZATION_KINDS for response in private.transcript.responses
                ),
                stored_inputs_by_id=private.stored_inputs,
                declared_stored_inputs=tuple(action_spec.stored_inputs),
                catalog=(
                    action_human.catalog_by_id(request.contract.machine_contract)
                    if action_spec.human_requests
                    else None
                ),
                file_withheld=private.file is not None
                and not action_files.authorized(action_spec.human_requests, private.transcript),
            ),
        )
    except action_failure.ActionFailedError as exc:
        audit.log(
            "assistant_action",
            request.team_id,
            result="error",
            assistant=request.assistant_id,
            action=action,
            reason="action-failed",
        )
        raise runtime_state.ApiError(HTTPStatus.BAD_GATEWAY, "Assistant Action failed") from exc
    except action_execution.StoredInputRejectedError as exc:
        audit.log(
            "assistant_action",
            request.team_id,
            result="error",
            assistant=request.assistant_id,
            action=action,
            stored_input=exc.stored_input,
            reason="stored-input-rejected",
        )
        try:
            runtime_state._assistant_stored_inputs.delete(request.team_id, request.assistant_id, exc.stored_input)
        except action_stored_input.StoredInputStoreError as store_error:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant Stored Input state is unavailable",
            ) from store_error
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            "Assistant rejected its stored input; retry the task to provide a new value",
        ) from None
    except action_execution.RpcSecretExposureError:
        audit.log(
            "assistant_action",
            request.team_id,
            result="error",
            assistant=request.assistant_id,
            action=action,
            reason="secret-exposure",
        )
        raise runtime_state.ApiError(HTTPStatus.BAD_GATEWAY, "Assistant Action exposed protected data") from None
    except action_execution.RpcInvalidResultError as exc:
        audit.log(
            "assistant_action",
            request.team_id,
            result="error",
            assistant=request.assistant_id,
            action=action,
            reason="invalid-output",
        )
        raise runtime_state.ApiError(HTTPStatus.BAD_GATEWAY, "Assistant Action returned an invalid result") from exc


def _action_files(
    team_id: str,
    action_spec: object,
    private: action_execution.ResolvedInvocationEvidence,
    safe_input: dict[str, object],
) -> dict[str, object]:
    """The invocation's files: the turn's selected file, with its bytes only behind the Action's authorization."""
    try:
        return action_files.deliver(
            action_spec,
            private.file,
            private.transcript,
            safe_input,
            lambda file_id: runtime_state._storage().get(team_id, file_id),
        )
    except (action_files.FileDeliveryError, team_storage.StorageError) as exc:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT, "the attached file is unavailable for this Action; attach it again"
        ) from exc


def _invoke_assistant_action(request: ActionInvocationRequest) -> dict[str, object]:
    team_id = request.team_id
    assistant_id = request.assistant_id
    contract = request.contract
    container = request.container
    action = request.action
    if http_payload.canonical_action_id(action) is None or action not in contract.actions:
        raise runtime_state.ApiError(
            action_execution.UNDECLARED_ACTION_STATUS,
            "Assistant requested an undeclared Action",
        )
    try:
        safe_input = _validate_action_payload(contract, action, request.payload, output=False)
    except ValueError as exc:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc
    validated = request.validated_assistant
    if validated is None:
        _current_id, current_contract, current_container = _installed_assistant(
            team_id,
            assistant_id,
            request.inspect_memo,
        )
    else:
        _current_id = validated.assistant_id
        current_contract = validated.contract
        current_container = validated.container
    if _current_id != assistant_id or current_contract != contract or current_container.id != container.id:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "installed Assistant changed during the chat turn")
    active = _ActiveAssistant(assistant_id, contract, container)
    private = action_execution.resolve_invocation_evidence(
        request.evidence,
        lambda: _resolve_action_integrations(team_id, active, action),
        lambda: _resolve_action_stored_inputs(team_id, active, action),
    )
    files = _action_files(team_id, contract.actions[action], private, safe_input)
    audit.log(
        "assistant_action",
        team_id,
        result="ok",
        phase="started",
        assistant=assistant_id,
        action=action,
        operation_id=private.operation_id,
    )
    rpc_payload = {
        "input": safe_input,
        "stored_inputs": tuple(private.stored_inputs),
        "files": files,
        "operation_id": private.operation_id,
    }
    if private.transcript.responses:
        rpc_payload["responses"] = private.transcript.payloads()
    # Audit names a delivered file by its opaque id and size only, never its name or content (ADR-0093).
    sent = action_files.delivered(files)
    try:
        raw_result = _assistant_rpc(
            team_id, request.token, container, action, rpc_payload, _provider_broker(request, private)
        )
    except runtime_state.ApiError as exc:
        audit.log(
            "assistant_action",
            team_id,
            result="error",
            assistant=assistant_id,
            action=action,
            operation_id=private.operation_id,
            status=int(exc.status),
        )
        if sent is not None:
            # The exchange failed, so whether the workload received the bytes is unknown.
            audit.log(
                "assistant_action",
                team_id,
                result="error",
                phase="file-delivery-unconfirmed",
                assistant=assistant_id,
                action=action,
                file=sent.id,
                size=sent.size,
            )
        raise
    if sent is not None:
        audit.log(
            "assistant_action",
            team_id,
            result="ok",
            phase="file-delivered",
            assistant=assistant_id,
            action=action,
            file=sent.id,
            size=sent.size,
        )
    projected = _project_hosted_action_result(request, raw_result, private)
    audit.log(
        "assistant_action",
        team_id,
        result="ok",
        phase="completed",
        assistant=assistant_id,
        action=action,
        operation_id=private.operation_id,
    )
    return {"assistant": assistant_id, "action": action, "result": projected}


def _provider_broker(
    request: ActionInvocationRequest, private: action_execution.ResolvedInvocationEvidence
) -> action_provider.Broker:
    """Team's answer to this attempt's provider calls, through the Assistant's admitted egress policy (ADR-0106)."""
    identity = container_spec.team_assistant_container_name(request.team_id, request.assistant_id)
    action = str(request.action)

    def route() -> tuple[str, frozenset[str]]:
        try:
            admitted = assistant_lifecycle._egress_store().admitted(identity)
        except egress_policy.EgressPolicyError as exc:
            raise action_provider.CallRefusedError("unavailable", "egress-policy") from exc
        return ("", frozenset()) if admitted is None else (admitted[0], frozenset(admitted[1]))

    def record(fields: Mapping[str, object]) -> None:
        phase = fields["phase"]
        result = "denied" if phase == "refused" else "error" if "error" in fields else "ok"
        audit.log(
            "assistant_provider_call",
            request.team_id,
            result=result,
            assistant=request.assistant_id,
            action=action,
            operation_id=private.operation_id,
            **fields,
        )

    attempt = action_provider.Attempt(request.team_id, request.assistant_id, action, private.operation_id)
    return action_provider.Broker(
        action_provider.call_scope(attempt, request.contract, request.contract.actions[action], private, route, record)
    )


def _validate_assistant_action_input(bindings, assistant_id: str, action: str, action_input) -> object:
    """Normalize one hosted Action input without touching Docker or another external system."""
    active = bindings.get(assistant_id)
    if active is None:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Brain requested an unavailable Assistant")
    try:
        return _validate_action_payload(active.contract, action, action_input, output=False)
    except ValueError as exc:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc


def _validate_action_payload(
    contract: assistant_registry.AssistantContract,
    action_id: str,
    payload: object,
    *,
    output: bool,
) -> dict[str, object]:
    action = contract.actions.get(action_id)
    if action is None:
        raise ValueError("the Action has no declared contract")
    schema = action.output_schema if output else action.input_schema
    return assistant_manifest.validate_schema_payload(
        assistant_manifest.action_schema_validator(schema),
        payload,
    )


def _validate_chat_file_ids(file_ids: object) -> list[object]:
    if file_ids is None:
        return []
    if not isinstance(file_ids, list) or len(file_ids) > MAX_CHAT_FILES:
        raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, f"files must contain at most {MAX_CHAT_FILES} opaque ids")
    return file_ids


def _raise_chat_storage_error(exc: team_storage.StorageError) -> NoReturn:
    if isinstance(exc, team_storage.StorageNotFoundError):
        raise runtime_state.ApiError(HTTPStatus.NOT_FOUND, "selected file not found in this Team") from exc
    if isinstance(exc, team_storage.StorageInputError):
        raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, str(exc)) from exc
    raise runtime_state.ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Team storage failed its safety checks") from exc


@contextlib.contextmanager
def _chat_file_metadata_connection(team_id: str, file_ids: object):
    safe_ids = _validate_chat_file_ids(file_ids)
    if not safe_ids:
        yield None
        return
    try:
        with runtime_state._storage().metadata_connection(team_id, safe_ids) as reader:
            yield reader
    except team_storage.StorageError as exc:
        _raise_chat_storage_error(exc)


def _chat_file_metadata(
    team_id: str,
    file_ids: object,
    metadata_connection=None,
) -> list[dict[str, object]]:
    safe_ids = _validate_chat_file_ids(file_ids)
    try:
        return runtime_state._storage().metadata(team_id, safe_ids, metadata_connection)
    except team_storage.StorageError as exc:
        _raise_chat_storage_error(exc)


def _model_credential(
    owner: str,
    provider: str,
    credential_session: integration_secrets_client.IntegrationSecretSession | None = None,
) -> tuple[str, int]:
    if not owner:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "this Team has no account owner for model credentials")
    try:
        credential = integration_secrets_client.resolve(owner, provider, credential_session)
    except integration_secrets_client.IntegrationSecretError as exc:
        raise runtime_state.ApiError(HTTPStatus.BAD_GATEWAY, "model credential service is unavailable") from exc
    if credential is None:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, f"configure the {provider!r} API key before chatting")
    auth_type, api_key, generation = credential
    if auth_type != "api_key":
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "the selected model provider requires an API key")
    return api_key, generation


def _require_model_credential_current(
    owner: str,
    provider: str,
    generation: int,
    credential_session: integration_secrets_client.IntegrationSecretSession | None = None,
) -> None:
    try:
        current = integration_secrets_client.generation_is_current(owner, provider, generation, credential_session)
    except integration_secrets_client.IntegrationSecretError as exc:
        raise runtime_state.ApiError(HTTPStatus.BAD_GATEWAY, "model credential could not be verified") from exc
    if not current:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "model credential changed or was revoked; retry")
