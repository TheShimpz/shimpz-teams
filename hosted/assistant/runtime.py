"""Hosted Assistant contracts, RPC, private state, and Power execution."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import NoReturn

import docker
import docker.errors
from jsonschema import Draft202012Validator

from assistant import manifest as assistant_manifest
from assistant import spec as assistant_registry
from chat import contract as assistant_chat
from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from core.container import network as network_policy
from hosted import audit
from hosted import container as container_spec
from hosted import state as runtime_state
from hosted.assistant import lifecycle as assistant_lifecycle
from hosted.team import resources as hosted_resources
from inference import client as brain_runtime_client
from inference import integration_secrets as integration_secrets_client
from integrations import flow as integration_flow
from integrations import http as integration_http
from integrations import store as integration_store
from power import execution as power_execution
from power import journal as power_journal
from storage import files as team_storage

# ── Controller-owned Assistant chat ─────────────────────────────────────────────────────────────
CHAT_OUTPUT_CAP = 60000
MAX_INBOX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILE_BODY_BYTES = MAX_INBOX_FILE_BYTES
MAX_CHAT_FILES = 8
MAX_CHAT_ASSISTANTS = 16
CHAT_PAUSED_STATUSES = chat_turn_engine.CHAT_PAUSED_STATUSES


@dataclass(frozen=True, slots=True)
class _ActiveAssistant:
    assistant_id: str
    contract: assistant_registry.AssistantContract
    container: object
    image: str = ""


@dataclass(frozen=True, slots=True)
class _HostedAssistantSpec:
    """Small adapter for the closed integration contract."""

    assistant_id: str
    name: str
    powers: dict[str, object]
    integrations: dict[str, assistant_registry.IntegrationSpec]


@dataclass(frozen=True, slots=True)
class _HostedPowerSpec:
    integrations: tuple[str, ...]
    summary: str
    human_requests: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _HostedAssistantBinding:
    spec: _HostedAssistantSpec


@dataclass(frozen=True, slots=True)
class _PendingHostedChat:
    """Process-local state for one integration-gated hosted Team turn."""

    continuation: chat_orchestrator.ChatContinuation
    assistant_ids: tuple[str, ...]
    file_ids: tuple[str, ...]
    owner: str
    identity: tuple[object, ...]


def _hosted_integration_spec(active: _ActiveAssistant) -> _HostedAssistantSpec:
    name = active.assistant_id.replace("-", " ").title()
    return _HostedAssistantSpec(
        assistant_id=active.assistant_id,
        name=name,
        powers={
            power_id: _HostedPowerSpec(
                tuple(getattr(power, "integrations", ())),
                str(getattr(power, "summary", "")),
                tuple(getattr(power, "human_requests", ())),
            )
            for power_id, power in active.contract.powers.items()
        },
        integrations=getattr(active.contract, "integrations", {}),
    )


def _integration_bindings(
    bindings: dict[str, _ActiveAssistant],
) -> dict[str, _HostedAssistantBinding]:
    return {
        assistant_id: _HostedAssistantBinding(_hosted_integration_spec(active))
        for assistant_id, active in bindings.items()
    }


def _hosted_power_identity(active: _ActiveAssistant) -> tuple[object, object]:
    config = getattr(active.container, "attrs", {}).get("Config", {})
    image = config.get("Image") if isinstance(config, dict) else None
    if not isinstance(image, str) or not image:
        image = active.image
    return active.container.id, image


def _close_exec_stream(stream) -> None:
    power_execution.close_exec_stream(stream)


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
        if (team_id, container.id) in runtime_state._blocked_power_workloads:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant Power execution is blocked until this Assistant is reinstalled",
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
        active.append(_ActiveAssistant(current_id, contract, container, spec.image))
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


def _register_active_power(team_id: str, token: str, container) -> None:
    with runtime_state._active_chat_guard:
        if runtime_state._active_chat_tokens.get(team_id) != token or token in runtime_state._cancelled_chat_tokens:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "brain turn stopped")
        if team_id in runtime_state._active_power_container_ids:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Team already has an active Assistant Power")
        runtime_state._active_power_container_ids[team_id] = (token, container.id)


def _release_active_power(team_id: str, token: str, container_id: str) -> None:
    with runtime_state._active_chat_guard:
        if runtime_state._active_power_container_ids.get(team_id) == (token, container_id):
            runtime_state._active_power_container_ids.pop(team_id, None)


def _register_optional_power(team_id: str, token: str | None, container) -> None:
    if token is not None:
        _register_active_power(team_id, token, container)


def _release_optional_power(team_id: str, token: str | None, container_id: str) -> None:
    if token is not None:
        _release_active_power(team_id, token, container_id)


def _raise_if_rpc_cancelled(token: str | None, exc: BaseException | None = None) -> None:
    if token is not None and runtime_state._token_cancelled(token):
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "brain turn stopped") from exc


def _fail_stop_power(team_id: str, container) -> None:
    """Prove an ambiguous Assistant RPC can no longer execute before returning an error."""
    try:
        hosted_resources._fail_stop_team(container, timeout=3)
    except runtime_state.ApiError as exc:
        with runtime_state._active_chat_guard:
            runtime_state._blocked_power_workloads.add((team_id, container.id))
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant Power termination could not be proved; reinstall the Assistant",
        ) from exc


@dataclass(frozen=True, slots=True)
class AssistantRpcRequest:
    team_id: str
    container: object
    power_id: str
    payload: dict
    token: str | None


def _assistant_rpc_exchange(request: AssistantRpcRequest) -> object:
    team_id = request.team_id
    container = request.container
    token = request.token
    try:
        encoded = power_execution.encode_rpc_invocation(
            request.payload["input"],
            request.payload["integrations"],
        )
    except (KeyError, ValueError) as exc:
        raise runtime_state.ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Power input is too large") from exc
    _register_optional_power(team_id, token, container)

    def close_stream(stream: object) -> None:
        with contextlib.suppress(Exception):
            _close_exec_stream(stream)

    try:
        try:
            return power_execution.rpc_exchange(
                container.id,
                [power_execution.POWER_COMMAND, request.power_id],
                encoded,
                power_execution.RpcExchangeStrategy(
                    api=runtime_state._docker.api,
                    user=power_execution.ASSISTANT_RPC_USER,
                    workdir=container_spec.CONTAINER_TMP,
                    timeout=power_execution.RPC_TIMEOUT_SECONDS,
                    maximum=power_execution.MAX_RPC_RESPONSE_BYTES,
                    transport_errors=(docker.errors.DockerException,),
                    fail_stop=lambda: _fail_stop_power(team_id, container),
                    cancelled=lambda exc: _raise_if_rpc_cancelled(token, exc),
                    close_stream=close_stream,
                ),
            )
        except power_execution.RpcExchangeError as exc:
            message = power_execution.rpc_failure_message(exc.kind)[0]
            status = power_execution.rpc_failure_status(exc.kind)
            raise runtime_state.ApiError(status, message) from exc
    finally:
        _release_optional_power(team_id, token, container.id)


def _assistant_rpc(
    team_id: str,
    token: str,
    container,
    power_id: str,
    payload: dict,
) -> object:
    return _assistant_rpc_exchange(
        AssistantRpcRequest(
            team_id=team_id,
            container=container,
            power_id=power_id,
            payload=payload,
            token=token,
        )
    )


def _power_integration_generations(
    team_id: str,
    active: _ActiveAssistant,
    power_id: str,
) -> tuple[tuple[str, int], ...]:
    try:
        return power_execution.integration_generations(
            active.contract.powers,
            getattr(active.contract, "integrations", {}),
            power_id,
            lambda declarations: runtime_state._assistant_integrations.metadata(
                team_id,
                active.assistant_id,
                declarations,
            ),
        )
    except integration_store.OAuthIntegrationStoreError as exc:
        raise power_journal.PowerJournalConflictError("Power integration state is unavailable") from exc


def _refresh_oauth_integration(
    provider: str,
    scopes: tuple[str, ...],
    refresh_token: str,
    _broker_lease: str | None,
) -> object:
    try:
        return runtime_state._oauth_http.refresh(
            provider_id=provider,
            client_id=runtime_state._cloudflare_oauth_client_id,
            client_secret=runtime_state._cloudflare_oauth_client_secret,
            refresh_token=refresh_token,
            scopes=scopes,
        )
    except integration_http.OAuthHTTPError as exc:
        raise integration_store.OAuthIntegrationReauthorizationError(
            "OAuth integration requires reauthorization"
        ) from exc


def _resolve_power_integrations(
    team_id: str,
    active: _ActiveAssistant,
    power_id: str,
) -> dict[str, dict[str, str]]:
    try:
        return integration_flow.resolve_power_integrations(
            team_id,
            _hosted_integration_spec(active),
            power_id,
            runtime_state._assistant_integrations,
            _refresh_oauth_integration,
        )
    except integration_flow.IntegrationFlowError as exc:
        raise runtime_state.ApiError(
            power_execution.INTEGRATION_PRECONDITION_STATUS, "Assistant integration is unavailable"
        ) from exc


def _require_hosted_power_rpc_envelope(
    team_id: str,
    bindings: dict[str, _ActiveAssistant],
    request: brain_runtime_client.PowerRequest,
) -> Mapping[str, Mapping[str, object]]:
    active = bindings.get(request.assistant_id)
    if active is None:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Brain requested an unavailable Assistant")
    try:
        return power_execution.require_rpc_envelope(
            active,
            request,
            lambda binding, power_id: _resolve_power_integrations(team_id, binding, power_id),
        )
    except ValueError as exc:
        raise runtime_state.ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Assistant Power input is too large") from exc


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
        specs.append(_hosted_integration_spec(_ActiveAssistant(assistant_id, assistant_spec.contract, container)))
    return tuple(specs)


@dataclass(frozen=True, slots=True)
class PowerInvocationRequest:
    team_id: str
    token: str
    assistant_id: str
    contract: assistant_registry.AssistantContract
    container: object
    power: object
    payload: object
    inspect_memo: dict[str, object] | None = None
    validated_assistant: _ActiveAssistant | None = None
    integration_values: Mapping[str, Mapping[str, object]] | None = None


def _invoke_assistant_power(request: PowerInvocationRequest) -> dict[str, object]:
    team_id = request.team_id
    assistant_id = request.assistant_id
    contract = request.contract
    container = request.container
    power = request.power
    if (
        not isinstance(power, str)
        or assistant_chat.POWER_ID_RE.fullmatch(power) is None
        or power not in contract.powers
    ):
        raise runtime_state.ApiError(power_execution.UNDECLARED_POWER_STATUS, "Assistant requested an undeclared Power")
    try:
        safe_input = _validate_power_payload(contract, power, request.payload, output=False)
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
    integration_values = (
        _resolve_power_integrations(team_id, active, power)
        if request.integration_values is None
        else dict(request.integration_values)
    )
    audit.log(
        "assistant_power",
        team_id,
        result="ok",
        phase="started",
        assistant=assistant_id,
        power=power,
    )
    try:
        raw_result = _assistant_rpc(
            team_id,
            request.token,
            container,
            power,
            {
                "input": safe_input,
                "integrations": power_execution.integration_access_tokens(integration_values),
            },
        )
    except runtime_state.ApiError as exc:
        audit.log(
            "assistant_power",
            team_id,
            result="error",
            assistant=assistant_id,
            power=power,
            status=int(exc.status),
        )
        raise
    try:
        projected = power_execution.project_rpc_result(
            raw_result,
            integration_values,
            lambda value: _validate_power_payload(contract, power, value, output=True),
        )
    except power_execution.RpcSecretExposureError:
        audit.log(
            "assistant_power",
            team_id,
            result="error",
            assistant=assistant_id,
            power=power,
            reason="secret-exposure",
        )
        raise runtime_state.ApiError(HTTPStatus.BAD_GATEWAY, "Assistant Power exposed protected data") from None
    except power_execution.RpcInvalidResultError as exc:
        audit.log(
            "assistant_power",
            team_id,
            result="error",
            assistant=assistant_id,
            power=power,
            reason="invalid-output",
        )
        raise runtime_state.ApiError(HTTPStatus.BAD_GATEWAY, "Assistant Power returned an invalid result") from exc
    audit.log(
        "assistant_power",
        team_id,
        result="ok",
        phase="completed",
        assistant=assistant_id,
        power=power,
    )
    return {"assistant": assistant_id, "power": power, "result": projected}


def _validate_assistant_power_input(bindings, assistant_id: str, power: str, power_input) -> object:
    """Normalize one hosted Power input without touching Docker or another external system."""
    active = bindings.get(assistant_id)
    if active is None:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Brain requested an unavailable Assistant")
    try:
        return _validate_power_payload(active.contract, power, power_input, output=False)
    except ValueError as exc:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc


def _validate_power_payload(
    contract: assistant_registry.AssistantContract,
    power_id: str,
    payload: object,
    *,
    output: bool,
) -> dict[str, object]:
    power = contract.powers.get(power_id)
    if power is None:
        raise ValueError("the Power has no declared contract")
    schema = power.output_schema if output else power.input_schema
    return assistant_manifest.validate_schema_payload(
        Draft202012Validator(schema),
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
