"""The Team-bound lineage of a chat clarification: which words of an answer are the model's (ADR-0092 section 2).

Answering a multiple-choice clarification sends one new message that composes the original request, the question,
and the answer (ADR-0081). Only the original request and the chosen answer are the user's own words: the model's
question and the interface labels around it never become a Routine grant. Team remembers, for a short time, the last
question it returned to each Team's Supervisor, and marks those spans of a composed answer as not the user's. Without
that memory, after a restart, every composed-looking question line is marked instead, so lineage fails closed.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from routine.request import Request

LINEAGE_SECONDS = 900
# One composed answer: a blank line, then "<question label>: <question>", then "<answer label>: <answer>".
_LABEL_CHARS = 40
_COMPOSED_RE = re.compile(r"\n\n([^\n:]{1,40}): [^\n]*\n([^\n:]{1,40}): ")

Spans = tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class Lineage:
    principal: str
    message: str
    excluded: Spans
    question: str
    expires_at: float


def _answer(lineage: Lineage, message: str) -> Spans | None:
    """The spans of a composed answer to exactly this question that are not the user's, or None for another message."""
    prefix = lineage.message + "\n\n"
    if not message.startswith(prefix):
        return None
    start = len(prefix)
    end = message.find("\n", start)
    if end < 0 or not message[start:end].endswith(": " + lineage.question):
        return None
    label = message.find(": ", end + 1)
    if label < 0 or not 0 < label - end - 1 <= _LABEL_CHARS or "\n" in message[end + 1 : label]:
        return None
    return (*lineage.excluded, (start, end), (end + 1, label + 2))


def unbound(message: str) -> Spans:
    """Every composed-looking question line and answer label, marked when no lineage is known."""
    spans: list[tuple[int, int]] = []
    for match in _COMPOSED_RE.finditer(message):
        question_end = message.index("\n", match.start() + 2)
        spans.extend(((match.start() + 2, question_end), (question_end + 1, match.end())))
    return tuple(spans)


class LineageBook:
    """At most one live lineage per Team: the last question its Supervisor was asked, for 15 minutes."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._lineages: dict[str, Lineage] = {}

    def record(self, team_id: str, request: Request, question: str) -> None:
        """Remember that this request's turn ended with a question."""
        lineage = Lineage(request.principal, request.message, request.excluded, question, self._now() + LINEAGE_SECONDS)
        with self._lock:
            self._lineages[team_id] = lineage

    def take(self, team_id: str, principal: str, message: str) -> Spans:
        """Consume the Team's lineage and return the spans of ``message`` that are not the principal's own words."""
        with self._lock:
            lineage = self._lineages.pop(team_id, None)
        if lineage is not None and lineage.principal == principal and lineage.expires_at > self._now():
            spans = _answer(lineage, message)
            if spans is not None:
                return spans
        return unbound(message)

    def drop(self, team_id: str) -> None:
        with self._lock:
            self._lineages.pop(team_id, None)

    def clear(self) -> None:
        with self._lock:
            self._lineages.clear()
