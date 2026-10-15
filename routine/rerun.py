"""The work a Routine question asks the Brain to run again, as Team names it, without a model (ADR-0101)."""

from collections.abc import Sequence

from routine import provenance as routine_provenance
from routine import recording as routine_recording


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
        # The recorder's own decision: the run date, or the UTC date of another local day as a fixed literal.
        return (member, "clock", None) if clock[1] == "clock" else (member, "value", value)
    # A value the recorder copies from a result, or asks about as an unsourced identifier, needs fresh provenance;
    # free text or a container no result holds stays the assistant's own literal, as the recorder keeps it.
    if routine_provenance._referable(value) and (
        routine_provenance._identifier(value) or routine_provenance._holders(context, call, value)
    ):
        return member, "fresh", value
    return member, "value", value
