"""Local Assistant Integration configuration operations."""

from http import HTTPStatus
from typing import NoReturn

from docker.errors import DockerException

from action import execution as action_execution
from action import journal as action_journal
from inference import client as brain_runtime_client
from integrations import challenges as integration_challenges
from integrations import flow as integration_flow
from integrations import service as integration_service
from integrations import store as integration_store
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.chat.types import required_active_assistant as _required_active_assistant
from local.errors import ApiProblemError as ApiProblem
from local.install.runtime import AssistantSpec
from local.validation import validate_team_id


def _action_integration_generations(
    self,
    team_id: str,
    active: _ActiveAssistant,
    action_id: str,
) -> tuple[tuple[str, int], ...]:
    try:
        return action_execution.integration_generations(
            active.spec.actions,
            active.spec.integrations,
            action_id,
            lambda declarations: self.assistant_integrations.metadata(
                team_id,
                active.spec.assistant_id,
                declarations,
            ),
        )
    except integration_store.OAuthIntegrationStoreError as exc:
        raise action_journal.ActionJournalConflictError("Action integration state is unavailable") from exc


def _refresh_oauth_integration(
    self,
    provider: str,
    scopes: tuple[str, ...],
    refresh_token: str,
    broker_lease: str | None,
) -> object:
    return self.oauth_service.refresh(
        provider,
        scopes,
        refresh_token,
        broker_lease,
    )


def _resolve_action_integrations(
    self,
    team_id: str,
    spec: AssistantSpec,
    action_id: str,
) -> dict[str, dict[str, str]]:
    try:
        return integration_flow.resolve_action_integrations(
            team_id,
            spec,
            action_id,
            self.assistant_integrations,
            self._refresh_oauth_integration,
        )
    except integration_flow.IntegrationFlowError as exc:
        raise ApiProblem(
            action_execution.INTEGRATION_PRECONDITION_STATUS,
            "Assistant integration is unavailable",
            code="assistant-integration-unavailable",
        ) from exc


def _require_action_rpc_envelope(
    self,
    team_id: str,
    bindings: dict[str, _ActiveAssistant],
    request: brain_runtime_client.ActionRequest,
) -> object:
    active = _required_active_assistant(bindings, request.assistant_id)
    try:
        return action_execution.require_rpc_envelope(
            active,
            request,
            lambda binding, action_id: self._resolve_action_integrations(team_id, binding.spec, action_id),
        )
    except ValueError as exc:
        raise ApiProblem(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "Assistant Action input is too large",
            code="assistant-action-input-too-large",
        ) from exc


def _raise_integration_problem(exc: integration_store.OAuthIntegrationStoreError) -> NoReturn:
    raise ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "Assistant integration state is unavailable",
        code="assistant-integration-state-unavailable",
    ) from exc


def list_assistant_integrations(self, team_id: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    with self._lock(team_id):
        specs = [
            self.assistant_lifecycle._resolve(team_id, assistant_id)
            for assistant_id in self.assistant_lifecycle._assistant_ids(team_id)
        ]
        try:
            payload = integration_flow.inventory_payload(
                team_id,
                specs,
                self.assistant_integrations,
            )
        except integration_store.OAuthIntegrationStoreError as exc:
            self._raise_integration_problem(exc)
        except integration_flow.IntegrationFlowError as exc:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Assistant integration contract is unavailable",
                code="assistant-integration-contract-invalid",
            ) from exc
    return {"team_id": team_id, **payload}


def start_assistant_integration_authorization(
    self,
    team_id: object,
    challenge_id: object,
    assistant_id: object,
    integration_id: object,
    session_binding: object,
    callback_mode: object,
) -> dict[str, object]:
    try:
        challenge = self.integration_challenges.get(team_id, challenge_id)
        authorization_url = self.oauth_service.authorization_url(
            challenge,
            session_binding,
            assistant_id=assistant_id,
            integration_id=integration_id,
            callback_mode=callback_mode,
        )
    except integration_challenges.IntegrationChallengeError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant integration request expired; retry the message",
            code="assistant-integration-challenge-expired",
        ) from exc
    except integration_service.OAuthIntegrationUnavailableError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant integration is not pending",
            code="assistant-integration-not-pending",
        ) from exc
    except integration_service.OAuthIntegrationServiceError as exc:
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY,
            "Assistant integration authorization is unavailable",
            code="assistant-integration-oauth-unavailable",
        ) from exc
    return {"authorization_url": authorization_url}


def _current_integration_declaration(
    self,
    team_id: str,
    assistant_id: str,
    integration_id: str,
) -> object:
    with self._lock(team_id):
        spec = self.assistant_lifecycle._resolve(team_id, assistant_id)
        declaration = spec.integrations.get(integration_id)
        if (
            assistant_id not in self.assistant_lifecycle._assistant_ids(team_id, running_only=True)
            or declaration is None
        ):
            raise integration_service.OAuthIntegrationDeclarationError("OAuth integration declaration is unavailable")
        try:
            container = self.assistant_lifecycle._assistant_container(team_id, assistant_id)
            container.reload()
        except (ApiProblem, DockerException) as exc:
            raise integration_service.OAuthIntegrationDeclarationError(
                "OAuth integration declaration is unavailable"
            ) from exc
        attrs = container.attrs if isinstance(container.attrs, dict) else {}
        config = attrs.get("Config")
        if not isinstance(config, dict) or not self.assistant_lifecycle._has_current_assistant_artifact(config, spec):
            raise integration_service.OAuthIntegrationDeclarationError("OAuth integration declaration is unavailable")
        return declaration


def complete_cloudflare_oauth_callback(
    self,
    *,
    state: object,
    claim: object,
    session_binding: object,
) -> dict[str, object]:
    try:
        completed = self.oauth_service.complete(
            state,
            claim,
            session_binding,
            self._current_integration_declaration,
        )
    except integration_service.OAuthIntegrationServiceError as exc:
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY,
            "Assistant integration authorization could not be completed",
            code="assistant-integration-oauth-unavailable",
        ) from exc
    return {
        "connected": True,
        "team_id": completed.team_id,
        "assistant_id": completed.assistant_id,
        "integration_id": completed.integration_id,
    }


def cancel_assistant_integration_authorization(
    self,
    session_binding: object,
) -> dict[str, object]:
    try:
        cancelled = self.oauth_service.cancel(session_binding)
    except integration_service.OAuthIntegrationServiceError as exc:
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY,
            "Assistant integration authorization could not be cancelled",
            code="assistant-integration-oauth-unavailable",
        ) from exc
    return {"cancelled": cancelled}


def disconnect_assistant_integration(
    self,
    team_id: object,
    assistant_id: object,
    integration_id: object,
) -> dict[str, object]:
    try:
        disconnected = self.oauth_service.disconnect(
            team_id,
            assistant_id,
            integration_id,
        )
    except integration_service.OAuthIntegrationServiceError as exc:
        raise ApiProblem(
            HTTPStatus.BAD_GATEWAY,
            "Assistant integration could not be disconnected",
            code="assistant-integration-oauth-unavailable",
        ) from exc
    return {"disconnected": disconnected}


def pending_chat_integrations(self, team_id: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    self.assistant_lifecycle._network(team_id)
    challenge = self.integration_challenges.current(team_id)
    return self._integration_response(challenge) if challenge is not None else {"team_id": team_id, "status": "none"}
