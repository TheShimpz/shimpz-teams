"""The work a Routine question freezes for a re-run, and whether a later send settles it, without a model (ADR-0101)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from routine import plan as routine_plan
from routine import provenance as routine_provenance
from routine import recording as routine_recording
from routine import trace


def _split(
    context: routine_recording._Context, work: list[routine_recording._Call], nodes: dict[int, routine_recording._Call]
) -> None:
    """Ask when an earlier send ran a work Action for something the work did not run again.

    A call for a target the person did not choose, once they answered that choice, is never split evidence.
    """
    latest = context.calls[-1].send if context.calls else None
    for call in context.calls:
        if call.send == latest or not _evidence(context, call) or context.classes[call.index] in nodes:
            continue
        same = [item for item in work if item.action == call.action]
        if same and not any(_same_input(item.occurrence, call.occurrence) for item in same):
            split = [
                item
                for item in context.calls
                if item.send != latest and _evidence(context, item) and item.action == call.action
            ]
            manifest = _manifest(context, sorted([*split, *work], key=lambda item: item.index), ())
            raise routine_recording._AskError(routine_recording.Question("routine-work-split", manifest=manifest))


def _evidence(context: routine_recording._Context, call: routine_recording._Call) -> bool:
    """Whether an earlier call can count as split work: not before the frontier, nor for an unchosen target."""
    if call.send < context.frontier:
        return False
    given = call.occurrence.input.value
    for member, value in given.items() if isinstance(given, dict) else ():
        binding = routine_provenance._binding_for(routine_provenance._bindings(context), (call, member), value)
        if (
            binding is not None
            and binding.chosen is not None
            and routine_recording._json_text(value) != routine_recording._json_text(binding.chosen)
        ):
            return False
    return True


def _manifest(
    context: routine_recording._Context,
    calls: Sequence[routine_recording._Call],
    chosen: Sequence[routine_recording.Pending],
) -> routine_recording.Manifest:
    """The work these calls did, as a rerun must repeat it, with a chosen target in place of the one they sent."""
    slots: list[routine_recording.Slot] = []
    seen: set[int] = set()
    for call in calls:
        given = call.occurrence.input
        if given.withheld or given.oversize or not isinstance(given.value, dict):
            raise routine_recording.RecordingError("routine-secret-literal")
        representative = context.classes[call.index]
        if representative in seen:
            continue
        seen.add(representative)
        inputs = tuple(
            _slot_input(context, call, (member, value), chosen) for member, value in sorted(given.value.items())
        )
        sources = tuple(
            (member, context.calls[min(holders)].action)
            for member, kind, value in inputs
            if kind == "fresh" and (holders := routine_provenance._holders(context, call, value))
        )
        slots.append(routine_recording.Slot(call.action, call.read_only, inputs, sources))
    return routine_recording.Manifest(tuple(slots))


def _slot_input(
    context: routine_recording._Context,
    call: routine_recording._Call,
    given: tuple[str, object],
    chosen: Sequence[routine_recording.Pending],
) -> tuple[str, str, object]:
    """What a rerun must send for one input member: the chosen target, a date, a fresh value, or this exact one."""
    member, value = given
    binding = routine_provenance._binding_for(chosen, (call, member), value)
    if binding is not None:
        return member, "value", binding.chosen
    # As the recorder classifies it: what the person named stays exactly that, before any date is the run date.
    if context.known.names(value):
        return member, "value", value
    clock = routine_provenance._clock(context, call, value)
    if clock is not None:
        # The recorder's own decision: the run date, or near local midnight a fixed literal.
        return (member, "clock", None) if clock[1] == "clock" else (member, "value", value)
    # A value the recorder copies from a result, or asks about as an unsourced identifier, needs fresh provenance;
    # free text or a container no result holds stays the assistant's own literal, as the recorder keeps it.
    if routine_provenance._referable(value) and (
        routine_provenance._identifier(value) or routine_provenance._holders(context, call, value)
    ):
        return member, "fresh", value
    return member, "value", value


def settlement(sends: Sequence[routine_recording.Send], asked: routine_recording.Asked) -> int | None:
    """The latest send after the question whose calls satisfy its manifest, or None while none does."""
    for position in reversed(range(asked.after, len(sends))):
        if _satisfies(sends[position], asked.manifest):
            return position
    return None


def _satisfies(send: routine_recording.Send, manifest: routine_recording.Manifest) -> bool:
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


def _assigned(send: routine_recording.Send, slots: Sequence[routine_recording.Slot], state: _Assignment) -> bool:
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


def _only_sources(send: routine_recording.Send, state: _Assignment) -> bool:
    """Whether every call the slots did not take is a read-only call returning a fresh value they sent."""
    sent = [json.loads(text) for text in state.backward]
    return all(
        occurrence.read_only and any(_returned(occurrence, value) for value in sent)
        for place, occurrence in enumerate(send.occurrences)
        if place not in state.taken
    )


def _fills(
    send: routine_recording.Send,
    occurrence: trace.Occurrence,
    slot: routine_recording.Slot,
    mapping: tuple[dict[str, str], dict[str, str]],
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
    today = routine_provenance._date_at(send.started_at, "UTC").isoformat()
    for member, kind, value in slot.inputs:
        sent = given.value[member]
        if (kind == "value" and not routine_plan.same(sent, value)) or (kind == "clock" and sent != today):
            return None
        if kind == "fresh":
            before, after = routine_recording._json_text(value), routine_recording._json_text(sent)
            if forward.setdefault(before, after) != after or backward.setdefault(after, before) != before:
                return None
            if not any(item is not occurrence and _returned(item, sent) for item in send.occurrences):
                return None
    return forward, backward


def _returned(occurrence: trace.Occurrence, value: object) -> bool:
    """Whether a call returned the value at a position it kept."""
    result = occurrence.result
    return any(
        result.available(routine_provenance._pointer(position[0]))
        for position in routine_provenance._positions(result.value, value)
    )


def _same_input(left: trace.Occurrence, right: trace.Occurrence) -> bool:
    return (
        not left.input.withheld and not right.input.withheld and routine_plan.same(left.input.value, right.input.value)
    )
