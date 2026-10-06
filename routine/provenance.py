"""Where each recorded input value comes from, without a model (ADR-0101).

A value is a literal, the run date, a copy of an earlier result, or a question to the person.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Iterator, Sequence

from protocol.http.v1 import routine_proposal as http_routine_proposal
from routine import plan as routine_plan
from routine import recording as routine_recording
from routine import schedule, trace

# --- Each input ------------------------------------------------------------------------------------------------------


def _classify_call(context: routine_recording._Context, call: routine_recording._Call) -> None:
    """Classify every top-level input member of one plan call; refuses a secret."""
    given = call.occurrence.input
    if given.oversize:
        raise routine_recording.RecordingError("routine-recording-too-large")
    schema = context.contracts[call.action].input_schema
    if given.withheld or not isinstance(given.value, dict) or trace.exposes(given.value, context.known.protected):
        raise routine_recording.RecordingError("routine-secret-literal")
    try:
        secret = routine_plan.secret_literal(schema, given.value)
    except routine_plan.PlanError, RecursionError:
        secret = True
    if secret:
        raise routine_recording.RecordingError("routine-secret-literal")
    inputs: dict[str, dict[str, object]] = {}
    origins: dict[str, str] = {}
    for member, value in given.value.items():
        inputs[member], origins[member] = _classified(context, (call, member), value)
    context.inputs[call.index] = (inputs, origins)


def _classified(
    context: routine_recording._Context, input_: tuple[routine_recording._Call, str], value: object
) -> tuple[dict[str, object], str]:
    call, _member = input_
    pending = _binding_for(_bindings(context), input_, value)
    if _person_named(context, pending, value):
        return {"kind": "literal", "value": value}, "request"
    clock = _clock(context, call, value)
    if clock is not None:
        return clock
    if not _referable(value):
        return {"kind": "literal", "value": value}, "assistant"
    return _bound(context, input_, value, pending)


def _person_named(
    context: routine_recording._Context, pending: routine_recording.Pending | None, value: object
) -> bool:
    """Whether the person named the value: the target they chose, or, never for a target, a value their text holds.

    A target of the pending choice is confirmed only by the person's exact answer, never by a substring.
    """
    text = routine_recording._json_text(value)
    if pending is not None and any(text == routine_recording._json_text(target) for target, _label in pending.targets):
        return pending.chosen is not None and text == routine_recording._json_text(pending.chosen)
    return context.known.names(value)


def _bound(
    context: routine_recording._Context,
    input_: tuple[routine_recording._Call, str],
    value: object,
    pending: routine_recording.Pending | None,
) -> tuple[dict[str, object], str]:
    """A referable value's source, or the assistant's own; the question about it again until the person answers it.

    Once the person chose another of the targets of the choice this value belongs to, the work must run again with it.
    """
    try:
        source = _sourced(context, input_, value)
    except routine_recording._AskError as asking:
        if pending is not None and pending.chosen is not None and _same_choice(asking.question, pending):
            raise routine_recording._AskError(
                routine_recording.Question("routine-work-rerun", pending=pending)
            ) from asking
        raise
    if source is None:
        return {"kind": "literal", "value": value}, "assistant"
    _unexposed(source, context.known)
    return source, "selector" if "where" in source else "step"


def _clock(
    context: routine_recording._Context, call: routine_recording._Call, value: object
) -> tuple[dict[str, object], str] | None:
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
        return len(value) >= routine_recording.MIN_REF_STRING
    if type(value) is int:
        return len(str(abs(value))) >= routine_recording.MIN_REF_INTEGER_DIGITS
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


def _holders(
    context: routine_recording._Context, consumer: routine_recording._Call, value: object
) -> dict[int, list[_Position]]:
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


def _bindings(context: routine_recording._Context) -> tuple[routine_recording.Pending, ...]:
    """Every target choice the span holds, as the person's answers settle them.

    Each one already answered, and the open one, chosen as soon as a later send answers it exactly, so that answer
    decides everything at once: what the work sends, what counts as split work, and what a rerun must repeat.
    """
    asked = context.asked
    if asked is None:
        return ()
    if asked.pending is None:
        return asked.chosen
    selected = _selection(context, asked.pending)
    current = asked.pending if selected is None else dataclasses.replace(asked.pending, chosen=selected)
    return (*asked.chosen, current)


def _binding_for(
    bindings: Sequence[routine_recording.Pending], input_: tuple[routine_recording._Call, str], value: object
) -> routine_recording.Pending | None:
    """The target choice binding this Action input's value: one about this input with the value among its targets."""
    call, member = input_
    text = routine_recording._json_text(value)
    return next(
        (
            binding
            for binding in bindings
            if (call.action, member) == (binding.action, binding.member)
            and any(text == routine_recording._json_text(target) for target, _label in binding.targets)
        ),
        None,
    )


