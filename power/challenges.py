"""Bounded Team-owned challenges for admitted Power human requests."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from integrations import challenge_store
from power import human

DEFAULT_TTL_SECONDS = 300


class HumanChallengeError(RuntimeError):
    """A Power human challenge is invalid, unavailable, or conflicts."""


class HumanChallengeNotFoundError(HumanChallengeError):
    """The opaque challenge expired, was consumed, or belongs to another Team."""


@dataclass(frozen=True, slots=True)
class HumanRequirement:
    """Public context for one exact request without Power input or private values."""

    assistant_id: str
    assistant_name: str
    power_id: str
    power_summary: str
    interrupt_id: str
    request: human.HumanRequest


@dataclass(frozen=True, slots=True)
class PendingHumanChallenge:
    id: str
    team_id: str
    expires_at: float
    requirement: HumanRequirement
    payload: Any


def _requirement(value: object) -> bool:
    return isinstance(value, HumanRequirement) and isinstance(value.request, human.HumanRequest)


_CONTRACT = challenge_store.ChallengeContract(
    PendingHumanChallenge,
    _requirement,
    HumanChallengeError,
    HumanChallengeNotFoundError,
    "Power human request",
)


class HumanChallengeStore(challenge_store.ChallengeStore[PendingHumanChallenge]):
    """Keep one short-lived, one-use human decision per Team."""

    def __init__(
        self,
        *,
        capacity: int = challenge_store.MAX_PENDING_CHALLENGES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        super().__init__(
            _CONTRACT,
            capacity=capacity,
            ttl_seconds=ttl_seconds,
            clock=time.monotonic,
        )


def challenge_payload(challenge: PendingHumanChallenge) -> dict[str, object]:
    """Project one public modal descriptor without Power input or response material."""
    if not isinstance(challenge, PendingHumanChallenge) or not _requirement(challenge.requirement):
        raise HumanChallengeError("Power human challenge is invalid")
    remaining = math.ceil(challenge.expires_at - time.monotonic())
    if not 1 <= remaining <= DEFAULT_TTL_SECONDS:
        raise HumanChallengeError("Power human challenge is expired")
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
        },
        "power": {
            "id": requirement.power_id,
            "summary": requirement.power_summary,
        },
        "request": requirement.request.payload(),
    }
