"""Team's deterministic recording of a Routine plan from a chat turn's own trace, with no model (ADR-0101).

The chat agent runs the recurring work once and then calls ``record``; Team builds the plan from its own trace of that
turn's successful Action calls. Which calls replay is fixed: in ``show``, ``changes``, and ``none`` every call replays,
so a change repeats on every run; in ``decide`` only read-only calls replay, and every changing call's Action joins the
permitted set instead, so no conditional effect ever becomes unconditional.

Each top-level input member's whole value of each replay step is classified by the first rule that applies, against
the turn's Team-admitted message (the known text):

1. a secret (withheld, credential-shaped, protected by the turn, or bound for a secret destination) refuses;
2. a non-empty string occurring in the known text, or a number whose JSON text is a whole token of it, is a literal the
   person named;
3. the UTC date Brain pinned for the turn is the run date, when the turn did not start near local midnight; otherwise
   it is a fixed literal;
4. a long string, a long integer, or a non-empty container that an earlier read-only replay step returned at exactly
   one available position is copied from it, crossing at most one array by the item's single sibling member that the
   known text names and no other item shares;
5. anything else is a literal the assistant chose, the same on every run.

Equality is exact and type-sensitive (``plan.same``). A read-only step nothing later reads is pruned, except the last.
The result is a plan document, each input's origin for the confirmation card, and the permitted Actions at their pins.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from routine import plan as routine_plan
from routine import schedule, trace

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
    """What the ``record`` call chose: the output mode, when a decision runs, the timezone, and extra Actions."""

    mode: str
    when: str | None
    timezone: str
    decide_actions: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Recorded:
    """The recorded plan document, each input's origin by step and member, and the permitted Actions at their pins."""

    document: dict[str, object]
    origins: dict[str, dict[str, str]]
    permitted: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class _Known:
    text: str
    numbers: frozenset[str]
    protected: frozenset[str]

    def names(self, value: object) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, str):
            return bool(value) and value in self.text
        return isinstance(value, int | float) and routine_plan.canonical(value).decode() in self.numbers


def record(
    recorded: trace.Trace,
    recording: Recording,
    known: str,
    protection: trace.Protection,
    contracts: Mapping[tuple[str, str], routine_plan.ActionContract],
) -> Recorded:
    """The plan a recording turn's trace defines; raises RecordingError with its stable reason."""
    _admit(recording, contracts)
    if protection.lost:
        raise RecordingError("routine-recording-unavailable")
    for occurrence in recorded.occurrences:
        contract = contracts.get((occurrence.assistant, occurrence.action))
        if contract is None or contract.pin != occurrence.pin:
            raise RecordingError("plan-pin-drift")
    decide = recording.mode == "decide"
    replayed = [item for item in recorded.occurrences if item.read_only or not decide]
    if not replayed and not decide:
        raise RecordingError("routine-recording-empty")
    context = _Known(known, frozenset(match.group() for match in _NUMBER_RE.finditer(known)), protection.values)
    steps = [
        _step(index, occurrence, replayed[:index], recorded, recording.timezone, context, contracts)
        for index, occurrence in enumerate(replayed)
    ]
    if not decide:
        steps = _pruned(steps, replayed)
    steps, origins = _renumbered(steps)
    changing = [item for item in recorded.occurrences if not item.read_only]
    permitted = _permitted(
        [(step["assistant"], step["action"]) for step, _origin in steps]
        + [(item.assistant, item.action) for item in changing]
        + list(recording.decide_actions),
        contracts,
    )
    shown = steps[-1][0]["id"] if recording.mode in routine_plan.SHOWN_MODES else None
    document = {
        "version": routine_plan.RECORDED_VERSION,
        "timezone": recording.timezone,
        "steps": [step for step, _origin in steps],
        "output": {"mode": recording.mode, "step": shown, "when": recording.when},
    }
    return Recorded(document, origins, permitted)


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
        }
        for assistant, action in sorted(set(actions))
    )


def _step(
    index: int,
    occurrence: trace.Occurrence,
    earlier: list[trace.Occurrence],
    recorded: trace.Trace,
    timezone: str,
    known: _Known,
    contracts: Mapping[tuple[str, str], routine_plan.ActionContract],
) -> tuple[dict[str, object], dict[str, str]]:
    """One replay step, provisionally named by its replay position, and the origin of each of its inputs."""
    given = occurrence.input
    if given.oversize:
        raise RecordingError("routine-recording-too-large")
    schema = contracts[(occurrence.assistant, occurrence.action)].input_schema
    if given.withheld or not isinstance(given.value, dict) or trace.exposes(given.value, known.protected):
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
        inputs[member], origins[member] = _classified(value, earlier, recorded, timezone, known)
    step = {
        "id": f"s{index + 1}",
        "assistant": occurrence.assistant,
        "action": occurrence.action,
        "pin": occurrence.pin,
        "input": inputs,
    }
    return step, origins


