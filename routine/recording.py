"""Team's deterministic recording of a Routine plan from a person's recent chat sends, with no model (ADR-0101).

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

The schedule and the timezone are the person's own: the latest send that states a schedule (``routine.phrase``), and
the latest send naming an IANA zone, else the latest send's browser zone. What cannot be read is asked, never guessed.
"""

from __future__ import annotations

import dataclasses
import datetime
import heapq
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from protocol.http.v1 import routine as http_routine
from routine import phrase, schedule, trace
from routine import plan as routine_plan

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
    """What the ``record`` call chose: the output mode, when a decision runs, and extra decision Actions."""

    mode: str
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
    """The choice a target question binds: the input of one call it asked about, and every target it may take.

    Once the person chose a target other than the one the work used, ``chosen`` holds it, and the work run again
    must send exactly that target in the same Action's input.
    """

    consumer: str
    action: tuple[str, str]
    member: str
    # Each target as (value, label).
    targets: tuple[tuple[object, str | None], ...]
    chosen: object = None


@dataclass(frozen=True, slots=True)
class Asked:
    """The question a span last asked, which the person's later sends may answer."""

    code: str
    # How many sends the span held when it asked: only later sends answer it.
    after: int
    pending: Pending | None = None


@dataclass(frozen=True, slots=True)
class Question:
    """What Team asks the person before a card; ``options`` are targets ``{value, label}``."""

    code: str
    options: tuple[dict[str, object], ...] = ()
    value: int | None = None
    # The choice a target question binds, which only Team keeps.
    pending: Pending | None = field(default=None, compare=False)

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
    """A replaced Routine: its plan, and the schedule and zone a replacement keeps unless the person states others."""

    plan: Mapping[str, object]
    schedule: dict[str, object]
    timezone: str
    timezone_source: str


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
    zone: tuple[str, str] | None
    asked: Asked | None
    contracts: Mapping[tuple[str, str], routine_plan.ActionContract]
    # Whether changing calls replay on every run, so their results are sources too: never in a decision.
    replays_changes: bool = True
    # Every call's source representative, and each plan call's classified inputs and their origins.
    classes: dict[int, int] = field(default_factory=dict)
    inputs: dict[int, tuple[dict[str, dict[str, object]], dict[str, str]]] = field(default_factory=dict)


def record(
    sends: Sequence[Send],
    recording: Recording,
    protection: trace.Protection,
    contracts: Mapping[tuple[str, str], routine_plan.ActionContract],
    *,
    asked: Asked | None = None,
    existing: Existing | None = None,
) -> Recorded | Question:
    """The plan a recording span defines, or the question to ask first; raises RecordingError when it cannot be one."""
    _admit(recording, contracts)
    if protection.lost:
        raise RecordingError("routine-recording-unavailable")
    calls = _calls(sends, contracts)
    texts = [line for send in sends for line in (*_lines(send.person), *send.window)]
    latest = [call for call in calls if call.send == calls[-1].send] if calls else []
    try:
        context = _Context(sends, calls, _known(texts, protection), _zone(sends, existing), asked, contracts)
        context.replays_changes = recording.mode != "decide"
        if not latest and existing is not None:
            return _kept(context, recording, existing)
        work = [call for call in latest if call.read_only or recording.mode != "decide"]
        document, origins = _plan(context, recording, work)
        when = _schedule(sends, existing)
        timezone, source = _zoned(context, when, document)
    except _AskError as asking:
        return asking.question
    document["timezone"] = timezone
    actions = [(step["assistant"], step["action"]) for step in document["steps"]]
    changing = [call.action for call in latest if not call.read_only]
    permitted = _permitted(actions + changing + list(recording.decide_actions), contracts)
    return Recorded(document, origins, permitted, when, timezone, source)


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


