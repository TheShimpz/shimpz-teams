"""The person's own recent chat sends a Routine request may refer to (ADR-0092 amendment, 2026-10-04).

"Do this every 30 seconds" names its work only by pointing at an earlier send, such as "list my DNS zones". Team keeps,
per Team and person and only in memory, the last sends it admitted in admission order. A send that carried files,
composed a clarification answer, or cannot be cited exactly is a barrier: nothing before it is ever offered. When a send
is first admitted, its citable run is frozen: up to three consecutive sends right before it, oldest first, ending at the
newest barrier. A retry or resend of the same identity reuses exactly that run, never a newer one, and exactly the
person's Routine draft as it stood then (ADR-0092 amendment, 2026-10-05).

Team can place a send only when its identity is still fresh and was issued after the Team's record began: at start, at
the Team's creation, or since its deletion or a reset. Anything else may be a retry whose frozen run is lost, so it
cites nothing and becomes a barrier itself. The same identity with another message or person cannot change a Routine at
all; nor can a send that finds every frozen run still live at capacity or whose identity is issued too far ahead, or any
unfrozen identity issued by then, so a retry of it never acquires later history.
"""

from __future__ import annotations

import collections
import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from protocol.http.v1 import payload as http_payload
from routine import request as routine_request
from routine.request import Draft

# Sends kept per Team and person: enough to find the newest citable run of at most three behind any barrier.
MAX_SENDS = 8
# Live frozen runs per Team, as many as the Team's live Routine receipts; a full book refuses, never evicts one.
MAX_FROZEN = 256
FROZEN_SECONDS = http_payload.REQUEST_IDENTITY_SECONDS + http_payload.REQUEST_IDENTITY_SKEW_SECONDS


@dataclass(frozen=True, slots=True)
class _Send:
    # None is a barrier.
    text: str | None


@dataclass(frozen=True, slots=True)
class Admitted:
    """What a request may build its Routine words from, frozen when it was first admitted."""

    # The person's own citable sends right before it, oldest first.
    earlier: tuple[str, ...] = ()
    # The person's Routine draft as it then stood (ADR-0092 amendment, 2026-10-05), or None.
    draft: Draft | None = None


@dataclass(frozen=True, slots=True)
class _Frozen:
    principal: str
    commitment: str
    admitted: Admitted
    expires_at: int


def _run(sends: collections.deque[_Send]) -> tuple[str, ...]:
    """The newest consecutive citable sends, at most three, oldest first, stopping at the newest barrier."""
    run: list[str] = []
    for send in reversed(sends):
        if send.text is None or len(run) == routine_request.MAX_EARLIER:
            break
        run.append(send.text)
    return tuple(reversed(run))


class RecentBook:
    """Each Team's recent sends per person and the run each fresh identity froze, all in memory."""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._epoch = int(now())
        self._lock = threading.Lock()
        self._since: dict[str, int] = {}
        # The instant a Team's book was last full: an unfrozen identity issued by then can change no Routine.
        self._refused: dict[str, int] = {}
        self._sends: dict[tuple[str, str], collections.deque[_Send]] = {}
        self._frozen: dict[str, dict[str, _Frozen]] = {}

    def admit(
        self,
        team_id: str,
        principal: str,
        identity: dict[str, object],
        message: str,
        citable: bool,
        draft: Draft | None = None,
    ) -> Admitted | None:
        """Record one admitted send once per identity; what it may build a Routine from, or None when it changes none.

        ``citable`` says whether a later send may cite this one: plain text without files that answers no question.
        ``draft`` is the person's Routine draft as it stands now; only a first placement freezes it, so a retry keeps
        exactly what its identity first saw, and an identity Team cannot place gets none.
        """
        issued_at, nonce = identity["issued_at"], identity["nonce"]
        commitment = hashlib.sha256(message.encode("utf-8")).hexdigest()
        with self._lock:
            frozen = self._frozen.setdefault(team_id, {})
            now = int(self._now())
            for key in [key for key, item in frozen.items() if item.expires_at <= now]:
                del frozen[key]
            found = frozen.get(nonce)
            if found is not None:
                return found.admitted if (found.principal, found.commitment) == (principal, commitment) else None
            sends = self._sends.setdefault((team_id, principal), collections.deque(maxlen=MAX_SENDS))
            placed = self._since.get(team_id, self._epoch) < issued_at and http_payload.request_identity_fresh(
                issued_at, now
            )
            # An identity issued too far ahead would become fresh later: like a full book, it is refused for good.
            ahead = issued_at > now + http_payload.REQUEST_IDENTITY_SKEW_SECONDS
            refused = issued_at <= self._refused.get(team_id, -1) or ahead or (placed and len(frozen) >= MAX_FROZEN)
            if refused:
                self._refused[team_id] = max(now, issued_at, self._refused.get(team_id, -1))
            if refused or not placed:
                # Neither ever becomes citable history: a send a full book cannot freeze, or a lost retry.
                sends.append(_Send(None))
                return None if refused else Admitted()
            admitted = Admitted(_run(sends), draft)
            text = message if citable and routine_request.canonical_earlier(message) is not None else None
            sends.append(_Send(text))
            frozen[nonce] = _Frozen(principal, commitment, admitted, issued_at + FROZEN_SECONDS)
            return admitted

    def drop(self, team_id: str) -> None:
        """Forget a Team's record; only identities issued after now are placed again."""
        with self._lock:
            self._since[team_id] = int(self._now())
            self._refused.pop(team_id, None)
            self._frozen.pop(team_id, None)
            for key in [key for key in self._sends if key[0] == team_id]:
                del self._sends[key]

    def clear(self) -> None:
        with self._lock:
            self._epoch = int(self._now())
            self._since.clear()
            self._refused.clear()
            self._frozen.clear()
            self._sends.clear()
