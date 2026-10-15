"""Team's deterministic recording of a Routine plan from a person's recent chat sends, with no model (ADR-0101).

This module holds the recording's inputs, questions, and results; ``routine.compose`` builds the plan, while
``routine.provenance`` classifies each input and ``routine.rerun`` names the work a question asks to run again.
The chat agent runs the recurring work and calls ``record``; Team builds the plan from its own memory-only trace of the
recording span: the person's consecutive fresh sends in one Team incarnation, each with its own message, the person's
own words in it, the untruncated earlier sends its conversation window carried, its browser timezone, its start, and
its successful Action calls in dispatch order.

The work is the calls of the latest send that ran any Action; earlier sends only provide sources. Each top-level input
member of a plan call is classified by the first rule that applies:

1. a secret (withheld, credential-shaped, protected by the span, or bound for a secret destination) refuses;
2. a non-empty string occurring in one line the person wrote, or a number whose JSON text is a whole token of one, is
   a literal the person named;
3. the date its own send started on in the Routine's zone is the run date, which each run reads in that zone; the
   UTC date of a send that started on another date there is a fixed literal;
4. a long string, a long integer, or a non-empty container is copied from the one call result in the span holding it:
   from its one position outside arrays, or through the array item whose single member the person named and no other
   item shares; never by an index, which a reordered result would point at another item. An identifier no result
   holds, or a value inside an array or several positions that no named member separates, is asked about and never
   frozen; free text or a container no result holds falls to rule 5 or 6;
5. text no result holds that writes dates near its send's date is a template whose dates each run writes for its own
   date (``routine.dates``); a date the person wrote stays as written;
6. anything else is a literal the assistant chose, the same on every run.

A source is one specific occurrence, a changing call's too wherever changes replay; read-only calls of the same Action
with identical input and result and no changing call between them are one source, its earliest. The plan holds every
work call and every source they need, ordered by their data dependencies with every changing call kept where it ran; a
conflict or a cycle refuses. Before any card, Team resolves the plan against the recorded results and requires every
reference to reproduce what each call sent.

The schedule, output, and timezone are the person's own, read by the protocol's phrase tables
(``protocol/http/v1/phrase.py``): the latest segment that states each one, else the latest send's browser zone. What
cannot be read is asked, never guessed.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import phrase
from protocol.http.v1 import routine as http_routine
from routine import plan as routine_plan
from routine import trace

# The one question only work run again answers: a value no result provides, or a target the person chose instead.
RERUN = "routine-work-rerun"
MIN_REF_STRING = 6
MIN_REF_INTEGER_DIGITS = 6
MIN_SELECTOR_CHARS = 2
# A complete number as written, its sign and exponent included, never a fragment of a longer number or word.
_NUMBER_RE = re.compile(r"(?<![\w.,+-])[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?(?!\w|[.,]\d)")


def clarified_answer(message: str) -> tuple[str, str, str] | None:
    """The original request, question, and answer of a message that is exactly Admin's composed clarification answer.

    The message must be the composition ``payload.compose_clarified`` makes of an original request, one question, and
    one answer in one interface language, so the answer is its last authored segment and nothing follows it.
    """
    lines = message.split("\n")
    if len(lines) < 4:
        return None
    for locale, labels in http_payload.CLARIFICATION_LABELS.items():
        question, answer = f"{labels['question']}: ", f"{labels['answer']}: "
        if not (lines[-2].startswith(question) and lines[-1].startswith(answer)):
            continue
        original, asked, given = "\n".join(lines[:-3]), lines[-2][len(question) :], lines[-1][len(answer) :]
        if (
            original.strip()
            and given.strip()
            and http_payload.compose_clarified(original, asked, given, locale) == message
        ):
            return original.strip(), asked, given
    return None


def stated_schedules(sends: Sequence[Send]) -> tuple[dict[str, object], ...]:
    """Every schedule the latest authored segment stating any states; none when no segment does."""
    latest: tuple[dict[str, object], ...] = ()
    for send in sends:
        for segment in send.person:
            latest = phrase.stated(segment) or latest
    return latest


def zone(sends: Sequence[Send]) -> tuple[str, str]:
    """The Routine's zone and where it came from.

    The one zone the latest authored segment naming any names; else the request's browser zone; else UTC by
    convention, with no source. A segment naming several zones names none of them.
    """
    for segment in reversed([segment for send in sends for segment in send.person]):
        written = phrase.zones(segment)
        if written:
            if len(written) == 1:
                return written[0], "person"
            break
    browser = sends[-1].timezone if sends else None
    return (http_routine.CONVENTIONAL_TIMEZONE, "none") if browser is None else (browser, "browser")


class RecordingError(ValueError):
    """The recording was refused; ``code`` is the stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
    # The chat's interface language, or None: it decides how a date the recorded work wrote is read.
    locale: str | None = None


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
    """One call a rerun must make: its Action, and what each input member must be.

    Each input is (member, kind, value): ``value`` must be sent exactly; ``clock`` must be the date its own send
    started on, in the Routine's own zone; ``fresh`` must be a value one of the rerun send's own results holds, so its
    provenance is new, and ``value`` then keeps the value the work sent, so distinct values stay distinct in the rerun.
    ``sources`` names, for a fresh member, the Action of the earliest eligible source that returned its value, never
    the call itself.
    """

    action: tuple[str, str]
    inputs: tuple[tuple[str, str, object], ...]
    sources: tuple[tuple[str, tuple[str, str]], ...] = ()


