"""A Local Supervisor lists and deletes a Team's Routines (ADR-0086, ADR-0092)."""

from __future__ import annotations

import datetime
import time
from http import HTTPStatus

from action import journal as action_journal
from local.errors import ApiProblemError as ApiProblem
from local.routine import diagnostics as routine_diagnostics
from local.routine import state as routine_state
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from routine import grant as routine_grant
from routine import record


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _instant(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).isoformat().replace("+00:00", "Z")


def routine_view(value: record.Routine) -> dict[str, object]:
    """A Routine as its Supervisor inspects it: what it is, when it runs, and its plan's safe projection."""
    return {
        "routine_id": value.routine_id,
        "name": value.name,
        "quote": value.quote,
        "steps": routine_grant.steps(value.plan, value.grant),
        "schedule": dict(value.schedule),
        "timezone": value.timezone,
        "assistant_ids": [assistant for assistant, _digest in value.assistants],
        "next_run_at": _instant(value.next_run_at),
        "needs_reconfirm": value.needs_reconfirm,
        "deleting": value.deleting,
    }


def run_view(value: record.Run) -> dict[str, object]:
    """What a Supervisor may act on: a frozen run's request, or an uncertain run's exact batch to resolve."""
    return {
        "run_id": value.run_id,
        "routine_id": value.routine_id,
        "status": value.status,
        "scheduled_at": _instant(value.scheduled_at),
        "request_kind": value.request_kind or None,
        "assistant_id": value.assistant_id or None,
        "action": value.action or None,
        "batch_fingerprint": value.batch[1] or None,
        "actions": [list(pair) for pair in value.held_actions],
    }


def list_routines(self, team_id: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    state = routine_state.load(self, team_id)
    return {
        "team_id": team_id,
        "routines": [routine_view(item) for item in state.routines],
        "runs": [run_view(item) for item in state.runs],
    }


def _discard(self, team_id: str, run_id: str, generation: str, *, incident: bool, live: bool) -> None:
    """Remove one ended run's journal generation, continuation, and cursor; each removal is idempotent.

    An archive marker, cursor, and evidence an unresolved incident still needs stay (ADR-0092). A generation a resumed
    run left behind goes whole, archive marker included, while the files the live continuation shares stay.
    """
    if generation:
        try:
            if incident:
                self.action_state.discard(generation)
            else:
                self.action_state.purge(generation)
        except action_journal.ActionJournalError as exc:
            raise _problem(
                HTTPStatus.SERVICE_UNAVAILABLE, "Routine run state could not be removed", "routine-state-unavailable"
            ) from exc
    if live:
        return
    routine_state.call(lambda: self.routine_store.delete_continuation(team_id, run_id))
    # An incident keeps its own sealed copy of the recovery snapshot.
    routine_state.call(lambda: self.routine_store.delete_recovery(team_id, run_id))
    if not incident:
        routine_state.call(lambda: self.routine_store.delete_cursor(team_id, run_id))
        routine_state.call(lambda: self.routine_store.delete_incident(team_id, run_id))


def drain(self, team_id: str) -> None:
    """Remove what the Team's ended runs still hold; a failure keeps the rest queued for the next drain.

    A run's end queues its removal in the same write that ends it, so nothing it held is lost to a crash, and nothing
    is removed while the run could still resume.
    """
    state = routine_state.load(self, team_id)
    incidents = {item.incident_id for item in state.incidents if item.status != "released"}
    live = {item.run_id for item in state.runs}
    for run_id, generation in state.discards:
        _discard(self, team_id, run_id, generation, incident=run_id in incidents, live=run_id in live)
        routine_state.update(
            self, team_id, lambda state, run=run_id, held=generation: (record.discarded(state, run, held), None)
        )


def settle(self, team_id: str, routine_id: str) -> bool:
    """After a run ended: remove what it held, then finish a deletion it was blocking."""
    drain(self, team_id)
    return complete_deletion(self, team_id, routine_id)


def settle_team(self, team_id: str) -> None:
    """Retry every pending removal and deletion of a Team, as the watchdog does after a failure or a restart."""
    drain(self, team_id)
    for item in routine_state.load(self, team_id).routines:
        if item.deleting:
            complete_deletion(self, team_id, item.routine_id)


def end_frozen(self, team_id: str, run_id: str, outcome: str, detail: dict[str, object]) -> bool:
    """End a run only while it is still frozen; False when a replay or another ending reached it first."""
    now = int(time.time())

    def end(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return record.end(state, run_id, now, outcome, detail, status="frozen"), True
        except record.RoutineStateError:
            return state, False

    return routine_state.update(self, team_id, end)


def delete_routine(self, team_id: str, routine_id: object) -> dict[str, object]:
    """Delete a Routine: stop its running run and end a frozen one; an uncertain run must be resolved first.

    Marking the Routine deleting and reading its runs is one write, and a deleting Routine never resumes a run, so each
    frozen run seen here stays frozen until it is ended. A running segment is stopped and ends itself, and its end
    completes the deletion.
    """
    team_id = validate_team_id(team_id)
    if not isinstance(routine_id, str) or http_routine.ROUTINE_ID_RE.fullmatch(routine_id) is None:
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found")

    def begin(state: record.TeamRoutines) -> tuple[record.TeamRoutines, tuple[record.Run, ...] | str]:
        try:
            return record.begin_delete(state, routine_id)
        except record.RoutineStateError as exc:
            return state, str(exc)

    runs = routine_state.update(self, team_id, begin)
    if runs == "routine-run-uncertain":
        raise _problem(HTTPStatus.CONFLICT, "Resolve the Routine's uncertain run first", "routine-run-uncertain")
    if isinstance(runs, str):
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found")
    for value in runs:
        if value.status == "leased":
            self._stop_routine_run(team_id, value.run_id)
        elif value.status == "frozen":
            self._cancel_routine_challenge(team_id, value.run_id)
            end_frozen(self, team_id, value.run_id, "stopped", {"actions": []})
        # A held run settles into its incident, which outlives the Routine; that ending completes the deletion.
    return {"team_id": team_id, "routine_id": routine_id, "deleted": settle(self, team_id, routine_id)}


def complete_deletion(self, team_id: str, routine_id: str) -> bool:
    """Remove a deleting Routine once none of its runs remains, then its diagnostic bodies; False while a run ends."""

    def complete(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        value = (
            record.routine(state, routine_id) if any(item.routine_id == routine_id for item in state.routines) else None
        )
        if value is None or not value.deleting:
            return state, value is None
        try:
            return record.complete_delete(state, routine_id), True
        except record.RoutineStateError:
            return state, False

    if not routine_state.update(self, team_id, complete):
        return False
    try:
        self.routine_diagnostics.delete_routine(team_id, routine_id)
    except routine_diagnostics.DiagnosticStoreError as exc:
        raise routine_state.unavailable() from exc
    return True