def _admit(recording: Recording, contracts: Mapping[tuple[str, str], routine_plan.ActionContract]) -> None:
    decide = recording.mode == "decide"
    if recording.mode not in MODES or (recording.when in WHEN) != decide or (not decide and recording.when is not None):
        raise RecordingError("routine-recording-invalid")
    actions = set(recording.decide_actions)
    if (actions and not decide) or len(actions) > MAX_DECIDE_ACTIONS or not actions <= set(contracts):
        raise RecordingError("routine-decide-action-invalid")


def _permitted(
    actions: list[tuple[str, str]], contracts: Mapping[tuple[str, str], routine_plan.ActionContract]
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "assistant": assistant,
            "action": action,
            "pin": contracts[(assistant, action)].pin,
            "read_only": contracts[(assistant, action)].read_only,
            "stored_inputs": sorted(contracts[(assistant, action)].stored_inputs),
        }
        for assistant, action in sorted(set(actions))
    )


# --- When it runs ----------------------------------------------------------------------------------------------------


def _schedule(sends: Sequence[Send], existing: Existing | None) -> dict[str, object]:
    """The schedule the latest authored segment stating one states; a replacement keeps its own when none is stated.

    Every reading within one segment counts, so different schedules in one text are asked about.
    """
    latest: tuple[dict[str, object], ...] = ()
    for send in sends:
        for segment in send.person:
            found = phrase.stated(segment)
            if found:
                latest = found
    if len(latest) == 1:
        return dict(latest[0])
    if not latest and existing is not None:
        return dict(existing.schedule)
    raise _AskError(Question("routine-schedule-unstated"))


def _zone(sends: Sequence[Send], existing: Existing | None) -> tuple[str, str] | None:
    """The zone the latest authored segment names, else a replaced Routine's, else the latest browser zone, or None."""
    for segment in reversed([segment for send in sends for segment in send.person]):
        written = phrase.zones(segment)
        if len(written) > 1:
            raise _AskError(Question("routine-timezone-ambiguous"))
        if written:
            return written[0], "person"
    if existing is not None and existing.timezone_source != "none":
        return existing.timezone, existing.timezone_source
    browser = sends[-1].timezone if sends else None
    return None if browser is None else (browser, "browser")


def _zoned(context: _Context, when: dict[str, object], document: dict[str, object]) -> tuple[str, str]:
    """The Routine's zone: required by a calendar schedule or a run date, only a convention when neither needs one."""
    if context.zone is not None:
        return context.zone
    if when["kind"] in http_routine.CALENDAR_KINDS or routine_plan.clocked(document):
        raise _AskError(Question("routine-timezone-unstated"))
    return http_routine.CONVENTIONAL_TIMEZONE, "none"


# --- What it runs ----------------------------------------------------------------------------------------------------


def _kept(context: _Context, recording: Recording, existing: Existing) -> Recorded:
    """A replacement that ran no Action: the replaced plan's steps exactly, on a new schedule, zone, or output."""
    steps = [dict(step) for step in existing.plan["steps"]]
    for step in steps:
        contract = context.contracts.get((step["assistant"], step["action"]))
        if contract is None or contract.pin != step["pin"]:
            raise RecordingError("plan-pin-drift")
    if not steps and recording.mode != "decide":
        raise RecordingError("routine-recording-empty")
    origins = {
        step["id"]: {member: _kept_origin(source, context.known) for member, source in step["input"].items()}
        for step in steps
    }
    document = _document(steps, steps[-1]["id"] if steps else None, recording)
    when = _schedule(context.sends, existing)
    timezone, source = _zoned(context, when, document)
    document["timezone"] = timezone
    actions = [(step["assistant"], step["action"]) for step in steps] + list(recording.decide_actions)
    return Recorded(document, origins, _permitted(actions, context.contracts), when, timezone, source)


def _document(steps: list[dict[str, object]], last: str | None, recording: Recording) -> dict[str, object]:
    return {
        "version": routine_plan.VERSION,
        "timezone": http_routine.CONVENTIONAL_TIMEZONE,
        "steps": steps,
        "output": {
            "mode": recording.mode,
            "step": last if recording.mode in routine_plan.SHOWN_MODES else None,
            "when": recording.when,
        },
    }


