"""A Team's Routine starts in any rolling 24 hours, without I/O (ADR-0092 section 9, ADR-0101).

Each start is kept as its Routine, its instant, and the business steps its revision may run, for exactly one window, so
the Team's daily budget survives a Routine's pause, change, deletion, or recreation, while each Routine's own cap counts
only its own starts. A start reserves every step of its revision (ADR-0092 amendment, 2026-10-05, scale): at most
``routine_plan.MAX_DAILY_STEPS`` business steps start in any window, whatever Routine, revision, or person's Rodar
started them, so the window never holds more than that many starts. At a cap, nothing starts until enough starts leave
the window; no Brain is asked.
"""

from routine import plan as routine_plan

WINDOW_SECONDS = 86_400
# Every start reserves at least one step, so the window holds at most this many (ADR-0101).
MAX_STARTS = routine_plan.MAX_DAILY_STEPS

Starts = tuple[tuple[str, int, int], ...]


def window(starts: Starts, now: int) -> Starts:
    """The starts still inside the 24 hours that end at ``now``, oldest first."""
    # Chronological by instant alone: the oldest start leaves the window first, whichever Routine made it.
    return tuple(sorted((item for item in starts if item[1] > now - WINDOW_SECONDS), key=lambda item: item[1]))


def _free_at(instants: list[int], limit: int, now: int) -> int:
    """When one more start fits under ``limit``: now, or as soon as enough of these leave the window."""
    if len(instants) < limit:
        return now
    return instants[len(instants) - limit] + WINDOW_SECONDS


def _steps_free_at(current: Starts, steps: int, now: int) -> int:
    """When a start of ``steps`` business steps fits under the Team's daily step budget beside the window's starts."""
    used = sum(item[2] for item in current)
    for _routine_id, at, reserved in current:
        if used + steps <= routine_plan.MAX_DAILY_STEPS:
            break
        used -= reserved
        now = at + WINDOW_SECONDS
    return now


def free_at(starts: Starts, routine_id: str, cap: int | None, now: int, steps: int = 1) -> int:
    """The earliest instant the Routine may start again under its own cap, when it has one, and the Team's budget."""
    current = window(starts, now)
    own = now if cap is None else _free_at([at for item, at, _steps in current if item == routine_id], cap, now)
    return max(own, _steps_free_at(current, steps, now))


def started(starts: Starts, routine_id: str, now: int, steps: int) -> Starts:
    """The window after one more start of the Routine at ``now``, reserving its revision's ``steps``."""
    return (*window(starts, now), (routine_id, now, steps))
