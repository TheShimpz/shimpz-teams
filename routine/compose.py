"""Composing a confirmable Routine plan from a recording span, without a model (ADR-0101)."""

import dataclasses
import heapq
from collections.abc import Mapping, Sequence

from protocol.http.v1 import routine as http_routine
from routine import plan as routine_plan
from routine import provenance as routine_provenance
from routine import recording as routine_recording
from routine import rerun as routine_rerun
from routine import trace


def record(
    sends: Sequence[routine_recording.Send],
    protection: trace.Protection,
    contracts: Mapping[tuple[str, str], routine_plan.ActionContract],
    *,
    work: int | None,
    asked: routine_recording.Asked | None = None,
    existing: routine_recording.Existing | None = None,
) -> routine_recording.Recorded | routine_recording.Question:
    """The plan a recording span defines, or the question to ask first; raises RecordingError when it cannot be one.

    The work is the calls of the send at position ``work``, the one its record intent selected; with none, a
    replacement keeps its steps. A span gets one repair: once it asked for work to run
    again and a later send ran Actions, work that needs another repair refuses ``routine-record-again`` instead of
    asking again, whatever was asked between. No card ever records work that calls an Action a person's choice is
    about without sending the target they chose.
    """
    if protection.lost:
        raise routine_recording.RecordingError("routine-recording-unavailable")
    # The person's output, or show while the work is planned and its own questions come first; unstated, it is asked.
    stated = _output(sends, existing)
    mode = stated or "show"
    calls = routine_recording.span_calls(sends, contracts)
    texts = [line for send in sends for line in (*routine_recording.segment_lines(send.person), *send.window)]
    latest = [call for call in calls if call.send == work]
    context = routine_recording.Context(
        sends, calls, routine_recording.known_texts(texts, protection), routine_recording.zone(sends), asked, contracts
    )
    work = routine_provenance.narrowed(context, latest)
    context.work = tuple(call.index for call in work)
    # The send count when the span first asked for a repair, which it keeps across later questions until a card.
    repair_from = asked.repair_from if asked is not None else None
    repaired = repair_from is not None and any(send.occurrences for send in sends[repair_from:])
    try:
        if not latest and existing is not None:
            kept = _kept(context, mode, existing)
        else:
            kept = None
            document, origins = _plan(context, mode, work)
            when = _schedule(sends, existing)
        if stated is None:
            raise routine_recording.AskError(routine_recording.Question("routine-output-unstated"))
    except routine_recording.AskError as asking:
        repairing = asking.question.code == routine_recording.RERUN
        if repaired and repairing:
            raise routine_recording.RecordingError("routine-record-again") from asking
        if repairing and repair_from is None:
            repair_from = len(sends)
        return dataclasses.replace(_asked(context, work, asking.question), repair_from=repair_from)
    if kept is None and not _chosen_used(context, work):
        raise routine_recording.RecordingError("routine-record-again")
    if kept is None:
        permitted = _permitted([(step["assistant"], step["action"]) for step in document["steps"]], contracts)
        kept = routine_recording.Recorded(document, origins, permitted, when, context.zone[1])
    # A question Team still asks about the plan, such as its interval over the budget, keeps what the span settled.
    return dataclasses.replace(kept, chosen=_settled(context), repair_from=repair_from)


def _chosen_used(context: routine_recording.Context, work: list[routine_recording.Call]) -> bool:
    """Whether the work sends every target the person chose wherever it calls the Action that choice is about."""
    for binding in routine_provenance.target_choices(context):
        if binding.chosen is None:
            continue
        chosen = routine_recording.json_text(binding.chosen)
        calls = [call for call in work if call.action == binding.action]
        if calls and not any(routine_provenance.sent_value(call, binding.member) == chosen for call in calls):
            return False
    return True


