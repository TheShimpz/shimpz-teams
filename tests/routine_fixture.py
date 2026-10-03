"""A compiled Routine plan document for Routine record, store, and service tests (ADR-0092)."""

from __future__ import annotations

PIN = "sha256:" + "d" * 64


def plan_document(assistant: str = "dns", timezone: str = "UTC", action: str = "check") -> dict[str, object]:
    """One well-formed one-step plan that runs ``action`` of ``assistant`` with no input."""
    step = {"id": "check", "assistant": assistant, "action": action, "pin": PIN, "input": {}}
    return {"version": 1, "timezone": timezone, "steps": [step]}


def grant(plan: dict[str, object], revision: int = 1) -> dict[str, object]:
    """Complete request evidence for ``plan`` at ``revision``, as a committed change binds it."""
    from routine import grant as routine_grant

    def provenance(source: dict[str, object]) -> dict[str, object]:
        by = {"message": "f" * 64, "receipt": "e" * 64, "revision": revision, "selected": None}
        if source["kind"] == "literal":
            return {"proof": {"origins": [{"at": "", "from": "message", "span": [0, 1]}]}, "by": by}
        return {"proof": {"instruction": [0, 4]} if source["kind"] == "step_output" else {}, "by": by}

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
        "stored_inputs": {step["id"]: [] for step in plan["steps"]},
    }


def granted(value):
    """A Routine record with the evidence its plan and revision need."""
    import dataclasses

    return dataclasses.replace(value, grant=grant(value.plan, value.revision))


# A completed run's notice detail: the Actions it carried out.
DONE = {"actions": [["dns", "check"]]}


def large_definition() -> dict[str, object]:
    """A created notice's detail of about 70 KiB encoded: one fits a notice batch, two exceed its byte bound."""
    inputs = [{"member": f"m{index:03d}", "source": "literal", "value": '"' * 120} for index in range(30)]
    steps = [
        {"id": f"s{index}", "assistant": "dns", "action": "check", "inputs": inputs, "stored_inputs": []}
        for index in range(8)
    ]
    return {"name": "Large", "steps": steps, "schedule": {"kind": "daily", "time": "09:00"}, "timezone": "UTC"}


def set_aside(service, team_id: str, incident_id: str, choice: str = "run"):
    """A person sets a held run aside, as a card answer or a deletion does, without starting anything new."""
    import time

    from local.routine import incident as routine_incident
    from routine import hold as routine_hold

    def change(state):
        return routine_hold.skip_incident(state, incident_id, int(time.time()), choice=choice)

    return routine_incident.set_aside(service, team_id, incident_id, change)
