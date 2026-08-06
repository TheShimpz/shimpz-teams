"""Local chat suspension and challenge response operations."""

from http import HTTPStatus

from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from integrations import challenges as integration_challenges
from integrations import flow as integration_flow
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.chat.types import PendingLocalChat as _PendingLocalChat
from local.errors import ApiProblemError as ApiProblem
from power import challenges as power_challenges
from power import journal as power_journal


def _commit_suspension(
    self,
    team_id: str,
    token: str,
    outcome: chat_orchestrator.ChatSuspension | chat_orchestrator.ChatHumanSuspension,
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


def _human_response(
    self,
    challenge: power_challenges.PendingHumanChallenge,
) -> dict[str, object]:
    try:
        return power_challenges.challenge_payload(challenge)
    except power_challenges.HumanChallengeError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Power human request changed; retry the message",
            code="human-request-invalid",
        ) from exc


def _purge_human_generation(self, generation: object) -> None:
    if not isinstance(generation, str):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Team capabilities changed; retry",
            code="team-context-changed",
        )
    try:
        self.power_state.purge(generation)
    except power_journal.PowerJournalError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team Power execution state is unavailable",
            code="power-state-unavailable",
        ) from exc


def _purge_human_pending(self, pending: _PendingLocalChat) -> None:
    generation = pending.identity[1] if len(pending.identity) == 5 else None
    self._purge_human_generation(generation)


def _terminal_human_failure(
    self,
    team_id: str,
    token: str,
    pending: _PendingLocalChat,
    reason: str,
) -> dict[str, object]:
    self.human_challenges.cancel_team(team_id)
    self._delete_chat_continuation(team_id)
    self._purge_human_pending(pending)
    if not self._commit_chat_terminal(team_id, token):
        raise ApiProblem(HTTPStatus.CONFLICT, "chat turn stopped", code="chat-stopped")
    return {
        "team_id": team_id,
        "status": "human-denied",
        "reason": reason,
    }


def _pause_human(
    self,
    team_id: str,
    token: str,
    outcome: chat_orchestrator.ChatHumanSuspension,
    requirements: tuple[power_challenges.HumanRequirement, ...],
    payload: _PendingLocalChat,
) -> dict[str, object]:
    if len(requirements) != 1 or requirements[0].request != outcome.request:
        return self._terminal_human_failure(team_id, token, payload, "request-invalid")
    if any(response.secret for transcript in payload.transcripts for response in transcript.responses):
        return self._terminal_human_failure(team_id, token, payload, "secret-must-be-last")
    if outcome.request.kind in {"auth:second-factor", "auth:phishing-resistant"}:
        return self._terminal_human_failure(team_id, token, payload, "authentication-unavailable")
    try:
        challenge = self.human_challenges.create(team_id, requirements[0], payload)
    except power_challenges.HumanChallengeError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Power human request is already pending",
            code="human-request-conflict",
        ) from exc
    try:
        self._persist_chat_continuation("human", challenge, requirements, payload)
    except ApiProblem:
        self.human_challenges.cancel_team(team_id)
        self._purge_human_pending(payload)
        raise
    self._commit_suspension(team_id, token, outcome, payload, self.human_challenges, challenge.id)
    return self._human_response(challenge)


def _integration_response(
    self,
    challenge: integration_challenges.PendingIntegrationChallenge,
) -> dict[str, object]:
    bindings: dict[str, _ActiveAssistant] = {}
    for requirement in challenge.requirements:
        spec = self.assistant_lifecycle._resolve(challenge.team_id, requirement.assistant_id)
        bindings[spec.assistant_id] = _ActiveAssistant(spec, "")
    try:
        return integration_flow.challenge_payload(challenge, bindings)
    except integration_flow.IntegrationFlowError as exc:
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
    requirements: tuple[integration_challenges.IntegrationRequirement, ...],
    payload: _PendingLocalChat,
) -> dict[str, object]:
    try:
        challenge = self.integration_challenges.create(team_id, requirements, payload)
    except integration_challenges.IntegrationChallengeError as exc:
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