def _asked(
    context: routine_recording.Context,
    work: list[routine_recording.Call],
    question: routine_recording.Question,
) -> routine_recording.Question:
    """A question as Team keeps it: with the work it asks to repeat, and every choice already answered.

    A choice the person answered exactly stays bound, whatever Team asks next.
    """
    pending, now = question.pending, ()
    if pending is not None and pending.chosen is not None:
        pending, now = None, (pending,)
    chosen = _settled(context, *now)
    manifest = question.manifest
    if manifest is None and question.code == routine_recording.RERUN:
        manifest = routine_rerun.manifest(context, work, chosen)
    return dataclasses.replace(question, pending=pending, manifest=manifest, chosen=chosen)


def _settled(
    context: routine_recording.Context, *chosen: routine_recording.Pending
) -> tuple[routine_recording.Pending, ...]:
    """Every target choice the person answered exactly, and any chosen now, the latest one for each choice."""
    answered = [item for item in routine_provenance.target_choices(context) if item.chosen is not None]
    kept = {(item.action, item.member, item.targets): item for item in (*answered, *chosen)}
    return tuple(kept.values())


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


def _schedule(
    sends: Sequence[routine_recording.Send], existing: routine_recording.Existing | None
) -> dict[str, object]:
    """The schedule the latest authored segment stating one states; a replacement keeps its own when none is stated.

    Every reading within one segment counts, so different schedules in one text are asked about.
    """
    latest = routine_recording.stated_schedules(sends)
    if len(latest) == 1:
        return dict(latest[0])
    if not latest and existing is not None:
        return dict(existing.schedule)
    raise routine_recording.AskError(routine_recording.Question("routine-schedule-unstated"))


def _output(sends: Sequence[routine_recording.Send], existing: routine_recording.Existing | None) -> str | None:
    """The output choice the person states; a replacement keeps its mode when none is.

    None when no segment states one, or the latest that does states two: the person is then asked.
    """
    latest = routine_recording.stated_outputs(sends)
    if len(latest) == 1:
        return latest[0]
    if not latest and existing is not None:
        return existing.plan["output"]["mode"]
    return None


# --- What it runs ----------------------------------------------------------------------------------------------------


def _kept(
    context: routine_recording.Context, mode: str, existing: routine_recording.Existing
) -> routine_recording.Recorded:
    """A replacement that ran no Action: the replaced plan's steps exactly, on a new schedule, zone, or output.

    It is admitted again against the Team's exact current contracts, so a pin that drifted refuses.
    """
    steps = [dict(step) for step in existing.plan["steps"]]
    origins = {
        step["id"]: {member: _kept_origin(source, context.known) for member, source in step["input"].items()}
        for step in steps
    }
    document = _document(steps, steps[-1]["id"], mode, context.zone[0])
    _admitted(document, context.contracts)
    when = _schedule(context.sends, existing)
    actions = [(step["assistant"], step["action"]) for step in steps]
    return routine_recording.Recorded(document, origins, _permitted(actions, context.contracts), when, context.zone[1])


def _document(steps: list[dict[str, object]], last: str | None, mode: str, timezone: str) -> dict[str, object]:
    """The plan document of these steps, in the zone each run reads its dates in."""
    return {
        "version": routine_plan.VERSION,
        "timezone": timezone,
        "steps": steps,
        "output": {"mode": mode, "step": last if mode in http_routine.SHOWN_MODES else None},
    }


def _admitted(
    document: dict[str, object], contracts: Mapping[tuple[str, str], routine_plan.ActionContract]
) -> routine_plan.Plan:
    """The plan as every run admits it, against the exact current contracts; a refusal keeps the plan's own code."""
    try:
        return routine_plan.admit(document, contracts)
    except routine_plan.PlanError as exc:
        raise routine_recording.RecordingError(exc.code) from exc


def _kept_origin(source: Mapping[str, object], known: routine_recording.Known) -> str:
    """How the card names a kept input's origin, as a recording would now."""
    if source["kind"] == "literal":
        return "request" if known.names(source["value"]) else "assistant"
    if source["kind"] == "run_clock":
        return "clock"
    return "selector" if "where" in source else "step"


