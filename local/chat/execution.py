"""Local chat Action execution and context validation operations."""

from collections.abc import Callable
from http import HTTPStatus
from typing import NoReturn

from action import execution as action_execution
from action import human as action_human
from action import journal as action_journal
from action import stored_input as action_stored_input
from assistant.spec import validate_action_payload
from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from inference import client as brain_runtime_client
from inference import config as inference_config
from integrations import flow as integration_flow
from integrations import store as integration_store
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.chat.types import required_active_assistant as _required_active_assistant
from local.errors import ApiProblemError as ApiProblem


def project_action_result(
    raw_result: object,
    action_spec: object,
    private: action_execution.ResolvedInvocationEvidence,
    validate: Callable[[object, str, object], object],
) -> object:
    return action_execution.project_rpc_result(
        raw_result,
        private.integrations,
        lambda value: validate(action_spec, "output", value),
        action_execution.RpcResultPolicy(
            human_requests=action_spec.human_requests,
            protected_values=private.transcript.protected_values(),
            authorization_requested=any(
                response.kind in action_human.AUTHORIZATION_KINDS
                for response in private.transcript.responses
            ),
            stored_inputs_by_id=private.stored_inputs,
            declared_stored_inputs=action_spec.stored_inputs,
            supplied_stored_inputs=frozenset(private.stored_inputs)
            | frozenset(private.transcript.submitted_stored_inputs()),
        ),
    )


def seal_stored_inputs(
    store: action_stored_input.StoredInputStore,
    team_id: str,
    assistant_id: str,
    spec: object,
    action_spec: object,
    private: action_execution.ResolvedInvocationEvidence,
) -> None:
    submitted = private.transcript.submitted_stored_inputs()
    if not submitted:
        return
    if private.origin is None:
        raise AssertionError("Stored Input submission lacks Action evidence")
    for stored_input_id, value in submitted.items():
        if stored_input_id not in action_spec.stored_inputs:
            raise KeyError(stored_input_id)
        declaration = spec.stored_inputs[stored_input_id]
        store.seal(
            team_id,
            assistant_id,
            stored_input_id,
            declaration.kind,
            value,
            private.origin,
        )


def clear_rejected_stored_input(
    store: action_stored_input.StoredInputStore,
    team_id: str,
    assistant_id: str,
    stored_input_id: str,
) -> NoReturn:
    try:
        store.delete(team_id, assistant_id, stored_input_id)
    except action_stored_input.StoredInputStoreError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant Stored Input state is unavailable",
            code="assistant-stored-input-state-unavailable",
        ) from exc
    raise ApiProblem(
        HTTPStatus.CONFLICT,
        "the Assistant rejected its stored input; retry the task to provide a new value",
        code="assistant-stored-input-rejected",
    )


def _invoke_chat_action(
    self,
    team_id: str,
    token: str,
    action_request: brain_runtime_client.ActionRequest,
    frozen_container_id: str,
    transcript: action_human.ActionTranscript,
    private_inputs: action_execution.RpcPrivateInputs,
) -> object:
    assistant_id = action_request.assistant_id
    with self._lock(team_id):
        spec = self.assistant_lifecycle._resolve(team_id, assistant_id)
        network = self.assistant_lifecycle._network(team_id)
        container = self.assistant_lifecycle._assistant_container(team_id, assistant_id)
        self.assistant_lifecycle._validate_container(container, team_id, spec, network.name)
        if container.id != frozen_container_id:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Team capabilities changed; retry",
                code="team-context-changed",
            )
        with self._active_chat_guard:
            if (
                self._active_chat_tokens.get(team_id) != token
                or token in self._cancelled_chat_tokens
                or team_id in self._active_action_containers
            ):
                raise chat_orchestrator.ChatStoppedError("chat turn stopped")
            self._active_action_containers[team_id] = (token, container)
    try:
        invocation = self.assistant_lifecycle.invoke(
            team_id,
            assistant_id,
            action_request.action,
            action_request.input,
            action_execution.ActionInvocationEvidence(
                private_inputs,
                transcript,
                action_execution.stored_input_origin(action_request),
            ),
        )
    except ApiProblem:
        if self._chat_cancelled(token):
            raise chat_orchestrator.ChatStoppedError("chat turn stopped") from None
        raise
    finally:
        with self._active_chat_guard:
            active = self._active_action_containers.get(team_id)
            if active is not None and active[0] == token:
                self._active_action_containers.pop(team_id, None)
    if self._chat_cancelled(token):
        raise chat_orchestrator.ChatStoppedError("chat turn stopped")
    return invocation["result"]


