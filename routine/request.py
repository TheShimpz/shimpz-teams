"""The authenticated chat request a Routine change binds, and its one-use receipt (ADR-0092 section 2).

Local Admin issues one identity per sent message and keeps it across a transport retry and a resend. Team binds it to
the Supervisor principal, the Team incarnation, and the canonical message, so a Routine change the same request carries
commits at most once while the identity is fresh, and a receipt outlives a Routine it created or changed.

A request may also cite up to three of the same person's own earlier sends that its message refers to, such as "do
this every 30 seconds" after "list my DNS zones" (ADR-0092 amendment, 2026-10-04). Team chose and froze them when the
send was first admitted; the Routine's words are those sends and the message, each parsed on its own.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass

from protocol.http.v1 import payload as http_payload

MAX_MESSAGE_CHARS = 16_000
MAX_EARLIER = 3
MAX_EARLIER_CHARS = 2_000
# The parts of a Routine's words join with one blank line, only to give every span one coordinate space.
SEPARATOR = "\n\n"
MAX_SOURCE_CHARS = MAX_MESSAGE_CHARS + MAX_EARLIER * (MAX_EARLIER_CHARS + len(SEPARATOR))
_LAYOUT = frozenset({"\n", "\r", "\t"})


def canonical_earlier(value: object) -> str | None:
    """One earlier send exactly as a Routine may cite it: NFC, trimmed, no control character but layout, 1-2,000."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_EARLIER_CHARS
        or unicodedata.normalize("NFC", value) != value
        or value.strip() != value
        or any(unicodedata.category(character)[0] == "C" and character not in _LAYOUT for character in value)
    ):
        return None
    return value


def commitment(message: str, earlier: tuple[str, ...]) -> str:
    """The commitment to a Routine's words: its earlier sends and message as one structure, never one joined text."""
    body = json.dumps({"earlier": list(earlier), "message": message}, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
    # The person's own earlier sends this message may refer to, oldest first, frozen when it was first admitted.
    earlier: tuple[str, ...] = ()

    @property
    def commitment(self) -> str:
        return commitment(self.message, self.earlier)

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
