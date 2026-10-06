"""Team's deterministic recording of a Routine plan from a person's recent chat sends, with no model (ADR-0101).

This module holds the recording's inputs, questions, and results; ``routine.compose`` builds the plan, while
``routine.provenance`` classifies each input and ``routine.rerun`` checks a frozen re-run.
The chat agent runs the recurring work and calls ``record``; Team builds the plan from its own memory-only trace of the
recording span: the person's consecutive fresh sends in one Team incarnation, each with its own message, the person's
own words in it, the untruncated earlier sends its conversation window carried, its browser timezone, its start, and
its successful Action calls in dispatch order.

The work is the calls of the latest send that ran any Action; earlier sends only provide sources. Each top-level input
member of a plan call is classified by the first rule that applies:

1. a secret (withheld, credential-shaped, protected by the span, or bound for a secret destination) refuses;
2. a non-empty string occurring in one line the person wrote, or a number whose JSON text is a whole token of one, is
   a literal the person named;
3. the UTC date its own send started on is the run date, when that send did not start near local midnight; otherwise
   a fixed literal; with no timezone known, the person is asked for one;
4. a long string, a long integer, or a non-empty container is copied from the one call result in the span holding it:
   from its one position outside arrays, or through the array item whose single member the person named and no other
   item shares; never by an index, which a reordered result would point at another item. An identifier no result
   holds, or a value inside an array or several positions that no named member separates, is asked about and never
   frozen; free text or a container no result holds falls to rule 5;
5. anything else is a literal the assistant chose, the same on every run.

A source is one specific occurrence, a changing call's too wherever changes replay; read-only calls of the same Action
with identical input and result and no changing call between them are one source, its earliest. The plan holds every
work call and every source they need, ordered by their data dependencies with every changing call kept where it ran; a
conflict or a cycle refuses. Before any card, Team resolves the plan against the recorded results and requires every
reference to reproduce what each call sent.

The schedule, output, and timezone are the person's own, read by the protocol's phrase tables
(``protocol/http/v1/phrase.py``): the latest segment that states each one, else the latest send's browser zone. What
cannot be read is asked, never guessed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from routine import plan as routine_plan
from routine import trace

MODES = ("show", "changes", "none", "decide")
WHEN = ("always", "changes")
MAX_DECIDE_ACTIONS = 32
MIN_REF_STRING = 6
MIN_REF_INTEGER_DIGITS = 6
MIN_SELECTOR_CHARS = 2
# A complete number as written, its sign and exponent included, never a fragment of a longer number or word.
_NUMBER_RE = re.compile(r"(?<![\w.,+-])[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?(?!\w|[.,]\d)")


class RecordingError(ValueError):
    """The recording was refused; ``code`` is the stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Recording:
    """How each run handles its result: the output mode, when a decision runs, and extra decision Actions.

    A mode of None is the person's to state: Team reads it from their own words, and asks when they state none.
    """

    mode: str | None
    when: str | None
    decide_actions: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Send:
    """One fresh send of the person in a recording span."""

    message: str
    # The person's authored segments of the message, in order: its original text, then each composed answer.
    person: tuple[str, ...]
    # The person's own lines of each untruncated user entry of the conversation window the request carried.
    window: tuple[str, ...]
    # The browser's validated IANA zone, or None.
    timezone: str | None
    started_at: int
    occurrences: tuple[trace.Occurrence, ...] = ()


@dataclass(frozen=True, slots=True)
class Pending:
    """The choice a target question binds: the Action input it asked about, and every target that input may take.

    The choice is one logical target: an answer binds every occurrence of that input whose value is one of these
    targets, never an occurrence resolved independently. Once the person chose a target other than one the work used,
    ``chosen`` holds it, and the work run again must send exactly that target wherever it sent one of them.
    """

    action: tuple[str, str]
    member: str
    # Each target as (value, label).
    targets: tuple[tuple[object, str | None], ...]
    chosen: object = None


@dataclass(frozen=True, slots=True)
class Slot:
    """One call a rerun must make: its Action, whether it is read-only, and what each input member must be.

    Each input is (member, kind, value): ``value`` must be sent exactly; ``clock`` must be the date its own send
    started on, in UTC; ``fresh`` must be a value one of the rerun send's own results holds, so its provenance is new,
    and ``value`` then keeps the value the work sent, so distinct values stay distinct in the rerun. ``sources`` names,
    for a fresh member, the Action of the earliest eligible source that returned its value, never the call itself.
    """

    action: tuple[str, str]
    read_only: bool
    inputs: tuple[tuple[str, str, object], ...]
    sources: tuple[tuple[str, tuple[str, str]], ...] = ()


@dataclass(frozen=True, slots=True)
class Manifest:
    """The work a split, rerun, or unsourced question asks to run again, one slot per occurrence, in dispatch order.

    Read-only twins (the same Action, input, and result with no change between) are one occurrence; every other call
    is its own slot, so its multiplicity and its order relative to every change are kept.
    """

    slots: tuple[Slot, ...]


