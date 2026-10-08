"""What a Routine definition is on the wire and in its Team's budgets, without I/O (ADR-0092, ADR-0101).

A definition is a recorded plan and its standing scope: the Actions it may call at their pins (every replay step's),
whether each is read-only, and the Stored Inputs each uses by name only. A Supervisor inspects a Routine through its
plan's safe projection, its summary, and its pages. On the wire a step is named by its 1-based position in its
revision's plan, never by its internal id: a projected step shows its Action, whether it only reads, each input's source
(a literal as a bounded preview, a reference by the earlier step's position, its pointer, and through an array item its
selector and the item's pointer), and the Stored Inputs its Action uses. Every list view and notice carries a compact
summary of the revision instead of its steps.

A definition's share of its Team's budgets is one function of its units: its replay steps (``run_units``), which every
start reserves, every rolling 24-hour cap multiplies, and the run's active time grows with.
Paused Routines count, so resuming one never needs room it lacks; a deleting one keeps its share until it is gone.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence

from protocol.http.v1 import routine as http_routine
from routine import plan as routine_plan


def plan_digest(plan: Mapping[str, object]) -> str:
    return "sha256:" + hashlib.sha256(routine_plan.canonical(plan)).hexdigest()


def run_units(value: object) -> int:
    """A run's units: its replay steps, which every budget derives from (ADR-0101 §6.4)."""
    return len(value.plan["steps"])


def state(value: object) -> str:
    """The Routine's state: paused by a person, or active."""
    return "paused" if value.paused else "active"


def step_positions(plan: Mapping[str, object]) -> dict[str, int]:
    return {step["id"]: index for index, step in enumerate(plan["steps"], start=1)}


def permitted_entry(permitted: Sequence[Mapping[str, object]], assistant: str, action: str) -> Mapping[str, object]:
    return next(item for item in permitted if (item["assistant"], item["action"]) == (assistant, action))


def shown_where(source: Mapping[str, object]) -> dict[str, str] | None:
    """A step-output source's item selector as a person reads it, or None when it selects no item."""
    where = source.get("where")
    if where is None:
        return None
    ((key, constant),) = where.items()
    return {"member": key, "value_json": http_routine.where_text(constant)}


def _input(member: str, source: Mapping[str, object], positions: Mapping[str, int]) -> dict[str, object]:
    if source["kind"] == "literal":
        return {"member": member, "source": "literal", "value": http_routine.literal_preview(source["value"])}
    if source["kind"] == "run_clock":
        return {"member": member, "source": "run_clock", "value": source["format"]}
    shown = shown_where(source)
    return {
        "member": member,
        "source": "step_output",
        "step": positions[source["step"]],
        "pointer": source["pointer"],
        "where": shown,
        "item": source.get("item"),
    }


def step(plan: Mapping[str, object], permitted: Sequence[Mapping[str, object]], position: int) -> dict[str, object]:
    """The projection of the step at ``position``: its Action, its effect, every input's source, its Stored Inputs."""
    positions = step_positions(plan)
    value = plan["steps"][position - 1]
    entry = permitted_entry(permitted, value["assistant"], value["action"])
    return {
        "position": position,
        "assistant": value["assistant"],
        "action": value["action"],
        "read_only": entry["read_only"],
        "inputs": [_input(member, value["input"][member], positions) for member in sorted(value["input"])],
        "stored_inputs": list(entry["stored_inputs"]),
    }


def steps_fit(plan: Mapping[str, object], permitted: Sequence[Mapping[str, object]]) -> bool:
    """Whether every step's projection stays within its own wire bound, so every page of the plan is deliverable."""
    return all(
        http_routine.canonical_step(step(plan, permitted, position), position) is not None
        for position in range(1, len(plan["steps"]) + 1)
    )


