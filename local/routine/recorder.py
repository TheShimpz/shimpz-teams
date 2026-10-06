"""The recording of one Local chat turn that may define a Routine, in Team memory only (ADR-0101 section 4.1).

A new Local chat turn that may change a Routine (a fresh authenticated request of a person, without files) opens one
recording. Its id is the only thing a paused turn keeps, so the same logical turn goes on recording across a person's
answer and an Integration resume in the same process, while every segment keeps its own execution token for Stop.
The recording holds the turn's Team-admitted message, its principal, Team incarnation, timezone, start, the UTC date
Brain pinned, every successful Action call as a kept occurrence, and the turn's protection: every value Team injected
into an attempt before its RPC and every string at a secret position of every result. Nothing here persists: a Team
restart leaves a resumed turn naming nothing, and its ``record`` is then unavailable. A recording ends with its logical
turn, and a Team's recordings go with the Team.
"""

from __future__ import annotations

import dataclasses
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from assistant import effect as action_effect
from routine import pin as routine_pin
from routine import trace


@dataclass(frozen=True, slots=True)
class Recording:
    """One logical recording turn of a Team."""

    recording_id: str
    team_id: str
    principal: str
    incarnation: str
    message: str
    timezone: str | None
    started_at: int
    trace: trace.Trace
    protection: trace.Protection = dataclasses.field(default_factory=trace.Protection)
    # Why nothing recorded may define a Routine any more, such as a trace past its bound; empty while it may.
    refused: str = ""


class RecordingBook:
    """Every Team's open recording turn, at most one each, as a Team runs one chat turn at a time."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._recordings: dict[str, Recording] = {}

    def start(self, team_id: str, binding: tuple[str, str], message: str, timezone: str | None, now: int) -> str:
        """Open the Team's recording turn, replacing any earlier one, and return its id."""
        principal, incarnation = binding
        recording_id = secrets.token_hex(16)
        opened = Recording(
            recording_id, team_id, principal, incarnation, message, timezone, now, trace.Trace(None, now)
        )
        with self._guard:
            self._recordings[team_id] = opened
        return recording_id

    def get(self, team_id: str, recording_id: str | None) -> Recording | None:
        """The Team's open recording with exactly this id, or None."""
        with self._guard:
            found = self._recordings.get(team_id)
        return found if found is not None and recording_id is not None and found.recording_id == recording_id else None

    def _change(self, team_id: str, recording_id: str, change: Callable[[Recording], Recording]) -> None:
        with self._guard:
            found = self._recordings.get(team_id)
            if found is not None and found.recording_id == recording_id:
                self._recordings[team_id] = change(found)

    def protect(self, team_id: str, recording_id: str, values: Iterable[str]) -> None:
        """Protect more of the turn's values; a protection past its bound is lost, and so is the recording."""
        grown = tuple(values)
        self._change(
            team_id, recording_id, lambda found: dataclasses.replace(found, protection=found.protection.grow(grown))
        )

    def occurred(self, team_id: str, recording_id: str, occurrence: trace.Occurrence) -> None:
        """Keep one successful Action call; a trace past its bound refuses the recording, never cuts it."""

        def add(found: Recording) -> Recording:
            if found.refused:
                return found
            try:
                return dataclasses.replace(found, trace=found.trace.add(occurrence))
            except trace.TraceError as exc:
                return dataclasses.replace(found, refused=exc.code)

        self._change(team_id, recording_id, add)

    def end(self, team_id: str, recording_id: str | None) -> None:
        """End the Team's recording turn once its logical turn ended."""
        with self._guard:
            found = self._recordings.get(team_id)
            if found is not None and found.recording_id == recording_id:
                del self._recordings[team_id]

    def drop(self, team_id: str) -> None:
        with self._guard:
            self._recordings.pop(team_id, None)

    def clear(self) -> None:
        with self._guard:
            self._recordings.clear()


def recorded(book: RecordingBook, recording: tuple[str, str], call: tuple, invoke: Callable[[], object]) -> object:
    """Run one Action call of a recording turn, keeping it as an occurrence when it succeeds.

    ``call`` is the active Assistant, the Action request, the attempt's evidence, and the values Team injected into it.
    Those values are protected before the RPC; the result's secret strings are protected before the result goes on.
    The Action's own errors pass through, and a failed call is never an occurrence.
    """
    team_id, recording_id = recording
    active, action_request, evidence, injected = call
    action = active.spec.actions[action_request.action]
    book.protect(team_id, recording_id, injected)
    dispatched_at = int(time.time())
    result = invoke()
    book.protect(team_id, recording_id, trace.secret_values(result, action.output_schema))
    found = book.get(team_id, recording_id)
    protected = () if found is None else tuple(found.protection.values)
    pin = routine_pin.action_pins(active.spec, (action_request.action,), routine_pin.SCOPE_LOCALE)[
        action_request.action
    ]
    occurrence = trace.Occurrence(
        evidence.operation_id,
        active.spec.assistant_id,
        action_request.action,
        pin,
        action.effect == action_effect.READ_ONLY,
        dispatched_at,
        trace.keep(dict(action_request.input), action.input_schema, protected),
        trace.keep(result, action.output_schema, protected),
    )
    book.occurred(team_id, recording_id, occurrence)
    return result