@dataclass(frozen=True, slots=True)
class Manifest:
    """The work a rerun question asks to run again, one slot per occurrence, in dispatch order.

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
    # How many sends the span held when it first asked for work to run again; later sends that ran Actions repaired it.
    repair_from: int | None = None


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
    repair_from: int | None = field(default=None, compare=False)

    def wire(self) -> dict[str, object]:
        """The question as the chat reply carries it: each target as its exact JSON text, which no client rounds."""
        options = [{"value": json_text(item["value"]), "label": item["label"]} for item in self.options]
        return {"code": self.code, "options": options, "value": self.value}


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


class AskError(Exception):
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
    """The recorded plan document (its zone included), each input's origin, the permitted Actions, and when it runs."""

    document: dict[str, object]
    origins: dict[str, dict[str, str]]
    permitted: tuple[dict[str, object], ...]
    schedule: dict[str, object]
    timezone_source: str


@dataclass(frozen=True, slots=True)
class Known:
    texts: tuple[str, ...]
    numbers: frozenset[str]
    protected: frozenset[str]

    def names(self, value: object) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, str):
            return bool(value) and any(value in text for text in self.texts)
        return isinstance(value, int | float) and routine_plan.canonical(value).decode() in self.numbers


def known_texts(texts: Sequence[str], protection: trace.Protection) -> Known:
    """The person's texts, with every whole number token each one writes on its own."""
    numbers = frozenset(match.group() for text in texts for match in _NUMBER_RE.finditer(text))
    return Known(tuple(texts), numbers, protection.values)


@dataclass(frozen=True, slots=True)
class Call:
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
class Context:
    sends: Sequence[Send]
    calls: list[Call]
    known: Known
    zone: tuple[str, str]
    asked: Asked | None
    contracts: Mapping[tuple[str, str], routine_plan.ActionContract]
    # The dispatch indexes of the work's calls, once the work is narrowed to what the person chose.
    work: tuple[int, ...] = ()
    # Every call's source representative, and each plan call's classified inputs and their origins.
    classes: dict[int, int] = field(default_factory=dict)
    inputs: dict[int, tuple[dict[str, dict[str, object]], dict[str, str]]] = field(default_factory=dict)


def segment_lines(segments: Sequence[str]) -> list[str]:
    """Each line of the person's segments: a name or number never spans two."""
    return [line for segment in segments for line in segment.split("\n")]


def span_calls(sends: Sequence[Send], contracts: Mapping[tuple[str, str], routine_plan.ActionContract]) -> list[Call]:
    """Every call of the span in dispatch order; one whose Action is no longer at its pin refuses the recording."""
    occurrences = [(position, item) for position, send in enumerate(sends) for item in send.occurrences]
    calls = [Call(index, send, occurrence) for index, (send, occurrence) in enumerate(occurrences)]
    for call in calls:
        contract = contracts.get(call.action)
        if contract is None or contract.pin != call.occurrence.pin:
            raise RecordingError("plan-pin-drift")
    return calls