def summary(plan: Mapping[str, object], revision: int) -> dict[str, object]:
    """The revision's compact summary: its step count and its Actions as runs, at most 16, with the steps after them."""
    runs: list[list[object]] = []
    for value in plan["steps"]:
        pair = [value["assistant"], value["action"]]
        if runs and runs[-1][:2] == pair:
            runs[-1][2] += 1
        else:
            runs.append([*pair, 1])
    kept = runs[: http_routine.MAX_SUMMARY_RUNS]
    total = len(plan["steps"])
    return {
        "revision": revision,
        "plan_digest": plan_digest(plan),
        "steps": total,
        "actions": kept,
        "more": total - sum(run[2] for run in kept),
    }


def disposition(plan: Mapping[str, object]) -> dict[str, object]:
    """The plan's output disposition on the wire: its shown step by position, or none."""
    output = plan["output"]
    shown = output["step"]
    return {"mode": output["mode"], "step": None if shown is None else step_positions(plan)[shown]}


def permitted_summary(permitted: Sequence[Mapping[str, object]]) -> dict[str, int]:
    """How many Actions a Routine may call, and how many of them change something."""
    return {"total": len(permitted), "changes": sum(not item["read_only"] for item in permitted)}


def page(
    routine_id: str, revision: int, plan: Mapping[str, object], permitted: Sequence[Mapping[str, object]], offset: int
) -> dict[str, object]:
    """Whole consecutive projected steps from ``offset``, as many as fit one page, and where the next page starts."""
    total = len(plan["steps"])
    chosen: list[dict[str, object]] = []
    used = 2
    for position in range(offset + 1, total + 1):
        projected = step(plan, permitted, position)
        cost = http_routine.encoded_bytes(projected) + 1
        if chosen and (len(chosen) == http_routine.MAX_PAGE_STEPS or used + cost > http_routine.MAX_PAGE_BYTES):
            break
        chosen.append(projected)
        used += cost
    following = offset + len(chosen)
    return {
        "routine_id": routine_id,
        "revision": revision,
        "plan_digest": plan_digest(plan),
        "total": total,
        "offset": offset,
        "steps": chosen,
        "next": None if following == total else following,
    }


def scope(value: object) -> dict[str, object]:
    """A Routine's standing scope as views and notices carry it."""
    return {"state": state(value), "permitted": permitted_summary(value.permitted)}


def detail(value: object) -> dict[str, object]:
    """What a created or changed notice says a Routine does: its name, its plan's summary, when, and its scope."""
    return {
        "name": value.name,
        "plan": summary(value.plan, value.revision),
        "output": disposition(value.plan),
        "schedule": dict(value.schedule),
        "timezone": value.timezone,
        "timezone_source": value.timezone_source,
        **scope(value),
    }


def fits(value: object) -> bool:
    """Whether every projected step of a Routine is within its wire bound and its definition within its own budget."""
    return steps_fit(value.plan, value.permitted) and definition_bytes(value) <= routine_plan.MAX_DEFINITION_BYTES


def definition_bytes(value: object) -> int:
    """The canonical bytes of a Routine's definition: its plan and its standing scope, which its budget bounds."""
    standing = {"permitted": list(value.permitted), "confirmation": value.confirmation}
    return len(routine_plan.canonical(value.plan)) + len(routine_plan.canonical(standing))


def daily_steps(value: object) -> int:
    """The Action units a Routine's rolling 24-hour cap allocates: every start may use every unit."""
    return http_routine.daily_cap(value.schedule) * run_units(value)


def capacity(routines: Sequence[object]) -> int:
    """The daily Action units left for a new or changed Routine after these Routines' allocations."""
    return routine_plan.MAX_DAILY_STEPS - sum(daily_steps(item) for item in routines)


def over_budget(others: Sequence[object], admitted: object) -> str | None:
    """Which Team budget a definition outgrows beside the Team's other Routines, or None: rate, steps, or bytes."""
    if daily_steps(admitted) > capacity(others):
        return "routine-step-budget"
    if sum(definition_bytes(item) for item in (*others, admitted)) > routine_plan.TEAM_DEFINITION_BYTES:
        return "routine-team-budget"
    return None
