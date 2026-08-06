"""Local Supervisor responses to Team-owned Power human challenges."""

from __future__ import annotations

from http import HTTPStatus

from chat import progress as chat_progress
from local.chat.segment import SegmentRequest
from local.chat.types import PendingLocalChat, ResponseRequest
from local.errors import ApiProblemError as ApiProblem
from local.validation import validate_team_id
from power import challenges as power_challenges
from power import human as power_human


def pending_chat_human(self, team_id: str) -> dict[str, object]:
    """Return public metadata for the Team's active human challenge, if any."""
    team_id = validate_team_id(team_id)
    self.assistant_lifecycle._network(team_id)
    _expire_human_challenges(self)
    challenge = self.human_challenges.current(team_id)
    return self._human_response(challenge) if challenge is not None else {"team_id": team_id, "status": "none"}


def _expire_human_challenges(self) -> None:
    for challenge in self.human_challenges.drain_expired():
        if not isinstance(challenge.payload, PendingLocalChat):
            raise AssertionError("invalid expired local human continuation")
        self._delete_chat_continuation(challenge.team_id, challenge.id)
        self._purge_human_pending(challenge.payload)


def _resume_body(body: object) -> tuple[object, str, object | None]:
    if not isinstance(body, dict) or body.get("decision") not in {"submit", "deny"}:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Power human response is invalid",
            code="invalid-body",
        )
    decision = body["decision"]
    expected = {"challenge_id", "decision", "value"} if decision == "submit" else {"challenge_id", "decision"}
    if set(body) != expected:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Power human response is invalid",
            code="invalid-body",
        )
    return body["challenge_id"], decision, body.get("value")


def _pending_challenge(self, team_id: str, challenge_id: object) -> power_challenges.PendingHumanChallenge:
    try:
        challenge = self.human_challenges.get(team_id, challenge_id)
    except power_challenges.HumanChallengeNotFoundError as exc:
        _expire_human_challenges(self)
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Power human request expired; retry the message",
            code="human-request-expired",
        ) from exc
    if not isinstance(challenge.payload, PendingLocalChat):
        raise AssertionError("invalid local human continuation")
    return challenge


def _validate_pending_context(self, team_id: str, provider: str, challenge: object) -> PendingLocalChat:
    if not isinstance(challenge, power_challenges.PendingHumanChallenge) or not isinstance(
        challenge.payload, PendingLocalChat
    ):
        raise AssertionError("invalid local human continuation")
    pending = challenge.payload
    if pending.provider != provider:
        self.human_challenges.cancel_team(team_id)
        self._delete_chat_continuation(team_id)
        self._purge_human_pending(pending)
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Team capabilities changed; retry",
            code="team-context-changed",
        )
    current = self._chat_setup(team_id, list(pending.file_ids), provider, pending.assistant_ids)
    if self._chat_identity(*current) != pending.identity:
        self.human_challenges.cancel_team(team_id)
        self._delete_chat_continuation(team_id)
        self._purge_human_pending(pending)
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Team capabilities changed; retry",
            code="team-context-changed",
        )
    return pending


def _admit_human_response(
    self,
    team_id: str,
    challenge: power_challenges.PendingHumanChallenge,
    pending: PendingLocalChat,
    decision: str,
    value: object | None,
) -> tuple[power_human.PowerTranscript, ...] | None:
    if decision == "deny":
        self.human_challenges.claim(team_id, challenge.id)
        self._delete_chat_continuation(team_id, challenge.id)
        return None
    try:
        transcripts = power_human.append_response(
            pending.transcripts,
            challenge.requirement.interrupt_id,
            challenge.requirement.request,
            value,
        )
    except power_human.HumanRequestError as exc:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Power human response does not match its request",
            code="invalid-human-response",
        ) from exc
    self.human_challenges.claim(team_id, challenge.id)
    self._delete_chat_continuation(team_id, challenge.id)
    return transcripts


def resume_chat_human(
    self,
    team_id: str,
    body: object,
    provider: str,
    api_key: str,
    progress: chat_progress.Reporter | None = None,
) -> dict[str, object]:
    """Consume one exact Supervisor decision and deterministically replay its Power."""
    team_id = validate_team_id(team_id)
    challenge_id, decision, value = _resume_body(body)
    with self._exclusive_chat_turn(team_id) as token:
        with self._lock(team_id):
            challenge = _pending_challenge(self, team_id, challenge_id)
            pending = _validate_pending_context(self, team_id, provider, challenge)
            transcripts = _admit_human_response(self, team_id, challenge, pending, decision, value)
            if transcripts is None:
                return self._terminal_human_failure(team_id, token, pending, "denied")
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
                progress=progress or chat_progress.Reporter(),
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
            )
        )
