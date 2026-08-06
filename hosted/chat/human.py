"""Hosted Owner responses to Team-owned Power human challenges."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from http import HTTPStatus

from hosted import state as runtime_state
from hosted.assistant import runtime as hosted_assistants
from hosted.chat import segment as hosted_chat_segment
from hosted.team import resources as hosted_resources
from power import challenges as power_challenges
from power import human as power_human


def _expire_challenges() -> None:
    for challenge in runtime_state._human_challenges.drain_expired():
        if not isinstance(challenge.payload, hosted_assistants._PendingHostedChat):
            raise AssertionError("invalid expired hosted human continuation")
        hosted_chat_segment._purge_hosted_human_pending(challenge.payload)


def pending_chat_human(team_id: str) -> dict[str, object]:
    """Return public metadata for one current Hosted Team human challenge."""
    _expire_challenges()
    challenge = runtime_state._human_challenges.current(team_id)
    return (
        hosted_chat_segment._hosted_human_challenge_payload(challenge)
        if challenge is not None
        else {"team_id": team_id, "status": "none"}
    )


def _resume_body(body: object) -> tuple[object, str, object | None]:
    if not isinstance(body, dict) or body.get("decision") not in {"submit", "deny"}:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Power human response is invalid")
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
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Power human response is invalid")
    return body["challenge_id"], decision, body.get("value")


def _pending_challenge(team_id: str, challenge_id: object) -> power_challenges.PendingHumanChallenge:
    try:
        challenge = runtime_state._human_challenges.get(team_id, challenge_id)
    except power_challenges.HumanChallengeNotFoundError as exc:
        _expire_challenges()
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            "Power human request expired; retry the message",
        ) from exc
    if not isinstance(challenge.payload, hosted_assistants._PendingHostedChat):
        raise AssertionError("invalid hosted human continuation")
    return challenge


def _validate_pending_context(
    team_id: str,
    challenge: power_challenges.PendingHumanChallenge,
    container: object,
    owner: str,
) -> hosted_assistants._PendingHostedChat:
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
    if setup[-1] != pending.identity:
        runtime_state._human_challenges.cancel_team(team_id)
        hosted_chat_segment._purge_hosted_human_pending(pending)
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Team capabilities changed; retry")
    return pending


def _admit_response(
    team_id: str,
    challenge: power_challenges.PendingHumanChallenge,
    pending: hosted_assistants._PendingHostedChat,
    decision: str,
    value: object | None,
    assurance: dict[str, str] | None,
) -> tuple[power_human.PowerTranscript, ...] | None:
    if decision == "deny":
        if assurance is not None:
            raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Power human response is invalid")
        runtime_state._human_challenges.claim(team_id, challenge.id)
        return None
    request = challenge.requirement.request
    response = value
    if request.kind in power_human.AUTH_KINDS:
        expected = {"kind": request.kind, "challenge_id": challenge.id}
        if assurance != expected:
            return None
        response = True
    elif assurance is not None:
        return None
    try:
        transcripts = power_human.append_response(
            pending.transcripts,
            challenge.requirement.interrupt_id,
            request,
            response,
        )
    except power_human.HumanRequestError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Power human response does not match its request",
        ) from exc
    runtime_state._human_challenges.claim(team_id, challenge.id)
    return transcripts


def resume_chat_human(
    team_id: str,
    body: object,
    assurance: dict[str, str] | None,
    lease: hosted_resources._AuthorizationLease,
    exclusive_turn: Callable[[str, hosted_resources._AuthorizationLease], AbstractContextManager],
) -> dict[str, object]:
    """Consume one exact Owner decision and deterministically replay its Power."""
    challenge_id, decision, value = _resume_body(body)
    with exclusive_turn(team_id, lease) as (token, container):
        challenge = _pending_challenge(team_id, challenge_id)
        pending = _validate_pending_context(team_id, challenge, container, lease.owner)
        transcripts = _admit_response(team_id, challenge, pending, decision, value, assurance)
        if transcripts is None:
            reason = "denied" if decision == "deny" else "authentication-failed"
            return hosted_chat_segment._terminal_hosted_human_failure(team_id, token, pending, reason)
        segment = hosted_chat_segment._run_hosted_chat_segment(
            hosted_chat_segment.HostedChatSegmentRequest(
                team_id=team_id,
                file_ids=list(pending.file_ids),
                assistant_ids=pending.assistant_ids,
                token=token,
                container=container,
                owner=lease.owner,
                continuation=pending.continuation,
                expected_identity=pending.identity,
                transcripts=transcripts,
            )
        )
        return hosted_chat_segment._hosted_segment_response(
            team_id,
            token,
            segment,
            pending.assistant_ids,
            pending.file_ids,
            pending.owner,
            transcripts,
        )


def cancel_pending(team_id: str) -> bool:
    """Cancel and purge one current Hosted human continuation."""
    _expire_challenges()
    challenge = runtime_state._human_challenges.current(team_id)
    if challenge is None:
        return False
    if not isinstance(challenge.payload, hosted_assistants._PendingHostedChat):
        raise AssertionError("invalid hosted human continuation")
    cancelled = runtime_state._human_challenges.cancel_team(team_id)
    hosted_chat_segment._purge_hosted_human_pending(challenge.payload)
    return cancelled
