"""The work a Routine question asks the Brain to run again, as Team names it, without a model (ADR-0101)."""

from collections.abc import Sequence

from routine import provenance as routine_provenance
from routine import recording as routine_recording


def manifest(
    context: routine_recording.Context,
    calls: Sequence[routine_recording.Call],
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
            if kind == "fresh" and (holders := routine_provenance.holders(context, call, value))
        )
        slots.append(routine_recording.Slot(call.action, call.read_only, inputs, sources))
    return routine_recording.Manifest(tuple(slots))


def _slot_input(
    context: routine_recording.Context,
    call: routine_recording.Call,
    given: tuple[str, object],
    chosen: Sequence[routine_recording.Pending],
) -> tuple[str, str, object]:
    """What a rerun must send for one input member: the chosen target, a date, a fresh value, or this exact one.

    It names each value by the recorder's own classifier (``provenance.origin``): a value the recorder copies from a
    result, or asks about as an identifier no result holds, needs fresh provenance; the run date stays the run date;
    anything else, a dated text included, is kept exactly as the recorder keeps it.
    """
    member, value = given
    binding = routine_provenance.binding_for(chosen, (call, member), value)
    if binding is not None:
        return member, "value", binding.chosen
    kind = routine_provenance.origin(context, (call, member), value)
    if kind == "clock":
        return member, "clock", None
    if kind in ("sourced", "unsourced"):
        return member, "fresh", value
    return member, "value", value
