"""Local Supervisor responses to Team-owned Action human challenges."""

from __future__ import annotations

from http import HTTPStatus

from action import challenges as action_challenges
from action import human as action_human
from chat import progress as chat_progress
from local.chat.segment import SegmentRequest
from local.chat.types import PendingLocalChat, ResponseRequest
from local.errors import ApiProblemError as ApiProblem
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine


def pending_chat_human(self, team_id: str) -> dict[str, object]:
    """Return public metadata for the Team's active human challenge, if any."""
    team_id = validate_team_id(team_id)
    self.assistant_lifecycle._network(team_id)
    _expire_human_challenges(self)
    challenge = self.human_challenges.current(team_id)
    return self._human_response(challenge) if challenge is not None else {"team_id": team_id, "status": "none"}


def open_chat_human(self, team_id: str, body: object) -> dict[str, object]:
    """Open the Team's pending human challenge in the Admin interface language, or report that none is pending.

    The body is exactly ``{"locale": code}`` (ADR-0091). A challenge already in that language is returned unchanged;
    any other language replaces it with a fresh challenge (see ``relocalized``).
    """
    team_id = validate_team_id(team_id)
    opening = http_routine.canonical_challenge_open(body)
    if opening is None:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY, "opening a challenge requires only locale", code="invalid-body"
        )
    self.assistant_lifecycle._network(team_id)
    _expire_human_challenges(self)
    with self._lock(team_id):
        challenge = self.human_challenges.current(team_id)
        if challenge is None:
            return {"team_id": team_id, "status": "none"}
        return self._human_response(relocalized(self, challenge, opening["locale"]))


def relocalized(
    self, challenge: action_challenges.PendingHumanChallenge, locale: str
) -> action_challenges.PendingHumanChallenge:
    """The pending request rendered in one interface language; another language is always a fresh challenge.

    As with a Routine opening, the same canonical request and fingerprint are re-rendered from the same binding's
    pack under Team's lock, the earlier challenge stops answering, and the fresh one keeps its expiry and continuation.
    The turn keeps its own language and the purpose its origin locale, so a purpose is shown only in that locale.
    Reopening in the same language still validates the binding first, so a drifted binding ends the paused turn.
    """
    team_id = challenge.team_id
    with self._lock(team_id):
        # Even a challenge already in this language answers only while its binding is exactly as the turn left it.
        pending, assistants = _validate_pending_context(self, team_id, challenge.payload.provider, challenge)
        if challenge.requirement.copy.locale == locale:
            return challenge
        active = next(item for item in assistants if item.spec.assistant_id == challenge.requirement.assistant_id)
        try:
            requirement = action_challenges.relocalize(challenge.requirement, self._assistant_language(active), locale)
            fresh = self.human_challenges.reissue(team_id, challenge.id, requirement)
        except action_challenges.HumanChallengeNotFoundError as exc:
            raise ApiProblem(
                HTTPStatus.CONFLICT, "Action human request expired; retry the message", code="human-request-expired"
            ) from exc
        except action_challenges.HumanChallengeError as exc:
            raise ApiProblem(
                HTTPStatus.CONFLICT, "Action human request changed; retry the message", code="human-request-invalid"
            ) from exc
        try:
            self._persist_chat_continuation("human", fresh, (requirement,), pending)
        except ApiProblem:
            # The earlier challenge no longer answers, so a fresh one that cannot be kept ends the paused turn.
            self.human_challenges.cancel_team(team_id)
            self._delete_chat_continuation(team_id)
            self._purge_human_pending(pending)
            raise
    return fresh


def _expire_human_challenges(self, team_id: str | None = None) -> None:
    """Delete what expired continuations, every Team's or only one Team's, left behind."""
    for challenge in self.human_challenges.drain_expired(team_id):
        if not isinstance(challenge.payload, PendingLocalChat):
            raise AssertionError("invalid expired local human continuation")
        self._delete_chat_continuation(challenge.team_id, challenge.id)
        self._purge_human_pending(challenge.payload)


