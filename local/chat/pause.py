"""Local chat suspension and challenge response operations."""

from http import HTTPStatus
from typing import NoReturn

from action import challenges as action_challenges
from action import human as action_human
from action import journal as action_journal
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from chat import turn as chat_turn_engine
from integrations import challenges as integration_challenges
from integrations import flow as integration_flow
from local.chat.segment import SegmentRequest
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.chat.types import PendingLocalChat as _PendingLocalChat
from local.chat.types import ResponseRequest
from local.errors import ApiProblemError as ApiProblem
from local.errors import action_state_unavailable, chat_stopped, human_request_invalid, team_context_changed


def _commit_suspension(
    self,
    team_id: str,
    token: str,
    outcome: chat_orchestrator.ChatSuspension | chat_orchestrator.ChatHumanSuspension,
    payload: _PendingLocalChat,
    challenge_store: object,
    challenge_id: str,
) -> None:

    def rollback() -> None:
        self._delete_chat_continuation(team_id, challenge_id)
        if challenge_store is self.human_challenges:
            # A Stop that came first found no challenge to end this turn with, so its own batch goes here (ADR-0038).
            self._purge_human_pending(payload)

    chat_turn_engine.commit_suspension(
        outcome.continuation,
        payload.continuation,
        lambda: self._commit_chat_terminal(team_id, token),
        lambda: challenge_store.cancel_team(team_id),
        chat_stopped,
        rollback,
    )


def _human_response(
    self,
    challenge: action_challenges.PendingHumanChallenge,
) -> dict[str, object]:
    try:
        return action_challenges.challenge_payload(challenge)
    except action_challenges.HumanChallengeError as exc:
        raise human_request_invalid() from exc


def _purge_human_pending(self, pending: _PendingLocalChat) -> None:
    """Remove exactly the Action batch the paused turn holds; a newer turn's batch in its generation stays."""
    generation = pending.identity[1] if len(pending.identity) == 5 else None
    if not isinstance(generation, str) or not isinstance(pending.paused_batch, str):
        raise team_context_changed()
    try:
        self.action_state.purge_batch(generation, pending.paused_batch)
    except action_journal.ActionJournalError as exc:
        raise action_state_unavailable() from exc


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
        raise chat_stopped()
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
    requirements: tuple[action_challenges.HumanRequirement, ...],
    payload: _PendingLocalChat,
) -> dict[str, object]:
    if len(requirements) != 1 or requirements[0].request != outcome.request:
        return self._terminal_human_failure(team_id, token, payload, "request-invalid")
    if outcome.request.kind in action_human.AUTH_KINDS - {"auth:password"}:
        return self._terminal_human_failure(team_id, token, payload, "authentication-unavailable")
    # Publication, persistence, and the commit or its rollback hold the Team lock, so a relocalization never reissues a
    # challenge whose commit may still fail.
    with self._lock(team_id):
        try:
            challenge = self.human_challenges.create(team_id, requirements[0], payload)
        except action_challenges.HumanChallengeError as exc:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Action human request is already pending",
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
    # Publication, persistence, and the commit or its rollback hold the Team lock, so an OAuth start never creates PKCE
    # state from a challenge whose commit may still fail.
    with self._lock(team_id):
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


def _end_drifted_turn(self, team_id: str, challenge: object) -> NoReturn:
    """End exactly the paused turn whose context drifted: its live challenge, its continuation, and what it holds.

    The caller holds the Team lock, so no other turn can replace the live challenge between the check and the
    withdrawal; expiry may still remove it, and then this ends nothing. A human
    turn holds its Action batch. An Integration turn may hold OAuth state, which only a live challenge can start under
    this same lock; as on Stop, the Team's OAuth state is cancelled, before the continuation is deleted, so a failed
    deletion, which is reported, never leaves an authorization of the ended turn completable.
    """
    human = isinstance(challenge, action_challenges.PendingHumanChallenge)
    if not human and not isinstance(challenge, integration_challenges.PendingIntegrationChallenge):
        raise AssertionError("invalid local paused challenge")
    store = self.human_challenges if human else self.integration_challenges
    live = store.current(team_id)
    # A challenge that is no longer live has another owner: Stop or expiry ended it, a claimed answer is replaying its
    # turn, or an opening reissued it with the same batch. Nothing of it is touched here.
    if live is not None and live.id == challenge.id:
        store.withdraw_team(team_id)
        if not human:
            self.oauth_pkce.cancel_team(team_id)
        self._delete_withdrawn_continuation(team_id, challenge)
        if human:
            self._purge_human_pending(challenge.payload)
    raise team_context_changed()


def _paused_setup(self, team_id: str, provider: str, challenge: object) -> tuple[_PendingLocalChat, tuple[object, ...]]:
    """The paused turn's continuation and the Team's chat setup, ending the turn when its provider changed.

    A provider other than the one the turn paused with is proven drift, whether it is the request's or the Team's
    configured one; any other setup failure may be transient, so the paused turn stays answerable. The caller compares
    the setup's identity with the turn's.
    """
    pending = getattr(challenge, "payload", None)
    if not isinstance(pending, _PendingLocalChat):
        raise AssertionError("invalid local paused continuation")
    if pending.provider != provider:
        _end_drifted_turn(self, team_id, challenge)
    try:
        current = self._chat_setup(team_id, list(pending.file_ids), provider, pending.assistant_ids)
    except ApiProblem as exc:
        if exc.code != "inference-provider-mismatch":
            raise
        _end_drifted_turn(self, team_id, challenge)
    return pending, current


def _continue_paused(
    self,
    team_id: str,
    token: str,
    pending: _PendingLocalChat,
    effective: object,
    credentials: tuple[str, str],
    progress: chat_progress.Reporter | None,
) -> dict[str, object]:
    """Run the admitted remainder of a paused turn.

    `effective` carries the transcripts and request count the remainder starts from: the paused turn's own for an
    Integration resume, the admitted human response's for a human resume.
    """
    provider, api_key = credentials
    transcripts, requests_used = effective.transcripts, effective.requests_used
    segment = self._run_chat_segment(
        SegmentRequest(
            team_id=team_id,
            file_ids=list(pending.file_ids),
            assistant_ids=pending.assistant_ids,
            provider=provider,
            api_key=api_key,
            token=token,
            continuation=pending.continuation,
            expected_identity=pending.identity,
            transcripts=transcripts,
            requests_used=requests_used,
            locale=pending.locale,
            progress=progress or chat_progress.Reporter(),
            recording=pending.recording,
            model=pending.model,
        )
    )
    return self._segment_response(
        ResponseRequest(
            team_id,
            token,
            segment,
            pending.assistant_ids,
            pending.file_ids,
            provider,
            transcripts,
            requests_used,
            usage=pending.usage,
            recording=pending.recording,
        )
    )