def _narrowed(
    context: routine_recording._Context, work: list[routine_recording._Call]
) -> list[routine_recording._Call]:
    """The work without its calls that sent a target the person did not choose, when one of its calls sent theirs.

    Those calls were the choice not taken, so the work keeps the chosen call and runs nothing again. A changing call
    never drops: work that changed something with another target must run again with the chosen one.
    """
    for binding in _bindings(context):
        if binding.chosen is None:
            continue
        chosen = routine_recording._json_text(binding.chosen)
        sent = {call.index: _sent(call, binding) for call in work}
        others = {index for index, text in sent.items() if text is not None and text != chosen}
        if chosen in sent.values() and all(call.read_only for call in work if call.index in others):
            work = [call for call in work if call.index not in others]
    return work


def _sent(call: routine_recording._Call, binding: routine_recording.Pending) -> str | None:
    """The JSON text of the target of ``binding`` this call sent, or None when it sent none of them."""
    given = call.occurrence.input
    if (
        call.action != binding.action
        or given.withheld
        or not isinstance(given.value, dict)
        or binding.member not in given.value
    ):
        return None
    text = routine_recording._json_text(given.value[binding.member])
    return text if any(text == routine_recording._json_text(target) for target, _label in binding.targets) else None


def _same_choice(question: routine_recording.Question, pending: routine_recording.Pending) -> bool:
    """Whether a target question is the pending one again: the same input, offering the same targets."""
    asked = question.pending
    return asked is not None and (asked.action, asked.member, asked.targets) == (
        pending.action,
        pending.member,
        pending.targets,
    )


def _selection(context: routine_recording._Context, pending: routine_recording.Pending) -> object:
    """The target whose exact JSON text the person's latest answer is, or None; "123" and 123 never match each other."""
    after = context.asked.after
    for segment in reversed([segment for send in context.sends[after:] for segment in send.person]):
        answer = segment.strip()
        chosen = [target for target, _label in pending.targets if answer == routine_recording._json_text(target)]
        if chosen:
            return chosen[0]
    return None


def _sourced(
    context: routine_recording._Context, input_: tuple[routine_recording._Call, str], value: object
) -> dict[str, object] | None:
    """The one source occurrence and position holding ``value``, or None for text nothing holds; else it asks.

    An identifier nothing returned is asked about, since freezing one would replay a remembered id; free text or a
    container the assistant wrote and nothing returned is its own choice, the same on every run.
    """
    consumer, _member = input_
    found = _holders(context, consumer, value)
    if not found:
        if not _identifier(value):
            return None
        raise routine_recording._AskError(routine_recording.Question("routine-binding-unsourced"))
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
    context: routine_recording._Context,
    input_: tuple[routine_recording._Call, str],
    value: object,
    options: list[dict[str, object]],
) -> Exception:
    """The question which target an input means: a scalar's choices; a container is never chosen and refuses.

    A target the span protects is never offered, and a protected label is never shown.
    """
    if isinstance(value, dict | list):
        return routine_recording.RecordingError("routine-recording-ambiguous")
    protected = context.known.protected
    choices: list[dict[str, object]] = []
    for option in options:
        if trace.exposes(routine_recording._json_text(option["value"]), protected):
            continue
        if trace.exposes(option["label"], protected):
            option = {**option, "label": None}
        if all(option["value"] != item["value"] for item in choices) and _offered(option):
            choices.append(option)
    call, member = input_
    pending = routine_recording.Pending(call.action, member, tuple((item["value"], item["label"]) for item in choices))
    return routine_recording._AskError(
        routine_recording.Question("routine-binding-ambiguous", _shown(pending), pending=pending)
    )


def _shown(pending: routine_recording.Pending) -> tuple[dict[str, object], ...]:
    """The targets a question shows: every one, or none when there are more than it may show."""
    if len(pending.targets) > http_routine_proposal.MAX_QUESTION_OPTIONS:
        return ()
    return tuple({"value": value, "label": label} for value, label in pending.targets)


def _offered(option: dict[str, object]) -> bool:
    """Whether a target can be shown as one choice of the question."""
    question = routine_recording.Question("routine-binding-ambiguous", (option,)).wire()
    return http_routine_proposal.canonical_question(question) is not None


def _binding(context: routine_recording._Context, node: int, position: _Position) -> dict[str, object]:
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
        if key in _NAME_MEMBERS
        and isinstance(value, str)
        and 0 < len(value) <= http_routine_proposal.MAX_QUESTION_OPTION_CHARS
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


def _selectable(constant: object, known: routine_recording._Known) -> bool:
    if isinstance(constant, str):
        return len(constant) >= routine_recording.MIN_SELECTOR_CHARS and known.names(constant)
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


def _unexposed(source: dict[str, object], known: routine_recording._Known) -> None:
    """Refuse a reference no plan pointer can hold, or whose path or selector holds a value the span protects."""
    pointers = [source["pointer"], source.get("item", "")]
    tokens = [routine_plan.pointer_tokens(pointer) for pointer in pointers]
    if any(item is None for item in tokens):
        raise routine_recording.RecordingError("routine-recording-too-large")
    flat = [token for item in tokens for token in item]
    if trace.exposes([*pointers, *flat, source.get("where", {})], known.protected):
        raise routine_recording.RecordingError("routine-secret-literal")
