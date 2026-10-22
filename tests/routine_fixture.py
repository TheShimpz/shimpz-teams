"""A recorded Routine plan and confirmed definitions for Routine record, store, and service tests (ADR-0101)."""

import dataclasses
import datetime

PIN = "sha256:" + "d" * 64
SCOPE_PIN = "sha256:" + "a" * 64
# The routine key fingerprint runs are claimed under, and the Team network their generations are bound in.
KEY = "e" * 64
NETWORK = "a" * 64
# The first step of a plan, by position.
STEP = 1
EVERY_HOUR = {"kind": "continuous", "gap": 3600}


def epoch(*parts: int) -> int:
    return int(datetime.datetime(*parts, tzinfo=datetime.UTC).timestamp())


NINE = epoch(2026, 10, 1, 9)
ANCHOR = epoch(2026, 9, 1)


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


DESCRIPTION = "Checks the DNS zone."


def confirmed(value):
    """A Routine record with its plan's permitted Actions, its Assistants pinned, a description, and a confirmation."""
    entries = permitted(value.plan)
    assistants = tuple((assistant, SCOPE_PIN) for assistant in sorted({item["assistant"] for item in entries}))
    return dataclasses.replace(
        value,
        description=value.description or DESCRIPTION,
        permitted=entries,
        assistants=assistants,
        confirmation=dict(CONFIRMATION),
    )


def routine(
    routine_id: str = "a" * 32,
    schedule: dict[str, object] | None = None,
    *,
    name: str = "Daily DNS summary",
    anchor: int = ANCHOR,
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
            assistants=(),
            anchor=anchor,
            next_run_at=0,
        )
    )
    return dataclasses.replace(value, next_run_at=record.first_run(value))


def added(*routines):
    """A Team's Routine state holding these Routines and nothing else."""
    from routine import record

    state = record.TeamRoutines()
    for value in routines:
        state = record.add_routine(state, value)
    return state


def at(state, routine_id: str, next_run_at: int):
    """The state with one Routine next due at ``next_run_at``."""
    from routine import record

    return record.replace_routine(
        state, dataclasses.replace(record.routine(state, routine_id), next_run_at=next_run_at)
    )


def claim_bound(state, now: int, *, key: str = KEY, network: str = NETWORK):
    """Claim the Team's due run at ``now`` and bind its first generation, as its first segment does.

    Returns the state, the claim, and the lease that drives the run.
    """
    from routine import claim as routine_claim
    from routine import record

    state, claim = routine_claim.claim(state, now, key)
    lease = record.lease_of(claim.lease_token, key)
    return routine_claim.bind_generation(state, claim.run.run_id, lease, now, network), claim, lease


def claimed(now: int = NINE):
    """The default Routine due at NINE, claimed at ``now``: the state, the claim, and its lease."""
    from routine import claim as routine_claim
    from routine import record

    state, claim = routine_claim.claim(at(added(routine()), "a" * 32, NINE), now, KEY)
    return state, claim, record.lease_of(claim.lease_token, KEY)


def bound(now: int = NINE):
    """The claimed default run with its first generation bound in network ``net_1``."""
    from routine import claim as routine_claim

    state, claim, lease = claimed(now)
    return routine_claim.bind_generation(state, claim.run.run_id, lease, now, "net_1"), claim, lease


def held():
    """The bound default run fenced for a hold, and its run id."""
    from routine import hold as routine_hold

    state, claim, lease = bound()
    return routine_hold.fence(state, claim.run.run_id, lease, NINE), claim.run.run_id


def full_notices(count: int | None = None):
    """A full queue of undelivered done notices of another Routine."""
    from routine import record

    total = record.MAX_UNDELIVERED_NOTICES if count is None else count
    return tuple(record.Notice(f"{index:032x}", "c" * 32, "", "done", NINE, DONE) for index in range(total))


def put(store, team_id: str, state) -> None:
    """Replace a Team's Routine state through the store's only write path."""
    store.update(team_id, lambda _before: (state, None))


def update_routine(service, routine_id: str, *, confirm: bool = False, **changes: object) -> None:
    """Change fields of one of team_1's Routines in place through the store's write path; ``confirm`` re-pins it."""
    from routine import record

    def changed(state):
        value = dataclasses.replace(record.routine(state, routine_id), **changes)
        return record.replace_routine(state, confirmed(value) if confirm else value), None

    service.routine_store.update("team_1", changed)


def put_frozen_run(service, routine_id: str, assistant_id: str, *, run_id: str | None = None):
    """Make team_1's only live run a frozen human-request list-zones run; return the run."""
    from routine import record

    run = record.Run(
        run_id or record.new_id(),
        routine_id,
        "frozen",
        request_kind="human",
        assistant_id=assistant_id,
        action="list-zones",
    )
    service.routine_store.update(
        "team_1", lambda state: (dataclasses.replace(state, runs=(*record.incidents(state), run)), None)
    )
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


def set_aside(service, team_id: str, incident_id: str):
    """A person sets a held run aside, as a card answer or a deletion does, without starting anything new."""
    import time

    from local.routine import incident as routine_incident
    from routine import hold as routine_hold

    def change(state):
        return routine_hold.skip_incident(state, incident_id, int(time.time()))

    return routine_incident.set_aside(service, team_id, incident_id, change)
