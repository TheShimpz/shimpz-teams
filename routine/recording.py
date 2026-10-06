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
    and ``value`` then keeps the value the work sent, so distinct values stay distinct in the rerun.
    """

    action: tuple[str, str]
    read_only: bool
    inputs: tuple[tuple[str, str, object], ...]


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


@dataclass(frozen=True, slots=True)
class Question:
    """What Team asks the person before a card; ``options`` are targets ``{value, label}``."""

    code: str
    options: tuple[dict[str, object], ...] = ()
    value: int | None = None
    # The choice a target question binds, and the work a rerun must repeat, which only Team keeps.
    pending: Pending | None = field(default=None, compare=False)
    manifest: Manifest | None = field(default=None, compare=False)
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
    frontier: int = 0,
) -> Recorded | Question:
    """The plan a recording span defines, or the question to ask first; raises RecordingError when it cannot be one.

    A pending rerun question stands until one later send's calls satisfy its manifest; that send is then the new
    ``frontier``, and no call before it counts as split work any more, though each may still be a source.
    """
    _admit(recording, contracts)
    if protection.lost:
        raise RecordingError("routine-recording-unavailable")
    if asked is not None and asked.manifest is not None:
        settled = settlement(sends, asked)
        if settled is None:
            return Question(asked.code, pending=asked.pending, manifest=asked.manifest, frontier=frontier)
        frontier = max(frontier, settled)
    calls = _calls(sends, contracts)
    texts = [line for send in sends for line in (*_lines(send.person), *send.window)]
    latest = [call for call in calls if call.send == calls[-1].send] if calls else []
    context = _Context(sends, calls, _known(texts, protection), _zone(sends), asked, contracts)
    context.replays_changes = recording.mode != "decide"
    context.frontier = frontier
    work = [call for call in latest if call.read_only or recording.mode != "decide"]
    try:
        if not latest and existing is not None:
            return _kept(context, recording, existing)
        document, origins = _plan(context, recording, work)
        when = _schedule(sends, existing)
    except _AskError as asking:
        return _asked(context, work, asking.question, frontier)
    timezone, source = context.zone
    document["timezone"] = timezone
    actions = [(step["assistant"], step["action"]) for step in document["steps"]]
    changing = [call.action for call in latest if not call.read_only]
    permitted = _permitted(actions + changing + list(recording.decide_actions), contracts)
    return Recorded(document, origins, permitted, when, timezone, source)


# The questions only work run again can answer, which freeze that work as a manifest.
_RERUN_CODES = frozenset({"routine-work-split", "routine-work-rerun", "routine-binding-unsourced"})


def _asked(context: _Context, work: list[_Call], question: Question, frontier: int) -> Question:
    """A question as Team keeps it: with the frontier, the work it asks to repeat, and a choice already answered.

    A choice the person already made, exactly, stays bound while Team asks something else.
    """
    pending = question.pending
    if pending is None and context.asked is not None and context.asked.pending is not None:
        kept = context.asked.pending
        chosen = kept.chosen if kept.chosen is not None else _selection(context, kept)
        pending = None if chosen is None else dataclasses.replace(kept, chosen=chosen)
    manifest = question.manifest
    if manifest is None and question.code in _RERUN_CODES:
        manifest = _manifest(context, work, pending)
    return dataclasses.replace(question, pending=pending, manifest=manifest, frontier=frontier)


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


def _zone(sends: Sequence[Send]) -> tuple[str, str]:
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
    timezone, source = context.zone
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
    """Ask again until the work, run again for the person's chosen target, sends exactly that target.

    Every occurrence of the chosen input that sends one of the choice's targets must send the chosen one, and at least
    one must; an occurrence that sends another value was resolved on its own and is left as it is.
    """
    pending = None if context.asked is None else context.asked.pending
    if pending is None or pending.chosen is None:
        return
    sent = [_json_text(value) for value in _targeted(work, pending)]
    if _json_text(pending.chosen) not in sent or any(text != _json_text(pending.chosen) for text in sent):
        raise _AskError(Question("routine-work-rerun", pending=pending))


def _targeted(calls: Sequence[_Call], pending: Pending) -> list[object]:
    """What each call of the choice's input sent, among the choice's targets."""
    targets = {_json_text(target) for target, _label in pending.targets}
    values = [
        call.occurrence.input.value.get(pending.member)
        for call in calls
        if call.action == pending.action and isinstance(call.occurrence.input.value, dict)
    ]
    return [value for value in values if _json_text(value) in targets]


def _split(context: _Context, work: list[_Call], nodes: dict[int, _Call]) -> None:
    """Ask when an earlier send ran a work Action for something the work did not run again."""
    latest = context.calls[-1].send if context.calls else None
    for call in context.calls:
        if call.send == latest or call.send < context.frontier or context.classes[call.index] in nodes:
            continue
        same = [item for item in work if item.action == call.action]
        if same and not any(_same_input(item.occurrence, call.occurrence) for item in same):
            split = [
                item
                for item in context.calls
                if item.send != latest and item.send >= context.frontier and item.action == call.action
            ]
            manifest = _manifest(context, sorted([*split, *work], key=lambda item: item.index), None)
            raise _AskError(Question("routine-work-split", manifest=manifest))


def _manifest(context: _Context, calls: Sequence[_Call], pending: Pending | None) -> Manifest:
    """The work these calls did, as a rerun must repeat it, with a chosen target in place of the one they sent."""
    if not context.classes:
        _classes(context)
    slots: list[Slot] = []
    seen: set[int] = set()
    for call in calls:
        given = call.occurrence.input
        if given.withheld or given.oversize or not isinstance(given.value, dict):
            raise RecordingError("routine-secret-literal")
        representative = context.classes[call.index]
        if representative in seen:
            continue
        seen.add(representative)
        inputs = tuple(
            _slot_input(context, call, (member, value), pending) for member, value in sorted(given.value.items())
        )
        slots.append(Slot(call.action, call.read_only, inputs))
    return Manifest(tuple(slots))


def _slot_input(
    context: _Context, call: _Call, given: tuple[str, object], pending: Pending | None
) -> tuple[str, str, object]:
    """What a rerun must send for one input member: the chosen target, a date, a fresh value, or this exact one."""
    member, value = given
    targets = () if pending is None or pending.chosen is None else pending.targets
    if (call.action, member) == ((pending.action, pending.member) if pending else None) and any(
        _json_text(value) == _json_text(target) for target, _label in targets
    ):
        return member, "value", pending.chosen
    # As the recorder classifies it: what the person named stays exactly that, before any date is the run date.
    if context.known.names(value):
        return member, "value", value
    if value == _date_at(context.sends[call.send].started_at, "UTC").isoformat():
        return member, "clock", None
    if _referable(value):
        return member, "fresh", value
    return member, "value", value


def settlement(sends: Sequence[Send], asked: Asked) -> int | None:
    """The latest send after the question whose calls satisfy its manifest, or None while none does."""
    for position in reversed(range(asked.after, len(sends))):
        if _satisfies(sends[position], asked.manifest):
            return position
    return None


def _satisfies(send: Send, manifest: Manifest) -> bool:
    """Whether one send's calls repeat the manifest exactly.

    Every slot takes its own call, in the frozen order wherever a change is involved: a changing slot comes after every
    slot before it, and a read-only slot after every changing slot before it. Fresh values map one to one onto the
    values the work sent. Every call no slot takes must be a read-only source of a fresh value the slots sent.
    """
    return _assigned(send, manifest.slots, _Assignment((), -1, -1, {}, {}))


@dataclass(frozen=True, slots=True)
class _Assignment:
    """A partial match of slots to one send's calls: the calls taken, the order bounds, and the fresh value mapping."""

    taken: tuple[int, ...]
    last_change: int
    last_any: int
    forward: dict[str, str]
    backward: dict[str, str]


