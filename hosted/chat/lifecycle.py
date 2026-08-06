"""Hosted chat cleanup when an authorized Team lifecycle changes."""

from http import HTTPStatus

from hosted import state as runtime_state
from power import journal as power_journal


def cancel_replayable_human(team_id: str, generation: str) -> bool:
    """Cancel a pending human gate and remove only safely replayable Power state."""
    if not runtime_state._human_challenges.cancel_team(team_id):
        return False
    try:
        runtime_state._power_execution_journal().purge_replayable(generation)
    except power_journal.PowerJournalError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team Power execution state is unavailable",
        ) from exc
    return True
