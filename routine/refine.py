"""A recorded Routine's next revision built from its plan and the agent's explicit edits, without a model (ADR-0122).

The agent names each edit with the selected revision's step ids and the handles Team issued for the traced calls of
the current send (``c1`` for its first traced call, and so on): ``insert`` adds calls after a step in their traced
order, ``replace`` puts one call in a step's place, and ``remove`` drops a step. Nothing is matched by its Assistant and
Action alone. The new plan is deterministic: the base steps in their order, each kept with its own inputs, replaced, or
dropped, with every insertion after its step; then every step is numbered again (``s1``, ``s2``, ...), since a step id
names a step only within its revision, and every reference follows its step.

In this first slice a new step's every input is a literal or a run date, as the recorder classifies it from the span
(``routine.provenance``); one that would copy an earlier result, or that the recorder would ask about, is refused.
Removing or replacing a step that a kept step reads is refused too: the person records that group again. The plan keeps
its zone and output mode, its shown step being its new last one, and is admitted against the Team's exact current
contracts, so every pin is revalidated; each new step's input is resolved as a run resolves it on its call's own start
and must be exactly what that call sent.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from protocol.http.v1 import routine as http_routine
from routine import compose as routine_compose
from routine import plan as routine_plan
from routine import provenance as routine_provenance
from routine import recording as routine_recording

# A refinement that names a step or call that does not exist, or leaves no step.
INVALID = "routine-refine-invalid"
# A removed or replaced step that a kept step reads.
DEPENDENCY = "routine-refine-dependency"
# A new step input that is neither a literal nor a run date.
INPUT = "routine-refine-input"


@dataclass(frozen=True, slots=True)
class Refined:
    """The refined plan document, each step's input origins by its new id, and how it differs from its base.

    ``changes`` holds each new step's change and base position, in plan order; ``removed`` each base step that does not
    continue, with whether it was removed or replaced; ``last_changed`` whether the shown result now comes from
    another step than the base's last.
    """

    document: dict[str, object]
    origins: dict[str, dict[str, str]]
    changes: tuple[tuple[str, int | None], ...]
    removed: tuple[tuple[int, str], ...]
    last_changed: bool


@dataclass(frozen=True, slots=True)
class _Entry:
    """One step of the refined plan before it is numbered: what it is, its change, and where it came from."""

    step: dict[str, object]
    change: str
    base: int | None
    origins: dict[str, str]
    # The traced call a new step stands for; None for a kept step.
    call: routine_recording.Call | None = None


def refine(
    base: Mapping[str, object],
    edits: Sequence[Mapping[str, object]],
    context: routine_recording.Context,
    calls: Sequence[routine_recording.Call],
) -> Refined:
    """The plan ``edits`` make of ``base``; raises RecordingError with a stable code when it cannot be one.

    ``edits`` are canonical (no step or call twice); ``context`` covers the recording span, its zone the Routine's own;
    ``calls`` are the current send's traced calls, the first named ``c1``.
    """
    steps = list(base["steps"])
    by_step = {edit["step"]: edit for edit in edits}
    named = [_call(calls, handle) for edit in edits for handle in edit["calls"]]
    if not set(by_step) <= {step["id"] for step in steps}:
        raise routine_recording.RecordingError(INVALID)
    _independent(steps, by_step)
    context.work = tuple(call.index for call in named)
    routine_compose.source_classes(context)
    entries: list[_Entry] = []
    removed: list[tuple[int, str]] = []
    for position, step in enumerate(steps, start=1):
        edit = by_step.get(step["id"])
        kind = None if edit is None else edit["kind"]
        if kind is None or kind == "insert":
            origins = {
                member: routine_compose.kept_origin(source, context.known) for member, source in step["input"].items()
            }
            entries.append(_Entry(dict(step), "kept", position, origins))
        else:
            removed.append((position, "removed" if kind == "remove" else "replaced"))
        if kind == "replace":
            entries.append(_new(context, _call(calls, edit["calls"][0]), "replaced", position))
        if kind == "insert":
            # The inserted calls run in the order this send traced them, whatever order the edit names them in.
            traced = sorted(edit["calls"], key=lambda handle: int(handle[1:]))
            entries.extend(_new(context, _call(calls, handle), "added", None) for handle in traced)
    if not entries:
        raise routine_recording.RecordingError(INVALID)
    document = _numbered(base, entries)
    _verified(document, context, entries)
    last = entries[-1]
    return Refined(
        document,
        {f"s{place}": entry.origins for place, entry in enumerate(entries, start=1)},
        tuple((entry.change, entry.base) for entry in entries),
        tuple(removed),
        not (last.change == "kept" and last.base == len(steps)),
    )


def _call(calls: Sequence[routine_recording.Call], handle: str) -> routine_recording.Call:
    """The traced call a Team handle names: ``c{k}`` is the send's k-th traced call."""
    place = int(handle[1:])
    if place > len(calls):
        raise routine_recording.RecordingError(INVALID)
    return calls[place - 1]