def _kept_origin(source: Mapping[str, object], known: _Known) -> str:
    """How the card names a kept input's origin, as a recording would now."""
    if source["kind"] == "literal":
        return "request" if known.names(source["value"]) else "assistant"
    if source["kind"] == "run_clock":
        return "clock"
    return "selector" if "where" in source else "step"


def _plan(context: _Context, recording: Recording, work: list[_Call]) -> tuple[dict[str, object], dict]:
    """The plan document and each step's input origins, from the work and every source it needs."""
    if not work and recording.mode != "decide":
        raise RecordingError("routine-recording-empty")
    _rerun(context, work)
    _classes(context)
    nodes = _closure(context, work)
    _split(context, work, nodes)
    order = _ordered(context, nodes)
    names = {node: f"s{position}" for position, node in enumerate(order, start=1)}
    steps = [_step(context, nodes[node], names) for node in order]
    origins = {names[node]: context.inputs[node][1] for node in order}
    work_nodes = {context.classes[call.index] for call in work}
    shown = next((names[node] for node in reversed(order) if node in work_nodes), None)
    _verify(context, nodes, steps, names)
    return _document(steps, shown, recording), origins


def _closure(context: _Context, work: list[_Call]) -> dict[int, _Call]:
    """The plan calls: each work call's source representative, and every source their inputs read, transitively."""
    nodes: dict[int, _Call] = {}
    pending = [context.classes[call.index] for call in work]
    while pending:
        node = pending.pop()
        if node in nodes:
            continue
        nodes[node] = context.calls[node]
        _classify_call(context, nodes[node])
        inputs, _origins = context.inputs[node]
        pending.extend(source["node"] for source in inputs.values() if "node" in source)
    return nodes


def _step(context: _Context, call: _Call, names: dict[int, str]) -> dict[str, object]:
    inputs, _origins = context.inputs[call.index]
    return {
        "id": names[call.index],
        "assistant": call.occurrence.assistant,
        "action": call.occurrence.action,
        "pin": call.occurrence.pin,
        "input": {member: _named(source, names) for member, source in inputs.items()},
    }


def _named(source: dict[str, object], names: dict[int, str]) -> dict[str, object]:
    if "node" not in source:
        return dict(source)
    named = {key: value for key, value in source.items() if key != "node"}
    return {"kind": "step_output", "step": names[source["node"]], **named}


def _classes(context: _Context) -> None:
    """Each call's source: its earliest read-only twin (same Action, input, and result) with no change between."""
    open_sources: list[_Call] = []
    for call in context.calls:
        if not call.read_only:
            # Nothing read before a change is the same source as anything read after it.
            open_sources = []
            context.classes[call.index] = call.index
            continue
        twin = next((item for item in open_sources if _twins(item.occurrence, call.occurrence)), None)
        if twin is None:
            open_sources.append(call)
        context.classes[call.index] = call.index if twin is None else twin.index


def _twins(left: trace.Occurrence, right: trace.Occurrence) -> bool:
    """Whether two read-only calls are one source: the same Action, input, and result as JSON values compare them.

    A call holding anything withheld is only itself.
    """
    kept = (left.input, left.result, right.input, right.result)
    return (
        not any(item.withheld or item.oversize for item in kept)
        and (left.assistant, left.action) == (right.assistant, right.action)
        and routine_plan.same(left.input.value, right.input.value)
        and routine_plan.same(left.result.value, right.result.value)
    )


def _rerun(context: _Context, work: list[_Call]) -> None:
    """Ask again until the work, run again for the person's chosen target, sends exactly that target."""
    pending = None if context.asked is None else context.asked.pending
    if pending is None or pending.chosen is None:
        return
    sent = [
        call.occurrence.input.value.get(pending.member)
        for call in work
        if call.action == pending.action and isinstance(call.occurrence.input.value, dict)
    ]
    if not sent or not all(routine_plan.same(value, pending.chosen) for value in sent):
        raise _AskError(Question("routine-work-rerun", pending=pending))


