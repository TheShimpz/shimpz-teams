"""Bounded Team-owned challenges for admitted Action human requests."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from action import human
from integrations import challenge_store
from protocol.http.v1 import payload as http_payload

DEFAULT_TTL_SECONDS = 300


class HumanChallengeError(RuntimeError):
    """An Action human challenge is invalid, unavailable, or conflicts."""


class HumanChallengeNotFoundError(HumanChallengeError):
    """The opaque challenge expired, was consumed, or belongs to another Team."""


@dataclass(frozen=True, slots=True)
class HumanRequirement:
    """Public context for one exact request without Action input or private values."""

    assistant_id: str
    assistant_name: str
    action_id: str
    action_summary: str
    interrupt_id: str
    request: human.HumanRequest
    assistant_version: str
    # The reviewed key page of the Stored Input the request names, copied only from that binding's declaration.
    help_url: str | None = None
    # The Brain's task-bound sentence for why this Action pauses, in the turn's interface language (ADR-0090).
    purpose: str | None = None


def declared_help_url(request: human.HumanRequest, stored_inputs: object) -> str | None:
    """The key page the reviewed binding declares for the one Stored Input a password request names, if any."""
    if request.kind != "input:password" or request.stored_input is None or not isinstance(stored_inputs, Mapping):
        return None
    declaration = stored_inputs.get(request.stored_input)
    return getattr(declaration, "help_url", None)


@dataclass(frozen=True, slots=True)
class PendingHumanChallenge:
    id: str
    team_id: str
    expires_at: float
    requirement: HumanRequirement
    payload: Any


def _requirement(value: object) -> bool:
    return (
        isinstance(value, HumanRequirement)
        and isinstance(value.request, human.HumanRequest)
        and isinstance(value.assistant_version, str)
        and 1 <= len(value.assistant_version) <= 40
        and (
            value.help_url is None
            or (
                value.request.kind == "input:password"
                and value.request.stored_input is not None
                and http_payload.canonical_help_url(value.help_url) == value.help_url
            )
        )
        and (value.purpose is None or http_payload.canonical_purpose(value.purpose) == value.purpose)
    )


_CONTRACT = challenge_store.ChallengeContract(
    PendingHumanChallenge,
    _requirement,
    HumanChallengeError,
    HumanChallengeNotFoundError,
    "Action human request",
)


class HumanChallengeStore(challenge_store.ChallengeStore[PendingHumanChallenge]):
    """Keep one short-lived, one-use human decision per Team."""

    def __init__(
        self,
        *,
        capacity: int = challenge_store.MAX_PENDING_CHALLENGES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            _CONTRACT,
            capacity=capacity,
            ttl_seconds=ttl_seconds,
            clock=clock,
        )


def challenge_payload(challenge: PendingHumanChallenge) -> dict[str, object]:
    """Project one public modal descriptor without Action input or response material."""
    if not isinstance(challenge, PendingHumanChallenge) or not _requirement(challenge.requirement):
        raise HumanChallengeError("Action human challenge is invalid")
    remaining = math.ceil(challenge.expires_at - time.monotonic())
    if not 1 <= remaining <= DEFAULT_TTL_SECONDS:
        raise HumanChallengeError("Action human challenge is expired")
    requirement = challenge.requirement
    return {
        "team_id": challenge.team_id,
        "status": "human-required",
        "turn_id": challenge.id,
        "challenge_id": challenge.id,
        "expires_in": remaining,
        "assistant": {
            "id": requirement.assistant_id,
            "name": requirement.assistant_name,
            "version": requirement.assistant_version,
        },
        "action": {
            "id": requirement.action_id,
            "summary": requirement.action_summary,
        },
        "request": requirement.request.payload(),
        **({} if requirement.purpose is None else {"purpose": requirement.purpose}),
        **({} if requirement.help_url is None else {"help_url": requirement.help_url}),
    }
