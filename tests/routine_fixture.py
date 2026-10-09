"""A recorded Routine plan and confirmed definitions for Routine record, store, and service tests (ADR-0101)."""

import dataclasses

PIN = "sha256:" + "d" * 64
SCOPE_PIN = "sha256:" + "a" * 64


# The output disposition a plan names unless a test chooses another: show the one step's result after every run.
SHOW = {"mode": "show", "step": "check"}


def plan_document(
    assistant: str = "dns", timezone: str = "UTC", action: str = "check", output: dict[str, object] | None = None
) -> dict[str, object]:
    """One well-formed one-step plan that runs ``action`` of ``assistant`` with no input and shows its result."""
    step = {"id": "check", "assistant": assistant, "action": action, "pin": PIN, "input": {}}
    return {"version": 4, "timezone": timezone, "steps": [step], "output": dict(SHOW if output is None else output)}


def permitted(plan: dict[str, object], read_only: bool = True) -> tuple[dict[str, object], ...]:
    """Every Action of ``plan`` once, at its step's pin, as a recording permits them."""
    found = {(step["assistant"], step["action"]): step["pin"] for step in plan["steps"]}
    return tuple(
        {"assistant": assistant, "action": action, "pin": pin, "read_only": read_only, "stored_inputs": []}
        for (assistant, action), pin in sorted(found.items())
    )


CONFIRMATION = {
    "proposal_id": "c" * 32,
    "proposal_digest": "sha256:" + "e" * 64,
    "principal": "f" * 32,
    "incarnation": "b" * 64,
    "confirmed_at": 1,
}


def confirmed(value):
    """A Routine record with the permitted Actions its plan needs, its Assistants pinned, and a confirmation."""
    entries = permitted(value.plan)
    assistants = tuple((assistant, SCOPE_PIN) for assistant in sorted({item["assistant"] for item in entries}))
    return dataclasses.replace(value, permitted=entries, assistants=assistants, confirmation=dict(CONFIRMATION))


def routine(
    routine_id: str,
    *,
    name: str,
    anchor: int,
    schedule: dict[str, object] | None = None,
    timezone: str = "UTC",
    plan: dict[str, object] | None = None,
):
    """A confirmed Routine, by default one plan_document step daily at 09:00, due when it first fires after anchor."""
    from routine import record

    value = confirmed(
        record.Routine(
            routine_id=routine_id,
            name=name,
            plan=plan or plan_document(timezone=timezone),
            schedule=dict(schedule or {"kind": "daily", "time": "09:00"}),
            timezone=timezone,
            assistants=(),
            anchor=anchor,
            next_run_at=0,
        )
    )
    return dataclasses.replace(value, next_run_at=record.first_run(value))


def put(store, team_id: str, state) -> None:
    """Replace a Team's Routine state through the store's only write path."""
    store.update(team_id, lambda _before: (state, None))


def update_routine(service, routine_id: str, *, confirm: bool = False, **changes: object) -> None:
    """Change fields of one of team_1's Routines in place through the store's write path; ``confirm`` re-pins it."""
    from routine import record

    def changed(state):
        value = dataclasses.replace(record.routine(state, routine_id), **changes)
        return record._replace_routine(state, confirmed(value) if confirm else value), None

    service.routine_store.update("team_1", changed)


def put_frozen_run(
    service, routine_id: str, assistant_id: str, *, step: int, steps: int, run_id: str | None = None, lease: int = 0
):
    """Make team_1's only run a frozen human-request list-zones run at a replay step; return the run."""
    from routine import record

    run = record.Run(
        run_id or record.new_id(),
        routine_id,
        "frozen",
        lease,
        request_kind="human",
        assistant_id=assistant_id,
        action="list-zones",
        position={"phase": "replay", "step": step},
        steps=steps,
    )
    service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, runs=(run,)), None))
    return run


# A completed run's notice detail: the summary of the plan it carried out.
DONE = {
    "plan": {
        "revision": 1,
        "plan_digest": "sha256:" + "d" * 64,
        "steps": 1,
        "actions": [["dns", "check", 1]],
        "more": 0,
    },
    "output": None,
}


def large_completion() -> dict[str, object]:
    """A completed run's detail at about its largest: a shown output near its 16 KiB bound and a full summary."""
    from routine import definition as routine_definition
    from routine import plan as routine_plan

    plan = plan_document()
    plan["steps"] = [
        {**plan["steps"][0], "id": f"s{index}", "action": "check" if index % 2 else "notify"} for index in range(256)
    ]
    node = routine_plan.output_safe({f"k{index:02d}": "\u0001" * 300 for index in range(24)}, {})
    return {
        "plan": routine_definition.summary(plan, 1),
        "output": routine_plan.output_shown(256, node),
    }


def set_aside(service, team_id: str, incident_id: str, choice: str = "run"):
    """A person sets a held run aside, as a card answer or a deletion does, without starting anything new."""
    import time

    from local.routine import incident as routine_incident
    from routine import hold as routine_hold

    def change(state):
        return routine_hold.skip_incident(state, incident_id, int(time.time()), choice=choice)

    return routine_incident.set_aside(service, team_id, incident_id, change)