def _split(context: _Context, work: list[_Call], nodes: dict[int, _Call]) -> None:
    """Ask when an earlier send ran a work Action for something the work did not run again."""
    latest = context.calls[-1].send if context.calls else None
    pending = None if context.asked is None else context.asked.pending
    for call in context.calls:
        if call.send == latest or context.classes[call.index] in nodes:
            continue
        if pending is not None and pending.chosen is not None and call.action == pending.action:
            # The work ran again for the person's chosen target: the earlier call it replaces is no split.
            continue
        same = [item for item in work if item.action == call.action]
        if same and not any(_same_input(item.occurrence, call.occurrence) for item in same):
            raise _AskError(Question("routine-work-split"))


def _same_input(left: trace.Occurrence, right: trace.Occurrence) -> bool:
    return (
        not left.input.withheld and not right.input.withheld and routine_plan.same(left.input.value, right.input.value)
    )


def _edges(context: _Context, nodes: dict[int, _Call]) -> dict[int, set[int]]:
    """Each plan call's successors: what reads it, and every call on the far side of a changing call."""
    edges: dict[int, set[int]] = {node: set() for node in nodes}
    for node in nodes:
        for source in context.inputs[node][0].values():
            if "node" in source:
                edges[source["node"]].add(node)
    for changing in (node for node, call in nodes.items() if not call.read_only):
        for other in nodes:
            if other < changing:
                edges[other].add(changing)
            elif other > changing:
                edges[changing].add(other)
    return edges


def _ordered(context: _Context, nodes: dict[int, _Call]) -> list[int]:
    """The plan calls in data-dependency order, earliest first, every changing call where it ran; refuses a cycle."""
    edges = _edges(context, nodes)
    incoming = dict.fromkeys(nodes, 0)
    for targets in edges.values():
        for target in targets:
            incoming[target] += 1
    ready = [node for node, count in incoming.items() if count == 0]
    heapq.heapify(ready)
    order: list[int] = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for target in edges[node]:
            incoming[target] -= 1
            if incoming[target] == 0:
                heapq.heappush(ready, target)
    if len(order) != len(nodes):
        changed = any(not call.read_only for node, call in nodes.items() if node not in order)
        raise RecordingError("routine-recording-conflict" if changed else "routine-recording-cyclic")
    return order


# --- Each input ------------------------------------------------------------------------------------------------------


def _classify_call(context: _Context, call: _Call) -> None:
    """Classify every top-level input member of one plan call; refuses a secret."""
    given = call.occurrence.input
    if given.oversize:
        raise RecordingError("routine-recording-too-large")
    schema = context.contracts[call.action].input_schema
    if given.withheld or not isinstance(given.value, dict) or trace.exposes(given.value, context.known.protected):
        raise RecordingError("routine-secret-literal")
    try:
        secret = routine_plan.secret_literal(schema, given.value)
    except routine_plan.PlanError, RecursionError:
        secret = True
    if secret:
        raise RecordingError("routine-secret-literal")
    inputs: dict[str, dict[str, object]] = {}
    origins: dict[str, str] = {}
    for member, value in given.value.items():
        inputs[member], origins[member] = _classified(context, (call, member), value)
    context.inputs[call.index] = (inputs, origins)


def _classified(context: _Context, input_: tuple[_Call, str], value: object) -> tuple[dict[str, object], str]:
    call, _member = input_
    answered = _answered(context, input_, value)
    if answered or (answered is None and context.known.names(value)):
        return {"kind": "literal", "value": value}, "request"
    clock = _clock(context, call, value)
    if clock is not None:
        return clock
    if not _referable(value):
        return {"kind": "literal", "value": value}, "assistant"
    source = _sourced(context, input_, value)
    if source is None:
        return {"kind": "literal", "value": value}, "assistant"
    _unexposed(source, context.known)
    return source, "selector" if "where" in source else "step"


