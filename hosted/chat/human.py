"""Hosted Owner responses to Team-owned Action human challenges."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from http import HTTPStatus

from action import challenges as action_challenges
from action import execution as action_execution
from action import human as action_human
from action import stored_input as action_stored_input
from hosted import audit
from hosted import state as runtime_state
from hosted.assistant import runtime as hosted_assistants
from hosted.chat import segment as hosted_chat_segment
from hosted.team import resources as hosted_resources


def _expire_challenges(team_id: str) -> None:
    """Purge what one Team's expired continuations left in the Action journal; another Team's are never touched."""
    for challenge in runtime_state._human_challenges.drain_expired(team_id):
        if not isinstance(challenge.payload, hosted_assistants._PendingHostedChat):
            raise AssertionError("invalid expired hosted human continuation")
        hosted_chat_segment._purge_hosted_human_pending(challenge.payload)


def pending_chat_human(team_id: str) -> dict[str, object]:
    """Return public metadata for one current Hosted Team human challenge."""
    _expire_challenges(team_id)
    challenge = runtime_state._human_challenges.current(team_id)
    return (
        hosted_chat_segment._hosted_human_challenge_payload(challenge)
        if challenge is not None
        else {"team_id": team_id, "status": "none"}
    )


def _resume_body(body: object) -> tuple[object, str, object | None]:
    if not isinstance(body, dict) or body.get("decision") not in {"submit", "deny"}:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Action human response is invalid")
    decision = body["decision"]
    expected = (
        {"challenge_id", "decision", "value"}
        if decision == "submit"
        else {
            "challenge_id",
            "decision",
        }
    )
    if set(body) != expected:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Action human response is invalid")
    return body["challenge_id"], decision, body.get("value")


def _pending_challenge(team_id: str, challenge_id: object) -> action_challenges.PendingHumanChallenge:
    try:
        challenge = runtime_state._human_challenges.get(team_id, challenge_id)
    except action_challenges.HumanChallengeNotFoundError as exc:
        _expire_challenges(team_id)
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            "Action human request expired; retry the message",
        ) from exc
    if not isinstance(challenge.payload, hosted_assistants._PendingHostedChat):
        raise AssertionError("invalid hosted human continuation")
    return challenge


def _validate_pending_context(
    team_id: str,
    challenge: action_challenges.PendingHumanChallenge,
    container: object,
    owner: str,
) -> tuple[hosted_assistants._PendingHostedChat, object]:
    pending = challenge.payload
    if not isinstance(pending, hosted_assistants._PendingHostedChat) or pending.owner != owner:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Team capabilities changed; retry")
    setup = hosted_chat_segment._hosted_chat_setup(
        team_id,
        list(pending.file_ids),
        pending.assistant_ids,
        container,
        owner,
    )
    if setup[-1] != pending.identity or not _copy_binding_current(challenge.requirement, setup[1]):
        runtime_state._human_challenges.cancel_team(team_id)
        hosted_chat_segment._purge_hosted_human_pending(pending)
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Team capabilities changed; retry")
    return pending, setup[1]


def _copy_binding_current(requirement: action_challenges.HumanRequirement, assistants: object) -> bool:
    """Whether the requirement's Assistant still runs the catalog and pack its copy was rendered from (ADR-0091)."""
    active = next((item for item in assistants if item.assistant_id == requirement.assistant_id), None)
    return active is not None and action_challenges.copy_binding_current(
        requirement, active.contract.machine_contract, active.contract.pack_digest
    )


def _admit_response(
    team_id: str,
    challenge: action_challenges.PendingHumanChallenge,
    context: tuple[hosted_assistants._PendingHostedChat, object],
    decision: str,
    value: object | None,
    assurance: dict[str, str] | None,
) -> action_human.HumanResponseAdmission | None:
    pending, assistants = context
    if decision == "deny":
        if assurance is not None:
            raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Action human response is invalid")
        runtime_state._human_challenges.claim(team_id, challenge.id)
        return None
    request = challenge.requirement.request
    response = value
    if request.kind in action_human.AUTH_KINDS:
        expected = {"kind": request.kind, "challenge_id": challenge.id}
        if assurance != expected:
            return None
        response = True
    elif assurance is not None:
        return None
    try:
        admission = action_human.append_response(
            pending.transcripts,
            challenge.requirement.interrupt_id,
            request,
            response,
            pending.requests_used,
        )
    except action_human.HumanRequestError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Action human response does not match its request",
        ) from exc
    # The turn's chat slot excludes every Stored Input clear (ADR-0059), and a Stored Input answer is sealed while the
    # challenge is still held, so the challenge is consumed only once the value is kept.
    runtime_state._human_challenges.claim_after(
        team_id,
        challenge.id,
        lambda claimed: _seal_stored_input_answer(team_id, claimed, pending, assistants, admission),
    )
    return admission


def _seal_stored_input_answer(
    team_id: str,
    challenge: action_challenges.PendingHumanChallenge,
    pending: hosted_assistants._PendingHostedChat,
    assistants: object,
    admission: action_human.HumanResponseAdmission,
) -> None:
    submission = admission.stored_input
    if submission is None:
        return
    requirement = challenge.requirement
    active = next((item for item in assistants if item.assistant_id == requirement.assistant_id), None)
    try:
        if active is None:
            raise action_human.HumanRequestError("Stored Input answer has no running Assistant")
        action_execution.seal_admitted_stored_input(
            runtime_state._assistant_stored_inputs,
            team_id,
            requirement,
            pending.continuation.turn.actions,
            active.contract,
            submission,
        )
    except action_human.HumanRequestError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Action human response does not match its request",
        ) from exc
    except action_stored_input.StoredInputStoreError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant Stored Input could not be saved",
        ) from exc
    audit.log(
        "assistant_action",
        team_id,
        result="ok",
        phase="stored-input-sealed",
        assistant=requirement.assistant_id,
        action=requirement.action_id,
        stored_input=submission.stored_input,
    )


def resume_chat_human(
    team_id: str,
    body: object,
    assurance: dict[str, str] | None,
    lease: hosted_resources._AuthorizationLease,
    exclusive_turn: Callable[[str, hosted_resources._AuthorizationLease], AbstractContextManager],
) -> dict[str, object]:
    """Consume one exact Owner decision and deterministically replay its Action."""
    challenge_id, decision, value = _resume_body(body)
    with exclusive_turn(team_id, lease) as (token, container):
        challenge = _pending_challenge(team_id, challenge_id)
        context = _validate_pending_context(team_id, challenge, container, lease.owner)
        pending = context[0]
        admission = _admit_response(team_id, challenge, context, decision, value, assurance)
        if admission is None:
            reason = "denied" if decision == "deny" else "authentication-failed"
            return hosted_chat_segment._terminal_hosted_human_failure(team_id, token, pending, reason)
        return hosted_chat_segment.continue_paused(team_id, token, container, lease.owner, pending, admission)


def cancel_pending(team_id: str) -> bool:
    """Cancel and purge one current Hosted human continuation."""
    _expire_challenges(team_id)
    challenge = runtime_state._human_challenges.withdraw_team(team_id)
    if challenge is None:
        return False
    if not isinstance(challenge.payload, hosted_assistants._PendingHostedChat):
        raise AssertionError("invalid hosted human continuation")
    # Only the paused turn's own batch: a turn started since keeps its batch (ADR-0038).
    hosted_chat_segment._purge_hosted_human_pending(challenge.payload)
    return True
