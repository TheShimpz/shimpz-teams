"""The private creation source of a Routine, which Recriar compiles from again (ADR-0092 amendment, 2026-10-02).

A Routine created from a person's own chat message keeps that exact message, and the value the person selected when
the planner asked about one field, sealed apart from the plaintext Routine state and bound to its Team incarnation and
Routine. It is written before the state write that creates the Routine, so a crash leaves at worst an unreferenced
record, which the watchdog removes; it is never replaced by a later update, and it goes when the Routine is deleted. It
is not shown, audited, sent to diagnostics, or kept in any Brain history: only Recriar reads it, to recompile the
Routine from scratch in place.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from core import strict_json
from local.routine import state as routine_state
from routine import plan as routine_plan
from routine import record

VERSION = 1
_FIELDS = frozenset({"version", "routine_id", "incarnation", "message", "selected"})
MAX_MESSAGE_CHARS = 16_000


@dataclass(frozen=True, slots=True)
class Source:
    """The exact message that created a Routine and the one field value its person selected, if any."""

    routine_id: str
    incarnation: str
    message: str
    # (field, value): ("schedule",), ("timezone",), or ("input", step, member), and the value that field held.
    selected: tuple[tuple[str, ...], object] | None = None

    def encode(self) -> bytes:
        selected = None if self.selected is None else {"field": list(self.selected[0]), "value": self.selected[1]}
        return routine_plan.canonical(
            {
                "version": VERSION,
                "routine_id": self.routine_id,
                "incarnation": self.incarnation,
                "message": self.message,
                "selected": selected,
            }
        )

    @property
    def commitment(self) -> str:
        return hashlib.sha256(self.encode()).hexdigest()


def field_value(value: record.Routine, field: tuple[str, ...]) -> object:
    """What one Routine holds in a question's field: its schedule, its zone, or one step input's source.

    None when the Routine has no such step input.
    """
    if field == ("schedule",):
        return dict(value.schedule)
    if field == ("timezone",):
        return value.timezone
    step = next((item for item in value.plan["steps"] if item["id"] == field[1]), None)
    return None if step is None else step["input"].get(field[2])


def _field(value: object) -> tuple[str, ...] | None:
    if value in (["schedule"], ["timezone"]):
        return tuple(value)
    if isinstance(value, list) and len(value) == 3 and value[0] == "input":
        return tuple(value) if all(isinstance(item, str) and item for item in value) else None
    return None


def decode(payload: bytes, routine_id: str) -> Source:
    """Admit a sealed source only for exactly the Routine it was sealed for."""
    try:
        value = strict_json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise routine_state.unavailable() from exc
    if not isinstance(value, dict) or set(value) != _FIELDS or value["version"] != VERSION:
        raise routine_state.unavailable()
    message, selected = value["message"], value["selected"]
    if (
        value["routine_id"] != routine_id
        or not isinstance(value["incarnation"], str)
        or not value["incarnation"]
        or not isinstance(message, str)
        or not 0 < len(message) <= MAX_MESSAGE_CHARS
    ):
        raise routine_state.unavailable()
    if selected is not None:
        field = (
            _field(selected.get("field"))
            if isinstance(selected, dict) and set(selected) == {"field", "value"}
            else None
        )
        if field is None:
            raise routine_state.unavailable()
        selected = (field, selected["value"])
    source = Source(routine_id, value["incarnation"], message, selected)
    if source.encode() != payload:
        raise routine_state.unavailable()
    return source


def seal(self, team_id: str, source: Source) -> None:
    """Seal a new Routine's creation source; the caller holds the Team's Routine lock through the creating write."""
    routine_state.call(lambda: self.routine_store.put_source(team_id, source.routine_id, source.encode()))


def load(self, team_id: str, routine_id: str) -> Source | None:
    """The Routine's sealed creation source, or None when it has none."""
    payload = routine_state.call(lambda: self.routine_store.source(team_id, routine_id))
    return None if payload is None else decode(payload, routine_id)


def sweep(self, team_id: str) -> None:
    """Remove every source whose Routine is not listed, under the lock a creating write holds across its seal."""
    with self.routine_store.lock(team_id):
        listed = {item.routine_id for item in self.routine_store.load(team_id).routines}
        for routine_id in self.routine_store.sources(team_id):
            if routine_id not in listed:
                self.routine_store.delete_source(team_id, routine_id)