def _chat_identity(
    team_name: str,
    network_id: str,
    assistants: tuple[_ActiveAssistant, ...],
    files: list[dict[str, object]],
    config: inference_config.InferenceConfig,
) -> tuple[object, ...]:
    return (
        team_name,
        network_id,
        tuple((item.spec.assistant_id, item.spec.image, item.container_id) for item in assistants),
        files,
        config,
    )


def _raise_chat_problem(reason: str, exc: BaseException | None) -> NoReturn:
    if reason == "invalid-continuation" or reason == "invalid-suspension":
        raise ApiProblem(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"invalid chat {reason.removeprefix('invalid-')}",
            code="internal-error",
        )
    if reason == "context-changed":
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Team capabilities changed; retry",
            code="team-context-changed",
        )
    if isinstance(exc, action_journal.ActionJournalError):
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team Action execution state is unavailable",
            code="action-state-unavailable",
        ) from exc
    if isinstance(exc, chat_orchestrator.ChatStoppedError):
        raise ApiProblem(HTTPStatus.CONFLICT, "chat turn stopped", code="chat-stopped") from exc
    if isinstance(exc, chat_orchestrator.ChatOrchestrationError):
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY,
            "Brain could not complete the Team turn",
            code="brain-runtime-failed",
        ) from exc
    if isinstance(exc, brain_runtime_client.BrainRuntimeError):
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY,
            "Brain runtime is unavailable",
            code="brain-runtime-failed",
        ) from exc
    raise AssertionError(f"unknown local chat failure: {reason}")


def _validate_chat_action(
    bindings: dict[str, _ActiveAssistant],
    assistant_id: str,
    action: str,
    payload: object,
) -> object:
    active = _required_active_assistant(bindings, assistant_id)
    action_spec = active.spec.actions.get(action)
    if action_spec is None:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "the Action has no declared input contract",
            code="invalid-action-input",
        )
    try:
        return validate_action_payload(action_spec, "input", payload)
    except ValueError as exc:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            str(exc),
            code="invalid-action-input",
        ) from exc


def _require_chat_private_inputs(
    self,
    team_id: str,
    bindings: dict[str, _ActiveAssistant],
    requests: tuple[brain_runtime_client.ActionRequest, ...],
    requirements: chat_turn_engine.SegmentRequirements,
) -> bool:
    try:
        requirements.integrations = integration_flow.requirements_for_batch(
            team_id,
            bindings,
            requests,
            self.assistant_integrations,
        )
    except (
        integration_flow.IntegrationFlowError,
        integration_store.OAuthIntegrationStoreError,
    ) as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant integration contract is unavailable",
            code="assistant-integration-contract-invalid",
        ) from exc
    return bool(requirements.integrations)


def _validate_chat_context(
    self,
    team_id: str,
    file_ids: list[str],
    provider: str,
    assistant_ids: tuple[str, ...],
    identity: tuple[object, ...],
    metadata_connection=None,
) -> None:
    current = self._chat_setup(team_id, file_ids, provider, assistant_ids, metadata_connection)
    if self._chat_identity(*current) != identity:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Team capabilities changed; retry",
            code="team-context-changed",
        )
