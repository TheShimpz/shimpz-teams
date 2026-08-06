"""Hosted human-request admission derived from captured Team HTTP input."""

from __future__ import annotations

import re
from dataclasses import dataclass
from http import HTTPStatus

from core.http import strict as strict_http
from hosted import state as runtime_state
from hosted.chat import human as hosted_chat_human
from power import challenges as power_challenges
from power import human as power_human

_ASSURANCE_HANDLE = re.compile(r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True, slots=True)
class AuthorityRequest:
    method: str
    route: strict_http.ControllerRouteMatch
    params: dict[str, str]
    query: dict[str, str]
    body: dict[str, object]
    assurance: dict[str, str] | None = None
    assurance_handle: object | None = None


def power_assurance(
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
    except KeyError, power_challenges.HumanChallengeNotFoundError:
        hosted_chat_human._expire_challenges()
        return None, None
    kind = challenge.requirement.request.kind
    if kind not in power_human.AUTH_KINDS:
        return None, None
    handle = body.get("value")
    if set(body) != {"challenge_id", "decision", "value"} or not isinstance(handle, str):
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Power authentication is invalid")
    if _ASSURANCE_HANDLE.fullmatch(handle) is None:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Power authentication is invalid")
    return {"kind": kind, "challenge_id": challenge.id}, handle
