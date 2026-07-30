"""Local chat Power execution and context validation operations."""

from http import HTTPStatus
from typing import NoReturn

from assistant.spec import validate_power_payload
from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from inference import client as brain_runtime_client
from inference import config as inference_config
from integrations import flow as integration_flow
from integrations import store as integration_store
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.chat.types import required_active_assistant as _required_active_assistant
from local_support.errors import ApiProblemError as ApiProblem
from power import journal as power_journal


def _invoke_chat_power(
    self,
    team_id: str,
    token: str,
    power_request: brain_runtime_client.PowerRequest,
    frozen_container_id: str,
) -> object:
    assistant_id = power_request.assistant_id
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
                or team_id in self._active_power_containers
            ):
                raise chat_orchestrator.ChatStoppedError("chat turn stopped")
            self._active_power_containers[team_id] = (token, container)
    try:
        invocation = self.assistant_lifecycle.invoke(
            team_id,
            assistant_id,
            power_request.power,
            power_request.input,
        )
    except ApiProblem:
        if self._chat_cancelled(token):
            raise chat_orchestrator.ChatStoppedError("chat turn stopped") from None
        raise
    finally:
        with self._active_chat_guard:
            active = self._active_power_containers.get(team_id)
            if active is not None and active[0] == token:
                self._active_power_containers.pop(team_id, None)
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
    if isinstance(exc, power_journal.PowerJournalError):
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team Power execution state is unavailable",
            code="power-state-unavailable",
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


def _validate_chat_power(
    bindings: dict[str, _ActiveAssistant],
    assistant_id: str,
    power: str,
    payload: object,
) -> object:
    active = _required_active_assistant(bindings, assistant_id)
    power_spec = active.spec.powers.get(power)
    if power_spec is None:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "the Power has no declared input contract",
            code="invalid-power-input",
        )
    try:
        return validate_power_payload(power_spec, "input", payload)
    except ValueError as exc:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            str(exc),
            code="invalid-power-input",
        ) from exc


def _require_chat_private_inputs(
    self,
    team_id: str,
    bindings: dict[str, _ActiveAssistant],
    requests: tuple[brain_runtime_client.PowerRequest, ...],
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
