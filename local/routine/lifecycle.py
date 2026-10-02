"""Remove a Team's Routines without residue: each run's Brain thread and journal generation, its state, diagnostics."""

from __future__ import annotations

from http import HTTPStatus

from action import journal as action_journal
from inference import client as brain_runtime_client
from local.errors import ApiProblemError as ApiProblem
from local.routine import diagnostics as routine_diagnostics
from local.routine import store as routine_store
from local.validation import routine_thread_id


def _unavailable(message: str, code: str) -> ApiProblem:
    return ApiProblem(HTTPStatus.SERVICE_UNAVAILABLE, message, code=code)


def delete_team_routines(self, team_id: str) -> None:
    """Delete every live or queued Routine run's Brain thread and journal generation, then the Team's Routine state.

    A run's generation names the network it ran in, so this works even after a crash removed the Team network. The
    Team's Routine lock is held throughout, so no transition can add a run that this cleanup would miss.
    """
    with self.routine_store.lock(team_id):
        _delete_team_routines(self, team_id)
    self.routine_proposals.drop_team(team_id)
    self.routine_human_challenges.cancel_team(team_id)


def _delete_team_routines(self, team_id: str) -> None:
    try:
        state = self.routine_store.load(team_id)
    except routine_store.RoutineStoreError as exc:
        raise _unavailable("Team Routine state is unavailable", "routine-state-unavailable") from exc
    # Live runs and the ended runs whose removal is still queued; the state that names them goes only after both.
    held = dict.fromkeys((*((run.run_id, run.generation) for run in state.runs), *state.discards))
    for run_id, generation in held:
        if not generation:
            continue
        network_id = generation.removesuffix(f":routine:{run_id}")
        try:
            self.brain_runtime.delete_thread(routine_thread_id(self.space_id, team_id, network_id, run_id))
        except brain_runtime_client.BrainRuntimeError as exc:
            raise _unavailable("Team Routine state could not be deleted", "brain-runtime-failed") from exc
        try:
            self.action_state.purge(generation)
        except action_journal.ActionJournalError as exc:
            raise _unavailable("Team Action execution state could not be deleted", "action-state-unavailable") from exc
    try:
        self.routine_store.delete(team_id)
        self.routine_diagnostics.delete(team_id)
    except (routine_store.RoutineStoreError, routine_diagnostics.DiagnosticStoreError) as exc:
        raise _unavailable("Team Routine state could not be deleted", "routine-state-unavailable") from exc


def delete_all_routines(self) -> None:
    """Delete every Team's Routines, including a Team whose network is already gone, and the Routine keyring.

    The store is exclusive for the whole reset: writes in flight finish first and every later one is refused.
    """
    self.routine_human_challenges.cancel_all()
    with self.routine_store.exclusive(), self.routine_proposals.fenced():
        try:
            teams = self.routine_store.teams()
        except routine_store.RoutineStoreError as exc:
            raise _unavailable("Team Routine state is unavailable", "routine-state-unavailable") from exc
        for team_id in teams:
            _delete_team_routines(self, team_id)
        try:
            self.routine_store.delete_all()
            self.routine_diagnostics.delete_all()
        except (routine_store.RoutineStoreError, routine_diagnostics.DiagnosticStoreError) as exc:
            raise _unavailable("Team Routine state could not be deleted", "routine-state-unavailable") from exc
