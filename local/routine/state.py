"""Reading and changing a Team's Routine state, failing closed with one retryable problem (ADR-0086)."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus

from local.errors import ApiProblemError as ApiProblem
from local.routine import store as routine_store
from routine import record


def problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def unavailable() -> ApiProblem:
    return problem(HTTPStatus.SERVICE_UNAVAILABLE, "Team Routine state is unavailable", "routine-state-unavailable")


def load(service, team_id: str) -> record.TeamRoutines:
    try:
        return service.routine_store.load(team_id)
    except routine_store.RoutineStoreError as exc:
        raise unavailable() from exc


def update[T](service, team_id: str, change: Callable[[record.TeamRoutines], tuple[record.TeamRoutines, T]]) -> T:
    try:
        return service.routine_store.update(team_id, change)
    except routine_store.RoutineStoreError as exc:
        raise unavailable() from exc


def call[T](action: Callable[[], T]) -> T:
    """Any other Routine store operation, such as a continuation's, failing closed the same way."""
    try:
        return action()
    except routine_store.RoutineStoreError as exc:
        raise unavailable() from exc
