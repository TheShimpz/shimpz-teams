"""Reading and changing a Team's Routine state, failing closed with one retryable problem (ADR-0086)."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus

from local.errors import ApiProblemError as ApiProblem
from local.routine import store as routine_store
from routine import record
from routine import runs as routine_runs


def unavailable() -> ApiProblem:
    return ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE, "Team Routine state is unavailable", code="routine-state-unavailable"
    )


def load(service, team_id: str) -> record.TeamRoutines:
    try:
        return service.routine_store.load(team_id)
    except routine_store.RoutineStoreError as exc:
        raise unavailable() from exc


def reconciled[T](
    service, team_id: str, change: Callable[[record.TeamRoutines], tuple[record.TeamRoutines, T]]
) -> Callable[[record.TeamRoutines], tuple[record.TeamRoutines, T]]:
    """``change`` after every run that lost its protection is marked so, in the same write (ADR-0101 section 6).

    Every Routine state write goes through this one rule, so whatever ends a run (its worker, a person's Stop, denial,
    or deletion, or the watchdog) records the loss in that run's notice and shows nothing the run produced. It runs
    inside the write's Team lock, and a loss is marked on the run's latest sealed cursor and never undone.
    """

    def marked(state: record.TeamRoutines) -> tuple[record.TeamRoutines, T]:
        lost = service._routine_lost_runs(team_id)
        for item in state.runs:
            if item.run_id in lost and not item.protection_lost:
                state = routine_runs.lose_protection(state, item.run_id)
        return change(state)

    return marked


def update[T](service, team_id: str, change: Callable[[record.TeamRoutines], tuple[record.TeamRoutines, T]]) -> T:
    try:
        return service.routine_store.update(team_id, reconciled(service, team_id, change))
    except routine_store.RoutineStoreError as exc:
        raise unavailable() from exc


def applied(service, team_id: str, transition: Callable[[record.TeamRoutines], record.TeamRoutines]) -> bool:
    """Write ``transition`` through ``update``; False when it refused, which leaves the state as it was."""

    def change(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return transition(state), True
        except record.RoutineStateError:
            return state, False

    return update(service, team_id, change)


def call[T](action: Callable[[], T]) -> T:
    """Any other Routine store operation, such as a continuation's, failing closed the same way."""
    try:
        return action()
    except routine_store.RoutineStoreError as exc:
        raise unavailable() from exc
