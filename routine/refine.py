"""A recorded Routine's next revision built from its plan and the agent's explicit edits, without a model (ADR-0122).

The agent names each edit with the selected revision's step ids and the handles Team issued for the traced calls of
the current send (``c1`` for its first traced call, and so on): ``insert`` adds calls after a step in their traced
order, ``replace`` puts one call in a step's place, and ``remove`` drops a step. Nothing is matched by its Assistant and
Action alone. The new plan is deterministic: the base steps in their order, each kept with its own inputs, replaced, or
dropped, with every insertion after its step; then every step is numbered again (``s1``, ``s2``, ...), since a step id
names a step only within its revision, and every reference follows its step.

A new step's inputs are classified from the span exactly as a recording classifies them (``routine.provenance``): a
literal, a run date, or a value copied from another traced call of the span, kept as a name-based selection. Every
call a new step copies from joins the plan as an added step, once, in the earliest edit that needs it and before every
step that reads it, in the recorder's data-dependency order; a named call that an earlier edit would read is refused,
since its edit would place it after its reader. A new step never reads a kept base step, whose recorded result is not
in the span. An input the recorder would ask about is refused. Removing or replacing a step that a kept step reads is
refused too: the person records that group again. The plan keeps
its zone and output mode, its shown step being its new last one, and is admitted against the Team's exact current
contracts, so every pin is revalidated; each new step's input is resolved as a run resolves it on its call's own start
and must be exactly what that call sent.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from protocol.http.v1 import routine as http_routine
from routine import compose as routine_compose
from routine import plan as routine_plan
from routine import recording as routine_recording

# A refinement that names a step or call that does not exist, or leaves no step.
INVALID = "routine-refine-invalid"
# A removed or replaced step that a kept step reads.
DEPENDENCY = "routine-refine-dependency"
# A new step input the recorder would have to ask about.
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
    # The traced call a new step stands for and its source class; None for a kept step.
    call: routine_recording.Call | None = None
    node: int | None = None


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
    if not set(by_step) <= {step["id"] for step in steps}:
        raise routine_recording.RecordingError(INVALID)
    _independent(steps, by_step)
    named = {edit["step"]: [_call(calls, handle) for handle in edit["calls"]] for edit in edits}
    context.work = tuple(call.index for group in named.values() for call in group)
    routine_compose.source_classes(context)
    groups = _placed(context, steps, by_step, named)
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
        if kind in ("insert", "replace"):
            replaced = {context.classes[call.index] for call in named[step["id"]]} if kind == "replace" else set()
            entries.extend(
                _new(context, node, "replaced" if node in replaced else "added", position if node in replaced else None)
                for node in groups[step["id"]]
            )
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


def _placed(
    context: routine_recording.Context,
    steps: list[Mapping[str, object]],
    by_step: Mapping[str, Mapping[str, object]],
    named: Mapping[str, list[routine_recording.Call]],
) -> dict[str, list[int]]:
    """Each edit's new steps as source classes in their order: its named calls and every call they copy from, once.

    A copied-from call that no edit names goes to the earliest edit that needs it; a named call stays with its edit,
    which is refused when an earlier edit reads it. Within an edit the recorder's data-dependency order holds.
    """
    place = {step["id"]: index for index, step in enumerate(steps)}
    # A replacement sits where its step was; an insertion just after its step.
    anchor = {sid: (place[sid], 0 if edit["kind"] == "replace" else 1) for sid, edit in by_step.items()}
    try:
        closures = {sid: routine_compose.closure(context, calls) for sid, calls in named.items() if calls}
    except routine_recording.AskError as asking:
        raise routine_recording.RecordingError(INPUT) from asking
    owner = {context.classes[call.index]: sid for sid, calls in named.items() for call in calls}
    for sid in sorted(closures, key=anchor.__getitem__):
        for node in closures[sid]:
            owner.setdefault(node, sid)
    if any(anchor[owner[node]] > anchor[sid] for sid, nodes in closures.items() for node in nodes):
        raise routine_recording.RecordingError(INVALID)
    # One data-dependency order over every new step, which each edit keeps for its own.
    nodes = {node: context.calls[node] for node in owner}
    order = routine_compose.ordered(context, nodes)
    groups = {sid: [node for node in order if owner[node] == sid] for sid in closures}
    # The plan runs the groups in their anchors' order: every read and every change barrier must still hold there.
    placed = {
        node: rank
        for rank, node in enumerate(node for sid in sorted(groups, key=anchor.__getitem__) for node in groups[sid])
    }
    for source, targets in routine_compose.edges(context, nodes).items():
        if any(placed[source] > placed[target] for target in targets):
            raise routine_recording.RecordingError(INVALID)
    return groups


def _new(context: routine_recording.Context, node: int, change: str, base: int | None) -> _Entry:
    """A new step for one source class, its inputs as the recorder classified them and its copies naming new steps."""
    call = context.calls[node]
    inputs, origins = context.inputs[node]
    occurrence = call.occurrence
    step = {
        "id": _placeholder(node),
        "assistant": occurrence.assistant,
        "action": occurrence.action,
        "pin": occurrence.pin,
        "input": {member: routine_compose.named(source, _Placeholders()) for member, source in inputs.items()},
    }
    return _Entry(step, change, base, dict(origins), call, node)


def _placeholder(node: int) -> str:
    """A new step's id until the plan is numbered: no base step id (``s{k}``) can be one."""
    return f"n{node}"


class _Placeholders(dict):
    """Every source class's placeholder id, for naming a copy before the plan is numbered."""

    def __missing__(self, node: int) -> str:
        return _placeholder(node)


def _numbered(base: Mapping[str, object], entries: list[_Entry]) -> dict[str, object]:
    """The plan document with every step numbered again in order and every reference following its step."""
    renamed = {entry.step["id"]: f"s{place}" for place, entry in enumerate(entries, start=1)}
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
    """Admit the plan against the exact contracts and prove every new step sends what its calls sent.

    Every call a new step stands for is resolved on its own start from the new steps' recorded selections, as a
    recording's verification proves its plan.
    """
    try:
        plan = routine_plan.admit(document, context.contracts)
    except routine_plan.PlanError as exc:
        raise routine_recording.RecordingError(exc.code) from exc
    new = [(step, entry) for step, entry in zip(plan.steps, entries, strict=True) if entry.call is not None]
    selected: dict[routine_plan.Key, object] = {}
    try:
        for step, entry in new:
            chosen = routine_plan.selections(plan, step.step_id, entry.call.occurrence.result.value)
            selected.update({(step.step_id, *key): value for key, value in chosen.items()})
        if not routine_plan.retained_within(selected):
            raise routine_recording.RecordingError("routine-recording-too-large")
        for step, entry in new:
            for call in (item for item in context.calls if context.classes[item.index] == entry.node):
                started = context.sends[call.send].started_at
                resolved = routine_plan.resolve(plan, step, selected, started, lambda value: value)
                if not routine_plan.same(resolved, call.occurrence.input.value):
                    raise routine_recording.RecordingError("routine-recording-unverified")
    except routine_plan.PlanError as exc:
        code = "routine-recording-too-large" if exc.code == "plan-input-too-large" else "routine-recording-unverified"
        raise routine_recording.RecordingError(code) from exc