def _clock(context: _Context, call: _Call, value: object) -> tuple[dict[str, object], str] | None:
    """The run date, when the value is the UTC date its own send started on; None when it is not that date."""
    started = context.sends[call.send].started_at
    if not isinstance(value, str) or value != _date_at(started, "UTC").isoformat():
        return None
    if context.zone is None:
        raise _AskError(Question("routine-timezone-unstated"))
    if _date_at(started, context.zone[0]) == _date_at(started, "UTC"):
        return {"kind": "run_clock", "format": "date"}, "clock"
    return {"kind": "literal", "value": value}, "assistant"


def _date_at(instant: int, timezone: str) -> datetime.date:
    return datetime.datetime.fromtimestamp(instant, schedule.zone(timezone)).date()


def _referable(value: object) -> bool:
    if isinstance(value, str):
        return len(value) >= MIN_REF_STRING
    if type(value) is int:
        return len(str(abs(value))) >= MIN_REF_INTEGER_DIGITS
    return isinstance(value, dict | list) and bool(value)


# A position inside a kept result: its tokens, and which of them index an array.
_Position = tuple[tuple[str, ...], tuple[bool, ...]]


def _positions(node: object, value: object, at: _Position = ((), ())) -> Iterator[_Position]:
    if routine_plan.same(node, value):
        yield at
    tokens, arrays = at
    if isinstance(node, dict):
        for key, item in node.items():
            yield from _positions(item, value, ((*tokens, key), (*arrays, False)))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _positions(item, value, ((*tokens, str(index)), (*arrays, True)))


def _pointer(tokens: tuple[str, ...]) -> str:
    return "".join("/" + trace.escape(token) for token in tokens)


def _holders(context: _Context, consumer: _Call, value: object) -> dict[int, list[_Position]]:
    """Each source representative whose replayed result holds ``value``, and the available positions it is at."""
    found: dict[int, list[_Position]] = {}
    for call in context.calls:
        representative = context.classes[call.index]
        replayed = call.read_only or context.replays_changes
        if call.index == consumer.index or not replayed or representative in found:
            continue
        positions = [
            position
            for position in _positions(call.occurrence.result.value, value)
            if call.occurrence.result.available(_pointer(position[0]))
        ]
        if positions:
            found[representative] = positions
    return found


def _identifier(value: object) -> bool:
    """Whether a referable value is shaped like an identifier: one word of text, or a whole number."""
    return type(value) is int or (isinstance(value, str) and not any(character.isspace() for character in value))


def _answered(context: _Context, input_: tuple[_Call, str], value: object) -> bool | None:
    """How the person answered the target question about this very input: None when none is pending for it.

    True when they chose exactly this value; False when their answer chose no target exactly, or named the target's
    item, which the ordinary reading then selects. A substring of an answer never confirms a target, and choosing
    another target asks for the work again.
    """
    call, member = input_
    asked = context.asked
    pending = None if asked is None else asked.pending
    if pending is None or member != pending.member:
        return None
    if pending.chosen is not None:
        return True if call.action == pending.action and routine_plan.same(value, pending.chosen) else None
    if call.occurrence.operation_id != pending.consumer:
        return None
    selection = _selection(context, asked)
    if selection is None:
        return False
    chosen, by_label = selection
    if not routine_plan.same(chosen, value):
        raise _AskError(Question("routine-work-rerun", pending=dataclasses.replace(pending, chosen=chosen)))
    return not by_label


def _selection(context: _Context, asked: Asked) -> tuple[object, bool] | None:
    """The target the person's latest answer selects exactly, by its value or by a label no other target shares."""
    targets = asked.pending.targets
    for segment in reversed([segment for send in context.sends[asked.after :] for segment in send.person]):
        answer = segment.strip()
        by_value = [value for value, _label in targets if answer == (value if isinstance(value, str) else str(value))]
        by_label = [value for value, label in targets if label is not None and answer == label]
        if len(by_value) == 1:
            return by_value[0], False
        if len(by_label) == 1:
            return by_label[0], True
    return None


