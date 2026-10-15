"""Composing a confirmable Routine plan from a recording span, without a model (ADR-0101)."""

import dataclasses
import datetime
import heapq
from collections.abc import Mapping, Sequence

from protocol.http.v1 import phrase
from protocol.http.v1 import routine as http_routine
from routine import dates as routine_dates
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
    asked: routine_recording.Asked | None = None,
    existing: routine_recording.Existing | None = None,
) -> routine_recording.Recorded | routine_recording.Question:
    """The plan a recording span defines, or the question to ask first; raises RecordingError when it cannot be one.

    The work is the calls of the latest send that ran any Action. A span gets one repair: once it asked for work to run
    again (a rerun, or a chosen chain the work lacks) and a later send ran Actions, work that needs another repair
    refuses ``routine-record-again`` instead of asking again, whatever was asked between. No card ever records work
    that calls an Action a person's choice is about without sending the target they chose.
    """
    if protection.lost:
        raise routine_recording.RecordingError("routine-recording-unavailable")
    # The person's output, or show while the work is planned and its own questions come first; unstated, it is asked.
    stated = _output(sends, existing)
    mode = _OUTPUT_MODES[stated or "show"]
    calls = routine_recording._calls(sends, contracts)
    texts = [line for send in sends for line in (*routine_recording._lines(send.person), *send.window)]
    latest = [call for call in calls if call.send == calls[-1].send] if calls else []
    context = routine_recording._Context(
        sends, calls, routine_recording._known(texts, protection), routine_recording.zone(sends), asked, contracts
    )
    work = routine_provenance._narrowed(context, latest)
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
        planned = kept.document if kept is not None else document
        chained_from = asked.chained_from if asked is not None else None
        if stated is None or (stated == "chain" and not _chained(planned, chained_from)):
            # Asked first, the question keeps the Action the work would show, whose result a chain chosen then uses.
            shown = chained_from or _shown_action(planned)
            raise routine_recording._AskError(routine_recording.Question("routine-output-unstated", chained_from=shown))
    except routine_recording._AskError as asking:
        repairing = _repairing(asking.question, stated)
        if repaired and repairing:
            raise routine_recording.RecordingError("routine-record-again") from asking
        if repairing and repair_from is None:
            repair_from = len(sends)
        return dataclasses.replace(_asked(context, work, asking.question), repair_from=repair_from)
    if kept is None and not _chosen_used(context, work):
        raise routine_recording.RecordingError("routine-record-again")
    if kept is not None:
        return kept
    timezone, source = context.zone
    document["timezone"] = timezone
    permitted = _permitted([(step["assistant"], step["action"]) for step in document["steps"]], contracts)
    return routine_recording.Recorded(document, origins, permitted, when, source)


def _repairing(question: routine_recording.Question, stated: str | None) -> bool:
    """Whether a question asks for work to run again: a rerun, or the chain the person chose and the work lacks."""
    return question.code == routine_recording.RERUN or (
        question.code == "routine-output-unstated" and stated == "chain"
    )


def _chosen_used(context: routine_recording._Context, work: list[routine_recording._Call]) -> bool:
    """Whether the work sends every target the person chose wherever it calls the Action that choice is about."""
    for binding in routine_provenance._bindings(context):
        if binding.chosen is None:
            continue
        chosen = routine_recording._json_text(binding.chosen)
        calls = [call for call in work if call.action == binding.action]
        if calls and not any(routine_provenance._sent_value(call, binding.member) == chosen for call in calls):
            return False
    return True


def _asked(
    context: routine_recording._Context,
    work: list[routine_recording._Call],
    question: routine_recording.Question,
) -> routine_recording.Question:
    """A question as Team keeps it: with the work it asks to repeat, and every choice already answered.

    A choice the person answered exactly stays bound, whatever Team asks next.
    """
    chosen = list(context.asked.chosen) if context.asked is not None else []
    answered = context.asked.pending if context.asked is not None else None
    if answered is not None:
        selected = routine_provenance._selection(context, answered)
        if selected is not None:
            chosen.append(dataclasses.replace(answered, chosen=selected))
    pending = question.pending
    if pending is not None and pending.chosen is not None:
        chosen.append(pending)
        pending = None
    kept = {(item.action, item.member, item.targets): item for item in chosen}
    manifest = question.manifest
    if manifest is None and question.code == routine_recording.RERUN:
        manifest = routine_rerun._manifest(context, work, tuple(kept.values()))
    chained_from = question.chained_from or (context.asked.chained_from if context.asked is not None else None)
    return dataclasses.replace(
        question, pending=pending, manifest=manifest, chosen=tuple(kept.values()), chained_from=chained_from
    )


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
    raise routine_recording._AskError(routine_recording.Question("routine-schedule-unstated"))


# How each output choice the person states runs: a chain uses the result in other Actions, which the recorded work then
# runs, and shows it. A chain whose work has no step reading an earlier step's result is no chain yet: the output
# question stands, and the Brain runs the chained work when the person answers it, once.
_OUTPUT_MODES = {**{mode: mode for mode in http_routine.OUTPUT_MODES}, "chain": "show"}


def _chained(document: Mapping[str, object], chained_from: tuple[str, str] | None) -> bool:
    """Whether a plan's work uses an earlier result, as a chain does: a later step reads it.

    Chosen when Team asked for the output, the chain must use a result of the Action the work would then have shown,
    whatever its input; stated with the request, any earlier result some step reads.
    """
    steps = document["steps"]
    read = {source["step"] for step in steps for source in step["input"].values() if source["kind"] == "step_output"}
    return any(
        step["id"] in read and (chained_from is None or (step["assistant"], step["action"]) == chained_from)
        for step in steps
    )