def _plan(
    context: routine_recording.Context, mode: str, work: list[routine_recording.Call]
) -> tuple[dict[str, object], dict]:
    """The plan document and each step's input origins, from the work and every source it needs."""
    if not work:
        raise routine_recording.RecordingError("routine-recording-empty")
    _classes(context)
    nodes = _closure(context, work)
    order = _ordered(context, nodes)
    names = {node: f"s{position}" for position, node in enumerate(order, start=1)}
    steps = [_step(context, nodes[node], names) for node in order]
    origins = {names[node]: context.inputs[node][1] for node in order}
    work_nodes = {context.classes[call.index] for call in work}
    shown = next((names[node] for node in reversed(order) if node in work_nodes), None)
    document = _document(steps, shown, mode, context.zone[0])
    _verify(context, nodes, document, names)
    return document, origins


def _closure(
    context: routine_recording.Context, work: list[routine_recording.Call]
) -> dict[int, routine_recording.Call]:
    """The plan calls: each work call's source representative, and every source their inputs read, transitively."""
    nodes: dict[int, routine_recording.Call] = {}
    pending = [context.classes[call.index] for call in work]
    while pending:
        node = pending.pop()
        if node in nodes:
            continue
        nodes[node] = context.calls[node]
        routine_provenance.classify_call(context, nodes[node])
        inputs, _origins = context.inputs[node]
        pending.extend(source["node"] for source in inputs.values() if "node" in source)
    return nodes


def _step(context: routine_recording.Context, call: routine_recording.Call, names: dict[int, str]) -> dict[str, object]:
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


def _classes(context: routine_recording.Context) -> None:
    """Each call's source: its earliest read-only twin (same Action, input, and result) with no change between."""
    open_sources: list[routine_recording.Call] = []
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


def _edges(context: routine_recording.Context, nodes: dict[int, routine_recording.Call]) -> dict[int, set[int]]:
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


def _ordered(context: routine_recording.Context, nodes: dict[int, routine_recording.Call]) -> list[int]:
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
        raise routine_recording.RecordingError("routine-recording-conflict" if changed else "routine-recording-cyclic")
    return order


# --- Verification ----------------------------------------------------------------------------------------------------


def _verify(
    context: routine_recording.Context,
    nodes: dict[int, routine_recording.Call],
    document: dict[str, object],
    names: dict[int, str],
) -> None:
    """Every call a step stands for, replayed through a run's own admission and resolver, sends exactly what it sent.

    The plan is admitted against the exact contracts, each step's selections from its recorded result are kept as a
    run keeps them and within their bound, and each call's input is resolved as a run resolves it on that call's own
    start, so a card never shows a plan that fails every run, such as an input past the bound a run dispatches.
    """
    plan = _admitted(document, context.contracts)
    recorded = {names[node]: call.occurrence.result.value for node, call in nodes.items()}
    steps = {step.step_id: step for step in plan.steps}
    selected: dict[routine_plan.Key, object] = {}
    try:
        for step in plan.steps:
            chosen = routine_plan.selections(plan, step.step_id, recorded[step.step_id])
            selected.update({(step.step_id, *key): value for key, value in chosen.items()})
        if not routine_plan.retained_within(selected):
            raise routine_recording.RecordingError("routine-recording-too-large")
        for call in context.calls:
            node = context.classes[call.index]
            if node not in nodes:
                continue
            started = context.sends[call.send].started_at
            resolved = routine_plan.resolve(plan, steps[names[node]], selected, started, lambda value: value)
            if not routine_plan.same(resolved, call.occurrence.input.value):
                raise routine_recording.RecordingError("routine-recording-unverified")
    except routine_plan.PlanError as exc:
        code = "routine-recording-too-large" if exc.code == "plan-input-too-large" else "routine-recording-unverified"
        raise routine_recording.RecordingError(code) from exc