@dataclass(frozen=True, slots=True)
class Asked:
    """The question a span last asked, which the person's later sends may answer."""

    code: str
    # How many sends the span held when it asked: only later sends answer it.
    after: int
    pending: Pending | None = None
    manifest: Manifest | None = None
    # The question as the person was asked it, which the Brain sees beside a freely typed answer.
    wire: dict[str, object] | None = None
    # Every target choice the person already answered, each bound until the span ends.
    chosen: tuple[Pending, ...] = ()
    # The call whose result the work showed when Team asked for the output, which a chain must use: its Assistant,
    # Action, and its input and result as JSON text, which only an equivalent call shares.
    chained_from: tuple[str, str, str, str] | None = None


@dataclass(frozen=True, slots=True)
class Question:
    """What Team asks the person before a card; ``options`` are targets ``{value, label}``."""

    code: str
    options: tuple[dict[str, object], ...] = ()
    value: int | None = None
    # The choice a target question binds, and the work a rerun must repeat, which only Team keeps.
    pending: Pending | None = field(default=None, compare=False)
    manifest: Manifest | None = field(default=None, compare=False)
    chosen: tuple[Pending, ...] = field(default=(), compare=False)
    chained_from: tuple[str, str, str, str] | None = field(default=None, compare=False)
    # The first send whose calls count: the send that settled a rerun, or 0.
    frontier: int = field(default=0, compare=False)

    def wire(self) -> dict[str, object]:
        """The question as the chat reply carries it: each target as its exact JSON text, which no client rounds."""
        options = [{"value": _json_text(item["value"]), "label": item["label"]} for item in self.options]
        return {"code": self.code, "options": options, "value": self.value}


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


class _AskError(Exception):
    def __init__(self, question: Question) -> None:
        super().__init__(question.code)
        self.question = question


@dataclass(frozen=True, slots=True)
class Existing:
    """A replaced Routine: its plan, and the schedule a replacement keeps unless the person states another."""

    plan: Mapping[str, object]
    schedule: dict[str, object]


@dataclass(frozen=True, slots=True)
class Recorded:
    """The recorded plan document, each input's origin by step and member, the permitted Actions, and when it runs."""

    document: dict[str, object]
    origins: dict[str, dict[str, str]]
    permitted: tuple[dict[str, object], ...]
    schedule: dict[str, object]
    timezone: str
    timezone_source: str


@dataclass(frozen=True, slots=True)
class _Known:
    texts: tuple[str, ...]
    numbers: frozenset[str]
    protected: frozenset[str]

    def names(self, value: object) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, str):
            return bool(value) and any(value in text for text in self.texts)
        return isinstance(value, int | float) and routine_plan.canonical(value).decode() in self.numbers


def _known(texts: Sequence[str], protection: trace.Protection) -> _Known:
    """The person's texts, with every whole number token each one writes on its own."""
    numbers = frozenset(match.group() for text in texts for match in _NUMBER_RE.finditer(text))
    return _Known(tuple(texts), numbers, protection.values)


@dataclass(frozen=True, slots=True)
class _Call:
    """One call of the span: its global dispatch index, its send, and its occurrence."""

    index: int
    send: int
    occurrence: trace.Occurrence

    @property
    def action(self) -> tuple[str, str]:
        return (self.occurrence.assistant, self.occurrence.action)

    @property
    def read_only(self) -> bool:
        return self.occurrence.read_only


@dataclass(slots=True)
class _Context:
    sends: Sequence[Send]
    calls: list[_Call]
    known: _Known
    zone: tuple[str, str]
    asked: Asked | None
    contracts: Mapping[tuple[str, str], routine_plan.ActionContract]
    # Whether changing calls replay on every run, so their results are sources too: never in a decision.
    replays_changes: bool = True
    # The send that settled a rerun: no call before it counts as split work.
    frontier: int = 0
    # The call each step of the plan stands for, by step id.
    planned: dict[str, trace.Occurrence] = field(default_factory=dict)
    # Every call's source representative, and each plan call's classified inputs and their origins.
    classes: dict[int, int] = field(default_factory=dict)
    inputs: dict[int, tuple[dict[str, dict[str, object]], dict[str, str]]] = field(default_factory=dict)


def _lines(segments: Sequence[str]) -> list[str]:
    """Each line of the person's segments: a name or number never spans two."""
    return [line for segment in segments for line in segment.split("\n")]


def _calls(sends: Sequence[Send], contracts: Mapping[tuple[str, str], routine_plan.ActionContract]) -> list[_Call]:
    """Every call of the span in dispatch order; one whose Action is no longer at its pin refuses the recording."""
    occurrences = [(position, item) for position, send in enumerate(sends) for item in send.occurrences]
    calls = [_Call(index, send, occurrence) for index, (send, occurrence) in enumerate(occurrences)]
    for call in calls:
        contract = contracts.get(call.action)
        if contract is None or contract.pin != call.occurrence.pin:
            raise RecordingError("plan-pin-drift")
    return calls
