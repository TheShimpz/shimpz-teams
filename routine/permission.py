"""What a person's Permitir of a frozen decision call is bound to (ADR-0101 section 8).

A decision call to an Action outside a Routine's permitted set freezes before dispatch, as an Integration pause does,
holding no Action batch. Its requirement binds the run, the Routine revision and permissions revision it froze under,
the pending interrupt and its logical operation, the Assistant Action at its complete pin, and the input commitment.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Requirement:
    run_id: str
    revision: int
    permissions_revision: int
    interrupt_id: str
    operation_id: str
    assistant: str
    action: str
    pin: str
    commitment: str