def _independent(steps: list[Mapping[str, object]], by_step: Mapping[str, Mapping[str, object]]) -> None:
    """Refuse removing or replacing a step that a step kept as it is reads: that group is recorded again."""
    gone = {step for step, edit in by_step.items() if edit["kind"] in ("remove", "replace")}
    for step in steps:
        if step["id"] in gone:
            continue
        if any(source["kind"] == "step_output" and source["step"] in gone for source in step["input"].values()):
            raise routine_recording.RecordingError(DEPENDENCY)


def _new(context: routine_recording.Context, call: routine_recording.Call, change: str, base: int | None) -> _Entry:
    """A new step for one traced call, its every input a literal or a run date as the recorder classifies it."""
    try:
        routine_provenance.classify_call(context, call)
    except routine_recording.AskError as asking:
        raise routine_recording.RecordingError(INPUT) from asking
    inputs, origins = context.inputs[call.index]
    # A copied value is held by the call it is copied from (``node``), which no kept plan step stands for.
    if any(source.get("kind") not in ("literal", "run_clock") for source in inputs.values()):
        raise routine_recording.RecordingError(INPUT)
    occurrence = call.occurrence
    step = {
        "id": "",
        "assistant": occurrence.assistant,
        "action": occurrence.action,
        "pin": occurrence.pin,
        "input": {member: dict(source) for member, source in inputs.items()},
    }
    return _Entry(step, change, base, dict(origins), call)


def _numbered(base: Mapping[str, object], entries: list[_Entry]) -> dict[str, object]:
    """The plan document with every step numbered again in order and every reference following its step."""
    renamed = {entry.step["id"]: f"s{place}" for place, entry in enumerate(entries, start=1) if entry.call is None}
    steps: list[dict[str, object]] = []
    for place, entry in enumerate(entries, start=1):
        inputs = {
            member: {**source, "step": renamed[source["step"]]} if source["kind"] == "step_output" else dict(source)
            for member, source in entry.step["input"].items()
        }
        steps.append({**entry.step, "id": f"s{place}", "input": inputs})
    mode = base["output"]["mode"]
    return {
        "version": routine_plan.VERSION,
        "timezone": base["timezone"],
        "steps": steps,
        "output": {"mode": mode, "step": steps[-1]["id"] if mode in http_routine.SHOWN_MODES else None},
    }


def _verified(document: dict[str, object], context: routine_recording.Context, entries: list[_Entry]) -> None:
    """Admit the plan against the exact contracts; each new step, resolved on its call's start, sends what it sent."""
    try:
        plan = routine_plan.admit(document, context.contracts)
    except routine_plan.PlanError as exc:
        raise routine_recording.RecordingError(exc.code) from exc
    for step, entry in zip(plan.steps, entries, strict=True):
        if entry.call is None:
            continue
        started = context.sends[entry.call.send].started_at
        try:
            resolved = routine_plan.resolve(plan, step, {}, started, lambda value: value)
        except routine_plan.PlanError as exc:
            code = (
                "routine-recording-too-large" if exc.code == "plan-input-too-large" else "routine-recording-unverified"
            )
            raise routine_recording.RecordingError(code) from exc
        if not routine_plan.same(resolved, entry.call.occurrence.input.value):
            raise routine_recording.RecordingError("routine-recording-unverified")
