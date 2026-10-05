"""A compiled Routine plan document for Routine record, store, and service tests (ADR-0092)."""

from __future__ import annotations

PIN = "sha256:" + "d" * 64


# The output disposition a plan names unless a test chooses another: show the one step's result after every run.
SHOW = {"mode": "show", "step": "check"}


def plan_document(
    assistant: str = "dns", timezone: str = "UTC", action: str = "check", output: dict[str, object] | None = None
) -> dict[str, object]:
    """One well-formed one-step plan that runs ``action`` of ``assistant`` with no input and shows its result."""
    step = {"id": "check", "assistant": assistant, "action": action, "pin": PIN, "input": {}}
    return {"version": 2, "timezone": timezone, "steps": [step], "output": dict(SHOW if output is None else output)}


def chain_document(assistant: str = "dns") -> dict[str, object]:
    """A two-step plan whose second step takes the first one's whole result, so a run hands its result on (chain)."""
    first = {"id": "check", "assistant": assistant, "action": "check", "pin": PIN, "input": {}}
    handed = {"value": {"kind": "step_output", "step": "check", "pointer": ""}}
    second = {"id": "notify", "assistant": assistant, "action": "notify", "pin": PIN, "input": handed}
    return {"version": 2, "timezone": "UTC", "steps": [first, second], "output": {"mode": "chain", "step": None}}


def grant(plan: dict[str, object], revision: int = 1) -> dict[str, object]:
    """Complete request evidence for ``plan`` at ``revision``, as a committed change binds it."""
    from routine import grant as routine_grant

    def provenance(source: dict[str, object]) -> dict[str, object]:
        by = {"message": "f" * 64, "receipt": "e" * 64, "revision": revision, "selected": None}
        if source["kind"] == "literal":
            return {"proof": {"origins": [{"at": "", "from": "message", "span": [0, 1]}]}, "by": by}
        return {"proof": {"instruction": [0, 4]} if source["kind"] in ("step_output", "step_text") else {}, "by": by}

    return {
        "receipt": "e" * 64,
        "revision": revision,
        "plan": routine_grant.plan_digest(plan),
        "message": "f" * 64,
        "quote": [0, 5],
        "selected": None,
        "sources": {
            step["id"]: {name: provenance(item) for name, item in step["input"].items()} for step in plan["steps"]
        },
        "output": {
            "proof": {"instruction": [0, 4]},
            "by": {"message": "f" * 64, "receipt": "e" * 64, "revision": revision, "selected": None},
        },
        "stored_inputs": {step["id"]: [] for step in plan["steps"]},
    }


def granted(value):
    """A Routine record with the evidence its plan and revision need."""
    import dataclasses

    return dataclasses.replace(value, grant=grant(value.plan, value.revision))


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
    from routine import grant as routine_grant
    from routine import plan as routine_plan

    plan = plan_document()
    plan["steps"] = [
        {**plan["steps"][0], "id": f"s{index}", "action": "check" if index % 2 else "notify"} for index in range(256)
    ]
    node = routine_plan.output_safe({f"k{index:02d}": "\u0001" * 300 for index in range(24)}, {})
    return {"plan": routine_grant.summary(plan, 1), "output": routine_plan.output_shown(256, node)}


def set_aside(service, team_id: str, incident_id: str, choice: str = "run"):
    """A person sets a held run aside, as a card answer or a deletion does, without starting anything new."""
    import time

    from local.routine import incident as routine_incident
    from routine import hold as routine_hold

    def change(state):
        return routine_hold.skip_incident(state, incident_id, int(time.time()), choice=choice)

    return routine_incident.set_aside(service, team_id, incident_id, change)