def _sourced(context: _Context, input_: tuple[_Call, str], value: object) -> dict[str, object] | None:
    """The one source occurrence and position holding ``value``, or None for text nothing holds; else it asks.

    An identifier nothing returned is asked about, since freezing one would replay a remembered id; free text or a
    container the assistant wrote and nothing returned is its own choice, the same on every run.
    """
    consumer, _member = input_
    found = _holders(context, consumer, value)
    if not found:
        if not _identifier(value):
            return None
        raise _AskError(Question("routine-binding-unsourced"))
    if len(found) > 1:
        raise _ambiguous(input_, value, [{"value": value, "label": None}])
    ((node, positions),) = found.items()
    bindings = [_binding(context, node, position) for position in positions]
    if len(bindings) == 1 and "options" not in bindings[0]:
        return bindings[0]
    selected = [item for item in bindings if "where" in item]
    if len(selected) == 1:
        return selected[0]
    options = [option for item in bindings for option in item.get("options") or [{"value": value, "label": None}]]
    raise _ambiguous(input_, value, options)


def _ambiguous(input_: tuple[_Call, str], value: object, options: list[dict[str, object]]) -> Exception:
    """The question which target an input means: a scalar's choices; a container is never chosen and refuses."""
    if isinstance(value, dict | list):
        return RecordingError("routine-recording-ambiguous")
    choices: list[dict[str, object]] = []
    for option in options:
        if all(option["value"] != item["value"] for item in choices) and _offered(option):
            choices.append(option)
    call, member = input_
    targets = tuple((item["value"], item["label"]) for item in choices)
    pending = Pending(call.occurrence.operation_id, call.action, member, targets)
    shown = tuple(choices) if len(choices) <= http_routine.MAX_QUESTION_OPTIONS else ()
    return _AskError(Question("routine-binding-ambiguous", shown, pending=pending))


def _offered(option: dict[str, object]) -> bool:
    """Whether a target can be shown as one choice of the question."""
    question = Question("routine-binding-ambiguous", (option,)).wire()
    return http_routine.canonical_question(question) is not None


def _binding(context: _Context, node: int, position: _Position) -> dict[str, object]:
    """How one position is read: its pointer outside arrays, or through one array by the member the person named.

    Never by an index, which a reordered result would point at another item; anything else offers its targets.
    """
    tokens, arrays = position
    crossed = [place for place, is_array in enumerate(arrays) if is_array]
    if not crossed:
        return {"node": node, "pointer": _pointer(tokens)}
    if len(crossed) > 1:
        return {"options": []}
    result = context.calls[node].occurrence.result
    place = crossed[0]
    array_tokens, rest = tokens[:place], tokens[place + 1 :]
    items = _at(result.value, array_tokens)
    chosen = int(tokens[place])
    item = items[chosen]
    named = [
        (key, constant)
        for key, constant in (item.items() if isinstance(item, dict) else ())
        if (not rest or key != rest[0]) and _selectable(constant, context.known)
    ]
    unique = [pair for pair in named if _unique(result, array_tokens, items, chosen, *pair)]
    if len(unique) == 1:
        return {"node": node, "pointer": _pointer(array_tokens), "where": dict(unique), "item": _pointer(rest)}
    if named:
        return {"options": _targets(items, named, rest)}
    return {"options": _listed(items, rest)}


def _targets(items: list, named: list[tuple[str, object]], rest: tuple[str, ...]) -> list[dict[str, object]]:
    """Each array item sharing a named member, as a target: the value its input would take and that member."""
    targets = []
    for key, constant in named:
        for item in items:
            if isinstance(item, dict) and key in item and routine_plan.same(item[key], constant):
                chosen = _reached(item, rest)
                if isinstance(chosen, str | int) and not isinstance(chosen, bool):
                    targets.append({"value": chosen, "label": constant if isinstance(constant, str) else None})
    return targets


# The members whose text names an item to a person, such as a zone's domain: a target's label when it has one only.
_NAME_MEMBERS = frozenset({"name", "title", "label", "display_name", "hostname", "domain"})