def _resume_body(body: object) -> tuple[object, str, object | None]:
    if not isinstance(body, dict) or body.get("decision") not in {"submit", "deny"}:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Action human response is invalid",
            code="invalid-body",
        )
    decision = body["decision"]
    expected = {"challenge_id", "decision", "value"} if decision == "submit" else {"challenge_id", "decision"}
    if set(body) != expected:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Action human response is invalid",
            code="invalid-body",
        )
    return body["challenge_id"], decision, body.get("value")


def _pending_challenge(self, team_id: str, challenge_id: object) -> action_challenges.PendingHumanChallenge:
    try:
        challenge = self.human_challenges.get(team_id, challenge_id)
    except action_challenges.HumanChallengeNotFoundError as exc:
        _expire_human_challenges(self)
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Action human request expired; retry the message",
            code="human-request-expired",
        ) from exc
    if not isinstance(challenge.payload, PendingLocalChat):
        raise AssertionError("invalid local human continuation")
    return challenge


def _validate_pending_context(
    self, team_id: str, provider: str, challenge: object
) -> tuple[PendingLocalChat, tuple[object, ...]]:
    """The challenge's continuation and the Team's running Assistants, which must be exactly as the turn left them."""
    if not isinstance(challenge, action_challenges.PendingHumanChallenge) or not isinstance(
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
    if self._chat_identity(*current) != pending.identity or not copy_binding_current(challenge.requirement, current[2]):
        self.human_challenges.cancel_team(team_id)
        self._delete_chat_continuation(team_id)
        self._purge_human_pending(pending)
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Team capabilities changed; retry",
            code="team-context-changed",
        )
    return pending, current[2]


def copy_binding_current(requirement: action_challenges.HumanRequirement, assistants: tuple[object, ...]) -> bool:
    """Whether the requirement's Assistant still runs the catalog and pack its copy was rendered from (ADR-0091)."""
    active = next((item for item in assistants if item.spec.assistant_id == requirement.assistant_id), None)
    return active is not None and action_challenges.copy_binding_current(
        requirement, active.spec.machine_contract, active.spec.pack_digest
    )


def _admit_human_response(
    self,
    team_id: str,
    challenge: action_challenges.PendingHumanChallenge,
    pending: PendingLocalChat,
    decision: str,
    value: object | None,
) -> action_human.HumanResponseAdmission | None:
    if decision == "deny":
        self.human_challenges.claim(team_id, challenge.id)
        self._delete_chat_continuation(team_id, challenge.id)
        return None
    try:
        admission = action_human.append_response(
            pending.transcripts,
            challenge.requirement.interrupt_id,
            challenge.requirement.request,
            value,
            pending.requests_used,
        )
    except action_human.HumanRequestError as exc:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Action human response does not match its request",
            code="invalid-human-response",
        ) from exc
    self.human_challenges.claim(team_id, challenge.id)
    self._delete_chat_continuation(team_id, challenge.id)
    return admission


def resume_chat_human(
    self,
    team_id: str,
    body: object,
    provider: str,
    api_key: str,
    progress: chat_progress.Reporter | None = None,
) -> dict[str, object]:
    """Consume one exact Supervisor decision and deterministically replay its Action."""
    team_id = validate_team_id(team_id)
    challenge_id, decision, value = _resume_body(body)
    with self._exclusive_chat_turn(team_id) as token:
        with self._lock(team_id):
            challenge = _pending_challenge(self, team_id, challenge_id)
            pending, _assistants = _validate_pending_context(self, team_id, provider, challenge)
            admission = _admit_human_response(self, team_id, challenge, pending, decision, value)
            if admission is None:
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
                transcripts=admission.transcripts,
                requests_used=admission.requests_used,
                locale=pending.locale,
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
                admission.transcripts,
                admission.requests_used,
                usage=pending.usage,
            )
        )
