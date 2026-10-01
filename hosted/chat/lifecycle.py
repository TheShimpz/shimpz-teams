"""Hosted chat cleanup when an authorized Team lifecycle changes."""

from http import HTTPStatus

from action import journal as action_journal
from hosted import state as runtime_state


def cancel_replayable_human(team_id: str, generation: str) -> bool:
    """Cancel a pending human gate and end only settled Action state; uncertain work stays."""
    if not runtime_state._human_challenges.cancel_team(team_id):
        return False
    try:
        runtime_state._action_execution_journal().end_settled(generation)
    except action_journal.ActionJournalError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team Action execution state is unavailable",
        ) from exc
    return True
