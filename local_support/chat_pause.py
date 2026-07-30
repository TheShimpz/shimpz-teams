"""Local chat suspension and challenge response operations."""

from http import HTTPStatus

from assistant_human import assistant_integration_challenges, assistant_integration_flow
from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from local_support.chat_types import ActiveAssistant as _ActiveAssistant
from local_support.chat_types import PendingLocalChat as _PendingLocalChat
from local_support.errors import ApiProblemError as ApiProblem


def _commit_suspension(
    self,
    team_id: str,
    token: str,
    outcome: chat_orchestrator.ChatSuspension,
    payload: _PendingLocalChat,
    challenge_store: object,
    challenge_id: str,
) -> None:
    chat_turn_engine.commit_suspension(
        outcome.continuation,
        payload.continuation,
        lambda: self._commit_chat_terminal(team_id, token),
        lambda: challenge_store.cancel_team(team_id),
        lambda: ApiProblem(HTTPStatus.CONFLICT, "chat turn stopped", code="chat-stopped"),
        lambda: self._delete_chat_continuation(team_id, challenge_id),
    )


def _integration_response(
    self,
    challenge: assistant_integration_challenges.PendingIntegrationChallenge,
) -> dict[str, object]:
    bindings: dict[str, _ActiveAssistant] = {}
    for requirement in challenge.requirements:
        spec = self.assistant_lifecycle._resolve(challenge.team_id, requirement.assistant_id)
        bindings[spec.assistant_id] = _ActiveAssistant(spec, "")
    try:
        return assistant_integration_flow.challenge_payload(challenge, bindings)
    except assistant_integration_flow.IntegrationFlowError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant integration contract changed; retry the message",
            code="assistant-integration-contract-invalid",
        ) from exc


def _pause_integration(
    self,
    team_id: str,
    token: str,
    outcome: chat_orchestrator.ChatSuspension,
    requirements: tuple[assistant_integration_challenges.IntegrationRequirement, ...],
    payload: _PendingLocalChat,
) -> dict[str, object]:
    try:
        challenge = self.integration_challenges.create(team_id, requirements, payload)
    except assistant_integration_challenges.IntegrationChallengeError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant integration request is already pending",
            code="assistant-integration-challenge-conflict",
        ) from exc
    try:
        self._persist_chat_continuation("integrations", challenge, requirements, payload)
    except ApiProblem:
        self.integration_challenges.cancel_team(team_id)
        raise
    self._commit_suspension(team_id, token, outcome, payload, self.integration_challenges, challenge.id)
    return self._integration_response(challenge)