def _listed(items: list, rest: tuple[str, ...]) -> list[dict[str, object]]:
    """Every array item as a target: the value its input would take, labelled by its one name-like member, if any."""
    targets = []
    for item in items:
        chosen = _reached(item, rest)
        if isinstance(chosen, str | int) and not isinstance(chosen, bool):
            targets.append({"value": chosen, "label": _label(item)})
    return targets


def _label(item: object) -> str | None:
    names = [
        value
        for key, value in (item.items() if isinstance(item, dict) else ())
        if key in _NAME_MEMBERS and isinstance(value, str) and 0 < len(value) <= http_routine.MAX_QUESTION_OPTION_CHARS
    ]
    return names[0] if len(names) == 1 else None


def _reached(value: object, tokens: tuple[str, ...]) -> object:
    """The node an item's member tokens reach, or None when one names nothing there; they cross no array."""
    for token in tokens:
        if not isinstance(value, dict) or token not in value:
            return None
        value = value[token]
    return value


def _at(value: object, tokens: tuple[str, ...]) -> object:
    """The node a position's tokens reach inside a kept value; every token names an existing member or index."""
    for token in tokens:
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _selectable(constant: object, known: _Known) -> bool:
    if isinstance(constant, str):
        return len(constant) >= MIN_SELECTOR_CHARS and known.names(constant)
    return type(constant) is int and known.names(constant)


def _unique(result: trace.Kept, array_tokens: tuple[str, ...], items: list, chosen: int, key: str, constant) -> bool:
    """Whether no other item holds the same member value, and none hides that member where it could."""
    for index, other in enumerate(items):
        if index == chosen:
            continue
        if not result.available(_pointer((*array_tokens, str(index), key))):
            return False
        if isinstance(other, dict) and key in other and routine_plan.same(other[key], constant):
            return False
    return True


def _unexposed(source: dict[str, object], known: _Known) -> None:
    """Refuse a reference no plan pointer can hold, or whose path or selector holds a value the span protects."""
    pointers = [source["pointer"], source.get("item", "")]
    tokens = [routine_plan.pointer_tokens(pointer) for pointer in pointers]
    if any(item is None for item in tokens):
        raise RecordingError("routine-recording-too-large")
    flat = [token for item in tokens for token in item]
    if trace.exposes([*pointers, *flat, source.get("where", {})], known.protected):
        raise RecordingError("routine-secret-literal")


# --- Verification ----------------------------------------------------------------------------------------------------


def _verify(context: _Context, nodes: dict[int, _Call], steps: list[dict[str, object]], names: dict[int, str]) -> None:
    """Every step, resolved against the recorded results, reproduces what each call it stands for sent."""
    results = {names[node]: call.occurrence.result.value for node, call in nodes.items()}
    by_name = {step["id"]: step for step in steps}
    for call in context.calls:
        node = context.classes[call.index]
        if node not in nodes:
            continue
        step = by_name[names[node]]
        sent = call.occurrence.input.value
        if not isinstance(sent, dict) or set(sent) != set(step["input"]):
            raise RecordingError("routine-recording-unverified")
        started = context.sends[call.send].started_at
        for member, source in step["input"].items():
            if not routine_plan.same(_resolved(context, source, results, started), sent[member]):
                raise RecordingError("routine-recording-unverified")


def _resolved(context: _Context, source: Mapping[str, object], results: Mapping[str, object], started: int) -> object:
    if source["kind"] == "literal":
        return source["value"]
    if source["kind"] == "run_clock":
        instant = datetime.datetime.fromtimestamp(started, datetime.UTC)
        return routine_plan.clock_value(source["format"], instant, context.zone[0])
    try:
        if "where" in source:
            return routine_plan.select_where(
                results[source["step"]], source["pointer"], source["where"], source["item"]
            )
        return routine_plan.select(results[source["step"]], source["pointer"])
    except routine_plan.PlanError:
        return _UNRESOLVED


_UNRESOLVED = object()
