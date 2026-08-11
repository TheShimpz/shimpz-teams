"""Hosted chat cleanup when an authorized Team lifecycle changes."""

from http import HTTPStatus

from action import journal as action_journal
from hosted import state as runtime_state


def cancel_replayable_human(team_id: str, generation: str) -> bool:
    """Cancel a pending human gate and remove only safely replayable Action state."""
    if not runtime_state._human_challenges.cancel_team(team_id):
        return False
    try:
        runtime_state._action_execution_journal().purge_replayable(generation)
    except action_journal.ActionJournalError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team Action execution state is unavailable",
        ) from exc
    return True