def _assigned(send: Send, slots: Sequence[Slot], state: _Assignment) -> bool:
    if not slots:
        return _only_sources(send, state)
    slot, rest = slots[0], slots[1:]
    floor = state.last_any if not slot.read_only else state.last_change
    for place, occurrence in enumerate(send.occurrences):
        if place <= floor or place in state.taken or occurrence.read_only != slot.read_only:
            continue
        mapping = _fills(send, occurrence, slot, (state.forward, state.backward))
        if mapping is None:
            continue
        taken = (*state.taken, place)
        last_change = place if not slot.read_only else state.last_change
        following = _Assignment(taken, last_change, max(state.last_any, place), *mapping)
        if _assigned(send, rest, following):
            return True
    return False


def _only_sources(send: Send, state: _Assignment) -> bool:
    """Whether every call the slots did not take is a read-only call returning a fresh value they sent."""
    sent = [json.loads(text) for text in state.backward]
    return all(
        occurrence.read_only and any(_returned(occurrence, value) for value in sent)
        for place, occurrence in enumerate(send.occurrences)
        if place not in state.taken
    )


def _fills(
    send: Send, occurrence: trace.Occurrence, slot: Slot, mapping: tuple[dict[str, str], dict[str, str]]
) -> tuple[dict[str, str], dict[str, str]] | None:
    """The fresh value mapping once one call fills a slot, or None when it does not fill it.

    It fills it with the same Action and every input member as the slot requires; a fresh value maps one to one onto
    the value the work sent there.
    """
    given = occurrence.input
    if (occurrence.assistant, occurrence.action) != slot.action or given.withheld or not isinstance(given.value, dict):
        return None
    if set(given.value) != {member for member, _kind, _value in slot.inputs}:
        return None
    forward, backward = dict(mapping[0]), dict(mapping[1])
    today = _date_at(send.started_at, "UTC").isoformat()
    for member, kind, value in slot.inputs:
        sent = given.value[member]
        if (kind == "value" and not routine_plan.same(sent, value)) or (kind == "clock" and sent != today):
            return None
        if kind == "fresh":
            before, after = _json_text(value), _json_text(sent)
            if forward.setdefault(before, after) != after or backward.setdefault(after, before) != before:
                return None
            if not any(item is not occurrence and _returned(item, sent) for item in send.occurrences):
                return None
    return forward, backward


