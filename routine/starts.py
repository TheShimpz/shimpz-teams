"""A Team's Routine starts in any rolling 24 hours, without I/O (ADR-0092 section 9).

Each start is kept as its Routine and instant for exactly one window, so the Team-wide ceiling survives a Routine's
pause, deletion, or recreation, while each Routine's own cap counts only its own starts. At a cap, nothing starts until
the earliest start leaves the window; no Brain is asked.
"""

from __future__ import annotations

from protocol.http.v1 import routine as http_routine

WINDOW_SECONDS = 86_400
TEAM_CEILING = http_routine.MAX_DAILY_RUNS

Starts = tuple[tuple[str, int], ...]


def window(starts: Starts, now: int) -> Starts:
    """The starts still inside the 24 hours that end at ``now``, oldest first."""
    return tuple(sorted(item for item in starts if item[1] > now - WINDOW_SECONDS))


def _free_at(instants: list[int], limit: int, now: int) -> int:
    """When one more start fits under ``limit``: now, or as soon as enough of these leave the window."""
    if len(instants) < limit:
        return now
    return instants[len(instants) - limit] + WINDOW_SECONDS


def free_at(starts: Starts, routine_id: str, cap: int | None, now: int) -> int:
    """The earliest instant the Routine may start again under its own cap, when it has one, and the Team ceiling."""
    current = window(starts, now)
    own = now if cap is None else _free_at([at for item, at in current if item == routine_id], cap, now)
    return max(own, _free_at([at for _item, at in current], TEAM_CEILING, now))


def started(starts: Starts, routine_id: str, now: int) -> Starts:
    """The window after one more start of the Routine at ``now``."""
    return (*window(starts, now), (routine_id, now))
