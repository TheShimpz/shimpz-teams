"""What a Local chat turn knows about the Team's Routines, and what it does with a proposed change (ADR-0086)."""

from __future__ import annotations

from http import HTTPStatus

from local.errors import ApiProblemError as ApiProblem
from local.routine import store as routine_store


def chat_routines(self, team_id: str) -> tuple[dict[str, object], ...]:
    """The Team's Routines as data for the Brain, so the user can name one to cancel; a deleting one is gone."""
    try:
        state = self.routine_store.load(team_id)
    except routine_store.RoutineStoreError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE, "Team Routine state is unavailable", code="routine-state-unavailable"
        ) from exc
    return tuple(
        {"routine_id": item.routine_id, "quote": item.quote, "schedule": dict(item.schedule), "timezone": item.timezone}
        for item in state.routines
        if not item.deleting
    )