def _returned(occurrence: trace.Occurrence, value: object) -> bool:
    """Whether a call returned the value at a position it kept."""
    result = occurrence.result
    return any(result.available(_pointer(position[0])) for position in _positions(result.value, value))


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
    pending = _pending_for(context, input_)
    if _person_named(context, pending, value):
        return {"kind": "literal", "value": value}, "request"
    clock = _clock(context, call, value)
    if clock is not None:
        return clock
    if not _referable(value):
        return {"kind": "literal", "value": value}, "assistant"
    return _bound(context, input_, value, pending)


def _person_named(context: _Context, pending: Pending | None, value: object) -> bool:
    """Whether the person named the value: the target they chose, or, never for a target, a value their text holds.

    A target of the pending choice is confirmed only by the person's exact answer, never by a substring.
    """
    text = _json_text(value)
    if pending is not None and any(text == _json_text(target) for target, _label in pending.targets):
        return pending.chosen is not None and text == _json_text(pending.chosen)
    return context.known.names(value)


def _bound(
    context: _Context, input_: tuple[_Call, str], value: object, pending: Pending | None
) -> tuple[dict[str, object], str]:
    """A referable value's source, the person's answer when it is the pending choice again, or the assistant's own."""
    try:
        source = _sourced(context, input_, value)
    except _AskError as asking:
        if pending is None or pending.chosen is not None or not _same_choice(asking.question, pending):
            raise
        return _answered(context, pending, value)
    if source is None:
        return {"kind": "literal", "value": value}, "assistant"
    _unexposed(source, context.known)
    return source, "selector" if "where" in source else "step"


