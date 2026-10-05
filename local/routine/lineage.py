"""The Team-bound lineage of a Routine question: a pending candidate and the one field it asks about (ADR-0092 §2).

When the Routine planner is unsure of exactly one field, the turn ends with an ordinary multiple-choice clarification
and Team keeps, for a short time, a canonical pending candidate: the question's identity, its option labels, and one
complete admitted Routine per option that differs from the others only in that field. Answering sends one new message
that composes the original request, the question, and the chosen label (ADR-0081). Only an answer that composes exactly
this question for the same principal, with one of its own labels, binds; the Routine of that option is then committed
as it was admitted, so the selection changes only the bound field, and neither the model's question text nor an
unselected option ever becomes the user's grant. The candidate stays until its Routine commits, so a failed or stopped
answer may be retried, and expires after 15 minutes. A create question binds only inside the person's Routine draft its
own turn wrote, so a discarded, replaced, or completed draft revokes it. Any other composed answer to a create question,
a free-text one or one after a restart, continues that draft through the planner instead (ADR-0092 amendment,
2026-10-05); every other composed answer cannot change a Routine at all.
"""

from __future__ import annotations

import dataclasses
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from routine import record
from routine import request as routine_request

LINEAGE_SECONDS = 900
# The header of a composed answer: a blank line, then "<question label>: <question>", then "<answer label>: ". Whatever
# follows, on one line or many, it is never read as a fresh request.
_LABEL_CHARS = 40
_COMPOSED_RE = re.compile(r"\n\n[^\n:]{1,40}: [^\n]*\n[^\n:]{1,40}: ")


@dataclass(frozen=True, slots=True)
class Question:
    """One pending Routine question: who was asked about which request, and the Routine each option commits."""

    principal: str
    message: str
    question: str
    labels: tuple[str, ...]
    # The one open field: ("schedule",), ("timezone",), or ("input", step_id, member).
    field: tuple[str, ...]
    op: str
    expected_revision: int | None
    routines: tuple[record.Routine, ...]
    # What the person is told once each option's Routine commits, written for exactly that Routine.
    replies: tuple[str, ...]
    expires_at: float = 0.0
    # The Routine's words the asking request compiled from, frozen with it; an answer never reselects them.
    words: tuple[routine_request.Part, ...] = ()
    # The person's Routine draft the same commit wrote with a create question; only that draft lets it bind
    # (ADR-0092 amendment, 2026-10-05). None for an update question, or when the words did not fit a draft.
    generation: str | None = None


@dataclass(frozen=True, slots=True)
class Answer:
    """A message bound to a pending question: the question and the index of the option it selects."""

    question: Question
    index: int

    @property
    def label(self) -> str:
        return self.question.labels[self.index]

    @property
    def reply(self) -> str:
        """The reply written for the selected option's own Routine, never one written for another option."""
        return self.question.replies[self.index]

    @property
    def words(self) -> tuple[routine_request.Part, ...]:
        """The Routine's words: the asking request's, then the selected label, the person's own answer."""
        return (*self.question.words, (routine_request.SAID, self.label))

    @property
    def routine(self) -> record.Routine:
        """The selected option's Routine, its evidence naming the field, the label, and the words with that answer.

        The label is appended after every span the option's evidence already holds, so none of them moves.
        """
        value = self.question.routines[self.index]
        selected = {"field": list(self.question.field), "label": self.label}
        message = routine_request.commitment(self.words)
        return dataclasses.replace(value, grant={**value.grant, "selected": selected, "message": message})


def composed(message: str) -> bool:
    """Whether a message holds a composed clarification answer's header anywhere, bound or not, of any length."""
    return _COMPOSED_RE.search(message) is not None


def label(value: str) -> bool:
    """Whether text can be a composed answer's question or answer label: 1-40 characters, no colon or newline."""
    return 0 < len(value) <= _LABEL_CHARS and ":" not in value and "\n" not in value


def _selected(question: Question, message: str) -> int | None:
    """The option a composed answer to exactly this question selects, or None for any other message."""
    prefix = question.message + "\n\n"
    if not message.startswith(prefix):
        return None
    lines = message[len(prefix) :].split("\n")
    if len(lines) != 2:
        return None
    asked, answered = lines
    if not asked.endswith(": " + question.question) or not label(asked[: -len(question.question) - 2]):
        return None
    answer_label, separator, answer = answered.partition(": ")
    if not separator or not label(answer_label) or answer not in question.labels:
        return None
    return question.labels.index(answer)


class LineageBook:
    """At most one pending Routine question per Team, for 15 minutes or until its Routine commits."""

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._questions: dict[str, Question] = {}

    def record(self, team_id: str, question: Question) -> None:
        """Keep the question a turn of this Team just asked; it replaces any earlier one."""
        now = self._now()
        kept = dataclasses.replace(question, expires_at=now + LINEAGE_SECONDS)
        with self._lock:
            self._expire(now)
            self._questions[team_id] = kept

    def bound(self, team_id: str, principal: str, message: str) -> Answer | None:
        """The Team's pending question this message answers for the same principal, or None; nothing is consumed."""
        with self._lock:
            self._expire(self._now())
            question = self._questions.get(team_id)
        if question is None or question.principal != principal:
            return None
        index = _selected(question, message)
        return None if index is None else Answer(question, index)

    def settle(self, team_id: str, question: Question) -> None:
        """Remove exactly this question once the Routine an answer selected committed."""
        with self._lock:
            if self._questions.get(team_id) is question:
                del self._questions[team_id]

    def drop(self, team_id: str) -> None:
        with self._lock:
            self._questions.pop(team_id, None)

    def clear(self) -> None:
        with self._lock:
            self._questions.clear()

    def _expire(self, now: float) -> None:
        """Release every Team's expired question, so an idle Team keeps no Routine options; the lock is held."""
        for team_id in [team_id for team_id, question in self._questions.items() if question.expires_at <= now]:
            del self._questions[team_id]
