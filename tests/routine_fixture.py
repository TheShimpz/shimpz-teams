"""A compiled Routine plan document for Routine record, store, and service tests (ADR-0092)."""

from __future__ import annotations

PIN = "sha256:" + "d" * 64


def plan_document(assistant: str = "dns", timezone: str = "UTC", action: str = "check") -> dict[str, object]:
    """One well-formed one-step plan that runs ``action`` of ``assistant`` with no input."""
    step = {"id": "check", "assistant": assistant, "action": action, "pin": PIN, "input": {}}
    return {"version": 1, "timezone": timezone, "steps": [step]}
