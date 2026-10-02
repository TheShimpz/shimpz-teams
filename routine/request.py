"""The authenticated chat request a Routine change binds, and its one-use receipt (ADR-0092 section 2).

Local Admin issues one identity per sent message and keeps it across a transport retry and a resend. Team binds it to
the Supervisor principal, the Team incarnation, and the canonical message, so a Routine change the same request carries
commits at most once while the identity is fresh, and a receipt outlives a Routine it created or changed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from protocol.http.v1 import payload as http_payload


@dataclass(frozen=True, slots=True)
class Request:
    """One sent chat message of an authenticated Supervisor, as Team admitted it."""

    principal: str
    message: str
    issued_at: int
    nonce: str
    # The browser's IANA zone, the default zone of a Routine the message creates; None when the browser named none.
    timezone: str | None = None
    # The interface language of the turn, which pins the language pack a Routine's requests render in.
    locale: str | None = None

    def fresh(self, now: int) -> bool:
        """Whether the identity may still change a Routine: issued under 900 s ago and not far ahead of Team."""
        return http_payload.request_identity_fresh(self.issued_at, now)

    @property
    def expires_at(self) -> int:
        return self.issued_at + http_payload.REQUEST_IDENTITY_SECONDS

    def receipt(self, incarnation: str) -> str:
        """The receipt key: principal, Team incarnation, message commitment, and nonce, never the message itself."""
        commitment = hashlib.sha256(self.message.encode("utf-8")).hexdigest()
        bound = ["shimpz-routine-request-v1", self.principal, incarnation, commitment, self.nonce]
        return hashlib.sha256(json.dumps(bound, separators=(",", ":")).encode("ascii")).hexdigest()
