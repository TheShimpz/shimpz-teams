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
        if source["kind"] == "literal":
            return {"origins": [{"at": "", "from": "message", "text": "x", "region": None, "instruction": None}]}
        return {"instruction": "then"} if source["kind"] == "step_output" else {}

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
