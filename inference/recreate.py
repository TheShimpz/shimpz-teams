"""Recriar's one compile of a held Routine's creation message, as Team asks it (ADR-0092 amendment, 2026-10-02).

Team sends only the Routine's sealed words, the person's own kinded parts whose last is the creating said text (ADR-0092
amendment, 2026-10-05), and the Team's current Assistant contracts. The Brain compiles it
from scratch, as a chat create would, with no turn, tools, history, or Routine to keep members from. The answer is the
create change and, when the planner asks about one field, its question; or one closed refusal. Team alone admits the
change against the message and the exact current contracts, and commits it in place of the current Routine.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from action import journal as action_journal
from inference.client import BrainRuntimeError, RuntimeAssistant, _action_wire
from protocol.http.v1 import payload as http_payload
from routine import request as routine_request

REFUSALS = frozenset(
    {
        "not-recurring",
        "quoted",
        "secret",
        "unspecified",
        "unsupported",
        "schedule",
        "unproven",
        "unavailable",
        "budget",
        "too-large",
    }
)


@dataclass(frozen=True, slots=True)
class Compiled:
    """The proposed create change, with its question beside exactly its clarification when the planner asked."""

    routine: dict[str, object]
    clarification: dict[str, object] | None


def _valid(
    credentials: tuple[str, str, str], parts: tuple[routine_request.Part, ...], assistants: Sequence[RuntimeAssistant]
) -> bool:
    provider, model, api_key = credentials
    return (
        provider in {"anthropic", "openai"}
        and isinstance(model, str)
        and action_journal.SAFE_ID_RE.fullmatch(model) is not None
        and isinstance(api_key, str)
        and 0 < len(api_key) <= 16 * 1024
        and "\0" not in api_key
        and 0 < len(assistants) <= 16
        and routine_request.canonical_parts([{"kind": kind, "text": text} for kind, text in parts]) == parts
    )


def compile_routine(
    client: object,
    credentials: tuple[str, str, str],
    parts: tuple[routine_request.Part, ...],
    assistants: Sequence[RuntimeAssistant],
    capacity: int,
) -> Compiled | str:
    """``credentials`` is the Team's provider, model, and key; returns the change or the closed refusal reason.

    ``parts`` are the Routine's sealed words, oldest first: the last is the compile's message and the others its draft.
    ``capacity`` is the daily business steps the Team leaves the recreated Routine, advisory to the compiler.
    """
    if not _valid(credentials, parts, assistants) or type(capacity) is not int:
        raise BrainRuntimeError("Brain runtime Routine compile request is invalid")
    provider, model, api_key = credentials
    payload = {
        "provider": {"provider": provider, "model": model, "api_key": api_key},
        "locale": None,
        "message": parts[-1][1],
        "draft": [{"kind": kind, "text": text} for kind, text in parts[:-1]],
        "capacity": capacity,
        "assistants": [
            {
                "id": assistant.id,
                "genesis": assistant.genesis,
                "actions": [_action_wire(action) for action in assistant.actions],
            }
            for assistant in assistants
        ],
    }
    answer = client.routine_compile(payload, provider, model)
    if not isinstance(answer, dict) or set(answer) != {"routine", "reply", "clarification", "refusal"}:
        raise BrainRuntimeError("Brain runtime returned an invalid response")
    routine, clarification, refusal = answer["routine"], answer["clarification"], answer["refusal"]
    if refusal is not None:
        if refusal not in REFUSALS or (routine, answer["reply"], clarification) != (None, None, None):
            raise BrainRuntimeError("Brain runtime returned an invalid response")
        return refusal
    if clarification is not None:
        clarification = http_payload.canonical_clarification(clarification)
    if (
        not isinstance(routine, dict)
        or not isinstance(answer["reply"], str)
        or (answer["clarification"] is None) == ("question" in routine)
        or (answer["clarification"] is not None and clarification is None)
    ):
        raise BrainRuntimeError("Brain runtime returned an invalid response")
    return Compiled(routine, clarification)
