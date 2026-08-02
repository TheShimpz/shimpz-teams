"""Bounded, process-local continuations for just-in-time OAuth integrations."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from integrations import challenge_store

MAX_PENDING_CHALLENGES = challenge_store.MAX_PENDING_CHALLENGES
DEFAULT_TTL_SECONDS = 600


class IntegrationChallengeError(RuntimeError):
    """A pending integration continuation is unavailable or conflicts."""


class IntegrationChallengeNotFoundError(IntegrationChallengeError):
    """The opaque challenge expired, was consumed, or belongs to another Team."""


@dataclass(frozen=True, slots=True)
class IntegrationRequirement:
    assistant_id: str
    assistant_name: str
    power_ids: tuple[str, ...]
    integrations: tuple[tuple[str, str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class PendingIntegrationChallenge:
    id: str
    team_id: str
    expires_at: float
    requirements: tuple[IntegrationRequirement, ...]
    payload: Any


_CONTRACT = challenge_store.ChallengeContract(
    PendingIntegrationChallenge,
    bool,
    IntegrationChallengeError,
    IntegrationChallengeNotFoundError,
    "integration",
)


class IntegrationChallengeStore(challenge_store.ChallengeStore[PendingIntegrationChallenge]):
    """Keep one short-lived, one-use paused integration turn per Team."""

    def __init__(
        self,
        *,
        capacity: int = MAX_PENDING_CHALLENGES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        super().__init__(
            _CONTRACT,
            capacity=capacity,
            ttl_seconds=ttl_seconds,
            clock=lambda: time.monotonic(),
        )