def _clock(context: _Context, call: _Call, value: object) -> tuple[dict[str, object], str] | None:
    """The run date, when the value is the UTC date its own send started on; None when it is not that date."""
    started = context.sends[call.send].started_at
    if not isinstance(value, str) or value != _date_at(started, "UTC").isoformat():
        return None
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


def _pending_for(context: _Context, input_: tuple[_Call, str]) -> Pending | None:
    """The pending target choice about this Action input, or None."""
    call, member = input_
    pending = None if context.asked is None else context.asked.pending
    if pending is None or (call.action, member) != (pending.action, pending.member):
        return None
    return pending


def _same_choice(question: Question, pending: Pending) -> bool:
    """Whether a target question is the pending one again: the same input, offering the same targets."""
    asked = question.pending
    return asked is not None and (asked.action, asked.member, asked.targets) == (
        pending.action,
        pending.member,
        pending.targets,
    )


def _answered(context: _Context, pending: Pending, value: object) -> tuple[dict[str, object], str]:
    """The person's answer to the pending choice for an input it binds: the target they chose exactly, if any.

    The chosen target is a literal they named; choosing another target than the one this input sent asks for the work
    again, and no exact answer asks the same question again.
    """
    chosen = _selection(context, pending)
    if chosen is None:
        raise _AskError(Question("routine-binding-ambiguous", _shown(pending), pending=pending))
    if _json_text(chosen) != _json_text(value):
        raise _AskError(Question("routine-work-rerun", pending=dataclasses.replace(pending, chosen=chosen)))
    return {"kind": "literal", "value": value}, "request"


def _selection(context: _Context, pending: Pending) -> object:
    """The target whose exact JSON text the person's latest answer is, or None; "123" and 123 never match each other."""
    after = context.asked.after
    for segment in reversed([segment for send in context.sends[after:] for segment in send.person]):
        answer = segment.strip()
        chosen = [target for target, _label in pending.targets if answer == _json_text(target)]
        if chosen:
            return chosen[0]
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
        raise _ambiguous(context, input_, value, [{"value": value, "label": None}])
    ((node, positions),) = found.items()
    bindings = [_binding(context, node, position) for position in positions]
    if len(bindings) == 1 and "options" not in bindings[0]:
        return bindings[0]
    selected = [item for item in bindings if "where" in item]
    if len(selected) == 1:
        return selected[0]
    options = [option for item in bindings for option in item.get("options") or [{"value": value, "label": None}]]
    raise _ambiguous(context, input_, value, options)


def _ambiguous(
    context: _Context, input_: tuple[_Call, str], value: object, options: list[dict[str, object]]
) -> Exception:
    """The question which target an input means: a scalar's choices; a container is never chosen and refuses.

    A target the span protects is never offered, and a protected label is never shown.
    """
    if isinstance(value, dict | list):
        return RecordingError("routine-recording-ambiguous")
    protected = context.known.protected
    choices: list[dict[str, object]] = []
    for option in options:
        if trace.exposes(_json_text(option["value"]), protected):
            continue
        if trace.exposes(option["label"], protected):
            option = {**option, "label": None}
        if all(option["value"] != item["value"] for item in choices) and _offered(option):
            choices.append(option)
    call, member = input_
    pending = Pending(call.action, member, tuple((item["value"], item["label"]) for item in choices))
    return _AskError(Question("routine-binding-ambiguous", _shown(pending), pending=pending))


def _shown(pending: Pending) -> tuple[dict[str, object], ...]:
    """The targets a question shows: every one, or none when there are more than it may show."""
    if len(pending.targets) > http_routine.MAX_QUESTION_OPTIONS:
        return ()
    return tuple({"value": value, "label": label} for value, label in pending.targets)


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
