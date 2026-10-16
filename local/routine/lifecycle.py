"""Remove a Team's Routines without residue: each run's journal generation, its state, and its diagnostics."""

from action import journal as action_journal
from local import errors as local_errors
from local.routine import diagnostics as routine_diagnostics
from local.routine import store as routine_store


def delete_team_routines(self, team_id: str) -> None:
    """Delete every live or queued Routine run's journal generation, then the Team's Routine state.

    A run's generation names the network it ran in, so this works even after a crash removed the Team network. The
    Team's Routine lock is held throughout, so no transition can add a run that this cleanup would miss.
    """
    with self.routine_store.lock(team_id):
        _delete_team_routines(self, team_id)
    self.routine_human_challenges.cancel_team(team_id)
    self.routine_proposals.drop(team_id)
    self.routine_recordings.drop(team_id)


def _delete_team_routines(self, team_id: str) -> None:
    try:
        state = self.routine_store.load(team_id)
    except routine_store.RoutineStoreError as exc:
        raise local_errors.routine_state_unavailable() from exc
    # Live and held runs and the ended runs whose removal is still queued; the state that names them goes only after.
    held = dict.fromkeys((*((run.run_id, run.generation) for run in state.runs), *state.discards))
    for _run_id, generation in held:
        if not generation:
            continue
        try:
            self.action_state.purge(generation)
        except action_journal.ActionJournalError as exc:
            raise local_errors.action_state_unavailable() from exc
    # The values each run protected, such as its Stored Inputs, leave memory before the state naming the runs goes,
    # so a cleanup that fails and is retried can never strand them.
    for run_id, _generation in held:
        self.routine_protections.drop(run_id)
    try:
        self.routine_store.delete(team_id)
        self.routine_diagnostics.delete(team_id)
    except (routine_store.RoutineStoreError, routine_diagnostics.DiagnosticStoreError) as exc:
        raise local_errors.routine_state_delete_failed() from exc


def delete_all_routines(self) -> None:
    """Delete every Team's Routines, including a Team whose network is already gone, and the Routine keyring.

    The store is exclusive for the whole reset: writes in flight finish first and every later one is refused.
    """
    self.routine_human_challenges.cancel_all()
    self.routine_proposals.clear()
    self.routine_recordings.clear()
    self.routine_protections.clear()
    with self.routine_store.exclusive():
        try:
            teams = self.routine_store.teams()
        except routine_store.RoutineStoreError as exc:
            raise local_errors.routine_state_unavailable() from exc
        for team_id in teams:
            _delete_team_routines(self, team_id)
        try:
            self.routine_store.delete_all()
            self.routine_diagnostics.delete_all()
        except (routine_store.RoutineStoreError, routine_diagnostics.DiagnosticStoreError) as exc:
            raise local_errors.routine_state_delete_failed() from exc