def _shown_action(document: Mapping[str, object]) -> tuple[str, str] | None:
    """The Assistant and Action of the step a plan shows, or None when it shows none."""
    shown = document["output"]["step"]
    step = next((step for step in document["steps"] if step["id"] == shown), None)
    return None if step is None else (step["assistant"], step["action"])


def _output(sends: Sequence[routine_recording.Send], existing: routine_recording.Existing | None) -> str | None:
    """The output choice the latest authored segment stating one states; a replacement keeps its mode when none is.

    None when no segment states one, or the latest that does states two: the person is then asked.
    """
    latest: tuple[str, ...] = ()
    for send in sends:
        for segment in send.person:
            found = phrase.outputs(segment)
            if found:
                latest = found
    if len(latest) == 1:
        return latest[0]
    if not latest and existing is not None:
        return existing.plan["output"]["mode"]
    return None


# --- What it runs ----------------------------------------------------------------------------------------------------


def _kept(
    context: routine_recording._Context, mode: str, existing: routine_recording.Existing
) -> routine_recording.Recorded:
    """A replacement that ran no Action: the replaced plan's steps exactly, on a new schedule, zone, or output."""
    steps = [dict(step) for step in existing.plan["steps"]]
    for step in steps:
        contract = context.contracts.get((step["assistant"], step["action"]))
        if contract is None or contract.pin != step["pin"]:
            raise routine_recording.RecordingError("plan-pin-drift")
    origins = {
        step["id"]: {member: _kept_origin(source, context.known) for member, source in step["input"].items()}
        for step in steps
    }
    document = _document(steps, steps[-1]["id"], mode)
    when = _schedule(context.sends, existing)
    timezone, source = context.zone
    document["timezone"] = timezone
    actions = [(step["assistant"], step["action"]) for step in steps]
    return routine_recording.Recorded(document, origins, _permitted(actions, context.contracts), when, source)


def _document(steps: list[dict[str, object]], last: str | None, mode: str) -> dict[str, object]:
    return {
        "version": routine_plan.VERSION,
        "timezone": http_routine.CONVENTIONAL_TIMEZONE,
        "steps": steps,
        "output": {"mode": mode, "step": last if mode in http_routine.SHOWN_MODES else None},
    }


def _kept_origin(source: Mapping[str, object], known: routine_recording._Known) -> str:
    """How the card names a kept input's origin, as a recording would now."""
    if source["kind"] == "literal":
        return "request" if known.names(source["value"]) else "assistant"
    if source["kind"] == "run_clock":
        return "clock"
    return "selector" if "where" in source else "step"


def _plan(
    context: routine_recording._Context, mode: str, work: list[routine_recording._Call]
) -> tuple[dict[str, object], dict]:
    """The plan document and each step's input origins, from the work and every source it needs."""
    if not work:
        raise routine_recording.RecordingError("routine-recording-empty")
    _classes(context)
    nodes = _closure(context, work)
    order = _ordered(context, nodes)
    names = {node: f"s{position}" for position, node in enumerate(order, start=1)}
    context.planned = {names[node]: nodes[node].occurrence for node in order}
    steps = [_step(context, nodes[node], names) for node in order]
    origins = {names[node]: context.inputs[node][1] for node in order}
    work_nodes = {context.classes[call.index] for call in work}
    shown = next((names[node] for node in reversed(order) if node in work_nodes), None)
    _verify(context, nodes, steps, names)
    return _document(steps, shown, mode), origins


def _closure(
    context: routine_recording._Context, work: list[routine_recording._Call]
) -> dict[int, routine_recording._Call]:
    """The plan calls: each work call's source representative, and every source their inputs read, transitively."""
    nodes: dict[int, routine_recording._Call] = {}
    pending = [context.classes[call.index] for call in work]
    while pending:
        node = pending.pop()
        if node in nodes:
            continue
        nodes[node] = context.calls[node]
        routine_provenance._classify_call(context, nodes[node])
        inputs, _origins = context.inputs[node]
        pending.extend(source["node"] for source in inputs.values() if "node" in source)
    return nodes


def _step(
    context: routine_recording._Context, call: routine_recording._Call, names: dict[int, str]
) -> dict[str, object]:
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


def _classes(context: routine_recording._Context) -> None:
    """Each call's source: its earliest read-only twin (same Action, input, and result) with no change between."""
    open_sources: list[routine_recording._Call] = []
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


def _edges(context: routine_recording._Context, nodes: dict[int, routine_recording._Call]) -> dict[int, set[int]]:
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


def _ordered(context: routine_recording._Context, nodes: dict[int, routine_recording._Call]) -> list[int]:
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
    context: routine_recording._Context,
    nodes: dict[int, routine_recording._Call],
    steps: list[dict[str, object]],
    names: dict[int, str],
) -> None:
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
            raise routine_recording.RecordingError("routine-recording-unverified")
        started = context.sends[call.send].started_at
        for member, source in step["input"].items():
            if not routine_plan.same(_resolved(context, source, results, started), sent[member]):
                raise routine_recording.RecordingError("routine-recording-unverified")


def _resolved(
    context: routine_recording._Context, source: Mapping[str, object], results: Mapping[str, object], started: int
) -> object:
    if source["kind"] == "literal":
        return source["value"]
    if source["kind"] == "run_clock":
        instant = datetime.datetime.fromtimestamp(started, datetime.UTC)
        return routine_dates.clock_value(source, instant, context.zone[0])
    try:
        if "where" in source:
            return routine_plan.select_where(
                results[source["step"]], source["pointer"], source["where"], source["item"]
            )
        return routine_plan.select(results[source["step"]], source["pointer"])
    except routine_plan.PlanError:
        return _UNRESOLVED


_UNRESOLVED = object()
