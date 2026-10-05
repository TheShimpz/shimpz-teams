"""The authenticated chat request a Routine change binds, and its one-use receipt (ADR-0092 section 2).

Local Admin issues one identity per sent message and keeps it across a transport retry and a resend. Team binds it to
the Supervisor principal, the Team incarnation, and the canonical message, so a Routine change the same request carries
commits at most once while the identity is fresh, and a receipt outlives a Routine it created or changed.

A Routine's words are ordered parts of the person's own text, each parsed on its own (ADR-0092 amendments, 2026-10-04
and 2026-10-05). A ``said`` part states the Routine: the current message, or the answer a composed reply bound, and the
messages and answers of the person's Routine draft that the request continues; together they state one request, a later
part overriding an earlier one, and the standing request line stands in one of them. A ``cited`` part is one of the
person's own earlier sends, such as "list my DNS zones" before "do this every 30 seconds": it supplies work, targets, or
values only where a said part refers to it. Team froze every part when the request was first admitted.
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
# A Routine draft holds at most eight of the person's own parts, 32,000 characters in all (ADR-0092, 2026-10-05).
MAX_DRAFT_PARTS = 8
MAX_DRAFT_CHARS = 32_000
# The longest free-text answer a composed reply binds: the Admin question card's own answer field.
MAX_ANSWER_CHARS = 4_000
# A selected option's label, as the clarification protocol bounds it.
MAX_LABEL_CHARS = 80
CITED = "cited"
SAID = "said"
KINDS = frozenset({CITED, SAID})
# The draft, the earlier sends, the current message, and a selected label.
MAX_PARTS = MAX_DRAFT_PARTS + MAX_EARLIER + 2
# The parts of a Routine's words join with one blank line, only to give every span one coordinate space.
SEPARATOR = "\n\n"
MAX_SOURCE_CHARS = (
    MAX_DRAFT_CHARS
    + MAX_EARLIER * MAX_EARLIER_CHARS
    + MAX_MESSAGE_CHARS
    + MAX_LABEL_CHARS
    + (MAX_PARTS - 1) * len(SEPARATOR)
)
_LAYOUT = frozenset({"\n", "\r", "\t"})

type Part = tuple[str, str]


def canonical_text(value: object, maximum: int) -> str | None:
    """One text exactly as a Routine may keep it: NFC, trimmed, no control character but layout, 1 to ``maximum``."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= maximum
        or unicodedata.normalize("NFC", value) != value
        or value.strip() != value
        or any(unicodedata.category(character)[0] == "C" and character not in _LAYOUT for character in value)
    ):
        return None
    return value


def canonical_earlier(value: object) -> str | None:
    """One earlier send exactly as a Routine may cite it: canonical and at most 2,000 characters."""
    return canonical_text(value, MAX_EARLIER_CHARS)


def canonical_parts(value: object) -> tuple[Part, ...] | None:
    """A Routine's words as a creation source keeps them: 1 to 13 kinded texts, the last said, within the bound."""
    if not isinstance(value, list) or not 0 < len(value) <= MAX_PARTS:
        return None
    parts = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "text"} or not isinstance(item["kind"], str):
            return None
        if item["kind"] not in KINDS:
            return None
        text = item["text"]
        if not isinstance(text, str) or not 0 < len(text) <= MAX_MESSAGE_CHARS or "\0" in text:
            return None
        parts.append((item["kind"], text))
    joined = sum(len(text) for _kind, text in parts) + (len(parts) - 1) * len(SEPARATOR)
    if parts[-1][0] != SAID or joined > MAX_SOURCE_CHARS:
        return None
    return tuple(parts)


def commitment(parts: tuple[Part, ...]) -> str:
    """The commitment to a Routine's words: its ordered kinded parts as one structure, never one joined text."""
    body = json.dumps(
        {"parts": [{"kind": kind, "text": text} for kind, text in parts]}, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Draft:
    """The person's Routine draft as Team froze it for one request: its parts and the question it last asked."""

    generation: str
    incarnation: str
    parts: tuple[Part, ...]
    # The question's text and the SHA-256 of the exact message it was asked about; None once nothing is asked.
    question: tuple[str, str] | None = None

    @property
    def texts(self) -> frozenset[str]:
        return frozenset(text for _kind, text in self.parts)


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
    # The person's own earlier sends this message may refer to, oldest first, frozen when it was first admitted; a send
    # already in the draft is not repeated.
    earlier: tuple[str, ...] = ()
    # The person's Routine draft as it stood when the request was first admitted, or None.
    draft: Draft | None = None
    # The answer a composed reply to the draft's question gave, which is then the request's said text.
    answer: str | None = None

    @property
    def said(self) -> str:
        """The request's own said text: the bound answer, or the message itself."""
        return self.message if self.answer is None else self.answer

    def parts(self, continues: bool = False) -> tuple[Part, ...]:
        """The Routine's words: the draft when the request continues it, the earlier sends, then the said text."""
        draft = self.draft.parts if continues and self.draft is not None else ()
        return (*draft, *((CITED, text) for text in self.earlier), (SAID, self.said))

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