def _classified(
    value: object, earlier: list[trace.Occurrence], recorded: trace.Trace, timezone: str, known: _Known
) -> tuple[dict[str, object], str]:
    if known.names(value):
        return {"kind": "literal", "value": value}, "request"
    if isinstance(value, str) and value == recorded.turn_date:
        if _date_at(recorded.started_at, "UTC") == _date_at(recorded.started_at, timezone):
            return {"kind": "run_clock", "format": "date"}, "clock"
        return {"kind": "literal", "value": value}, "assistant"
    copied = _copied(value, earlier, known) if _referable(value) else None
    if copied is not None:
        _unexposed(copied, known)
        return copied, "selector" if "where" in copied else "step"
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


def _copied(value: object, earlier: list[trace.Occurrence], known: _Known) -> dict[str, object] | None:
    """The source copying ``value`` from the one available position an earlier replay step returned it at, or None."""
    found = [
        (index, position)
        for index, occurrence in enumerate(earlier)
        for position in _positions(occurrence.result.value, value)
        if occurrence.result.available(_pointer(position[0]))
    ]
    if len(found) != 1 or not earlier[found[0][0]].read_only:
        return None
    index, (tokens, arrays) = found[0]
    crossed = [place for place, is_array in enumerate(arrays) if is_array]
    step = f"s{index + 1}"
    if not crossed:
        return {"kind": "step_output", "step": step, "pointer": _pointer(tokens)}
    if len(crossed) > 1:
        return None
    return _selected(earlier[index].result, tokens, crossed[0], step, known)


def _selected(
    result: trace.Kept, tokens: tuple[str, ...], place: int, step: str, known: _Known
) -> dict[str, object] | None:
    """A selector through one array: the item's single member the known text names and no other item may share."""
    array_tokens, rest = tokens[:place], tokens[place + 1 :]
    items = routine_plan.select(result.value, _pointer(array_tokens))
    chosen = int(tokens[place])
    item = items[chosen]
    if not isinstance(item, dict):
        return None
    candidates = [
        (key, constant)
        for key, constant in item.items()
        if (not rest or key != rest[0])
        and _selectable(constant, known)
        and _unique(result, array_tokens, items, chosen, key, constant)
    ]
    if len(candidates) != 1:
        return None
    return {
        "kind": "step_output",
        "step": step,
        "pointer": _pointer(array_tokens),
        "where": dict(candidates),
        "item": _pointer(rest),
    }


def _unexposed(source: dict[str, object], known: _Known) -> None:
    """Refuse a reference whose path or selector holds a value the turn protects, raw or as its escaped pointer."""
    pointers = [source["pointer"], source.get("item", "")]
    tokens = [token for pointer in pointers for token in routine_plan.pointer_tokens(pointer)]
    if trace.exposes([*pointers, *tokens, source.get("where", {})], known.protected):
        raise RecordingError("routine-secret-literal")


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


def _pruned(
    steps: list[tuple[dict[str, object], dict[str, str]]], replayed: list[trace.Occurrence]
) -> list[tuple[dict[str, object], dict[str, str]]]:
    """Every step except a read-only one, not the last, that no kept later step reads."""
    kept: list[tuple[dict[str, object], dict[str, str]]] = []
    read: set[str] = set()
    for (step, origins), occurrence in reversed(list(zip(steps, replayed, strict=True))):
        if kept and occurrence.read_only and step["id"] not in read:
            continue
        kept.append((step, origins))
        read.update(source["step"] for source in step["input"].values() if source["kind"] == "step_output")
    return list(reversed(kept))


def _renumbered(
    steps: list[tuple[dict[str, object], dict[str, str]]],
) -> tuple[list[tuple[dict[str, object], dict[str, str]]], dict[str, dict[str, str]]]:
    """The kept steps named ``s1`` onward by their position, with every reference and origin following."""
    names = {step["id"]: f"s{index}" for index, (step, _origin) in enumerate(steps, start=1)}
    renamed = []
    for step, origins in steps:
        inputs = {
            member: {**source, "step": names[source["step"]]} if source["kind"] == "step_output" else source
            for member, source in step["input"].items()
        }
        renamed.append(({**step, "id": names[step["id"]], "input": inputs}, origins))
    return renamed, {step["id"]: origins for step, origins in renamed}
