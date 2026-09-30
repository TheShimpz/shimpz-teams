"""Routine proposals a chat turn produced, awaiting a Local Supervisor's confirmation (ADR-0086).

A proposal lives only in memory: it is one-use, bound to its Team and to the Assistants the turn selected, and expires
after 15 minutes, so a controller restart simply asks the user to request the Routine again.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from protocol.http.v1 import routine as http_routine

PROPOSAL_TTL_SECONDS = 900
MAX_PROPOSALS = 32


class ProposalError(LookupError):
    """The proposal is unknown, expired, used, or belongs to another Team."""


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    team_id: str
    change: dict[str, object]
    # The contract digest of each Assistant the proposing turn selected; confirmation refuses any other scope.
    contracts: tuple[tuple[str, str], ...]
    expires_at: float

    @property
    def assistant_ids(self) -> tuple[str, ...]:
        return tuple(assistant for assistant, _digest in self.contracts)

    def view(self, now: float) -> dict[str, object]:
        """What Admin shows on the confirmation card; the Team keeps the binding."""
        return {
            "proposal_id": self.proposal_id,
            **self.change,
            "assistant_ids": list(self.assistant_ids),
            "expires_in": max(0, int(self.expires_at - now)),
        }


class ProposalBook:
    """At most 32 live proposals across Teams; the oldest gives way, since each is only an offer to confirm."""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._proposals: dict[str, Proposal] = {}
        self._closed = False

    def _live(self) -> dict[str, Proposal]:
        now = self._now()
        self._proposals = {key: value for key, value in self._proposals.items() if value.expires_at > now}
        return self._proposals

    def create(self, team_id: str, change: object, contracts: dict[str, str]) -> Proposal:
        canonical = http_routine.canonical_routine_change(change)
        if canonical is None:
            raise ProposalError("invalid Routine change")
        with self._lock:
            if self._closed:
                raise ProposalError("Routine proposals are being reset")
            live = self._live()
            while len(live) >= MAX_PROPOSALS:
                live.pop(min(live, key=lambda key: live[key].expires_at))
            proposal = Proposal(
                secrets.token_hex(16),
                team_id,
                canonical,
                tuple(sorted(contracts.items())),
                self._now() + PROPOSAL_TTL_SECONDS,
            )
            live[proposal.proposal_id] = proposal
            return proposal

    def peek(self, team_id: str, proposal_id: object) -> Proposal:
        with self._lock:
            proposal = self._live().get(proposal_id) if isinstance(proposal_id, str) else None
        if proposal is None or proposal.team_id != team_id:
            raise ProposalError("Routine proposal is unavailable")
        return proposal

    def take(self, team_id: str, proposal_id: object) -> Proposal:
        """Consume a proposal once; a second confirmation finds nothing."""
        with self._lock:
            proposal = self._live().get(proposal_id) if isinstance(proposal_id, str) else None
            if proposal is None or proposal.team_id != team_id:
                raise ProposalError("Routine proposal is unavailable")
            del self._proposals[proposal_id]
            return proposal

    def drop(self, team_id: str, proposal_id: str) -> None:
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is not None and proposal.team_id == team_id:
                del self._proposals[proposal_id]

    def drop_team(self, team_id: str) -> None:
        with self._lock:
            self._proposals = {key: value for key, value in self._proposals.items() if value.team_id != team_id}

    @contextmanager
    def fenced(self) -> Iterator[None]:
        """Refuse new proposals while a Space reset runs, then drop every proposal it leaves."""
        with self._lock:
            self._closed = True
        try:
            yield
        finally:
            with self._lock:
                self._proposals = {}
                self._closed = False
