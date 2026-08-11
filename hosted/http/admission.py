"""Hosted human-request admission derived from captured Team HTTP input."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus

from action import challenges as action_challenges
from action import human as action_human
from core.http import strict as strict_http
from hosted import state as runtime_state
from hosted.chat import human as hosted_chat_human
from protocol.http.v1 import payload as team_http_contract


@dataclass(frozen=True, slots=True)
class AuthorityRequest:
    method: str
    route: strict_http.ControllerRouteMatch
    params: dict[str, str]
    query: dict[str, str]
    body: dict[str, object]
    assurance: dict[str, str] | None = None
    assurance_handle: object | None = None


def action_assurance(
    operation: str,
    params: dict[str, str],
    body: object,
) -> tuple[dict[str, str] | None, object | None]:
    """Extract only a request-bound Account assurance handle from an auth response."""
    if operation != "chat-human-submit" or not isinstance(body, dict) or body.get("decision") != "submit":
        return None, None
    try:
        challenge = runtime_state._human_challenges.get(
            params["team_id"],
            body.get("challenge_id"),
        )
    except KeyError, action_challenges.HumanChallengeNotFoundError:
        hosted_chat_human._expire_challenges()
        return None, None
    kind = challenge.requirement.request.kind
    if kind not in action_human.AUTH_KINDS:
        return None, None
    handle = team_http_contract.canonical_assurance_handle(body.get("value"))
    if set(body) != {"challenge_id", "decision", "value"} or handle is None:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Action authentication is invalid")
    return {"kind": kind, "challenge_id": challenge.id}, handle
