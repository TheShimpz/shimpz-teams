"""A Local Supervisor lists and deletes a Team's Routines (ADR-0086, ADR-0092)."""

from __future__ import annotations

import datetime
import time
from http import HTTPStatus

from action import journal as action_journal
from local.errors import ApiProblemError as ApiProblem
from local.routine import diagnostics as routine_diagnostics
from local.routine import incident as routine_incident
from local.routine import state as routine_state
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from routine import definition as routine_definition
from routine import hold as routine_hold
from routine import record


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _instant(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).isoformat().replace("+00:00", "Z")


def routine_view(value: record.Routine) -> dict[str, object]:
    """A Routine as its Supervisor inspects it: what it is, when it runs, its plan's summary, and its output.

    Its steps are read page by page (``routine_steps``), bound to the summary's revision.
    """
    return {
        "routine_id": value.routine_id,
        "name": value.name,
        "plan": routine_definition.summary(value.plan, value.revision),
        "output": routine_definition.disposition(value.plan),
        "schedule": dict(value.schedule),
        "timezone": value.timezone,
        "assistant_ids": [assistant for assistant, _digest in value.assistants],
        "next_run_at": _instant(value.next_run_at),
        "needs_reconfirm": value.needs_reconfirm,
        "deleting": value.deleting,
        "permissions_revision": value.permissions_revision,
        **routine_definition.scope(value),
    }


def run_view(value: record.Run) -> dict[str, object]:
    """What a Supervisor may act on: a frozen run's request; a leased or held run only shows that it is live."""
    return {
        "run_id": value.run_id,
        "routine_id": value.routine_id,
        "status": value.status,
        "scheduled_at": _instant(value.scheduled_at),
        "request_kind": value.request_kind or None,
        "assistant_id": value.assistant_id or None,
        "action": value.action or None,
        "position": value.position,
        "steps": value.steps if value.position is not None else None,
    }


def incident_view(value: record.Incident) -> dict[str, object]:
    """An unresolved incident a recovery card settles: its Routine and the call whose effect is unknown."""
    return {
        "incident_id": value.incident_id,
        "routine_id": value.routine_id,
        "name": value.name,
        "created_at": _instant(value.created_at),
        **routine_hold.step_detail(routine_hold.held_step(value)),
    }


def routine_steps(self, team_id: str, routine_id: str, revision: int, offset: int) -> dict[str, object]:
    """One page of a Routine's current plan from ``offset``, only for the revision the reader names.

    A revision that is no longer current is refused, so a reader never combines two revisions' steps (ADR-0092
    amendment, 2026-10-05, scale).
    """
    team_id = validate_team_id(team_id)
    state = routine_state.load(self, team_id)
    try:
        value = record.routine(state, routine_id)
    except record.RoutineStateError as exc:
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found") from exc
    if value.deleting:
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found")
    if value.revision != revision:
        raise _problem(HTTPStatus.CONFLICT, "Routine revision changed", "routine-revision-changed")
    if not 0 <= offset < len(value.plan["steps"]):
        raise _problem(HTTPStatus.NOT_FOUND, "Routine steps are unavailable", "routine-steps-not-found")
    return routine_definition.page(value.routine_id, value.revision, value.plan, value.permitted, offset)


def list_routines(self, team_id: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    state = routine_state.load(self, team_id)
    return {
        "team_id": team_id,
        "routines": [routine_view(item) for item in state.routines],
        "runs": [run_view(item) for item in state.runs],
        "incidents": [incident_view(item) for item in state.incidents if item.status == "unresolved"],
    }


def _snapshot(self, team_id: str, run_id: str) -> routine_incident.Recovery | None:
    """An ended run's sealed recovery snapshot, or None when it has none or it cannot be read."""
    try:
        sealed = routine_state.call(lambda: self.routine_store.recovery(team_id, run_id))
        return None if sealed is None else routine_incident.read_recovery(sealed, run_id)
    except ApiProblem:
        return None


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
    if not incident:
        # The run ended for good: its terminal record proves which steps never started, before its cursor goes.
        routine_incident.seal_terminal(self, team_id, run_id, _snapshot(self, team_id, run_id))
    routine_state.call(lambda: self.routine_store.delete_continuation(team_id, run_id))
    # An incident keeps its own sealed copy of the recovery snapshot.
    routine_state.call(lambda: self.routine_store.delete_recovery(team_id, run_id))
    if not incident:
        routine_state.call(lambda: self.routine_store.delete_cursor(team_id, run_id))
        routine_state.call(lambda: self.routine_store.delete_incident(team_id, run_id))
        # Nothing of the run can show anything any more, so its protection goes with it.
        self.routine_protections.drop(run_id)


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
    """Delete a Routine: stop its running run, end a frozen one, and set every held run of it aside.

    Marking the Routine deleting, reading its runs, and setting its unresolved incidents aside is one write, and a
    deleting Routine never resumes a run, so each frozen run seen here stays frozen until it is ended, and a run held
    later is set aside as it is indexed. A running segment, verification, or recovery is stopped and ends itself; what
    a set-aside incident kept is released once nothing executes for it. A Routine already gone is deleted.
    """
    team_id = validate_team_id(team_id)
    if not isinstance(routine_id, str) or http_routine.ROUTINE_ID_RE.fullmatch(routine_id) is None:
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found")
    now = int(time.time())

    def begin(state: record.TeamRoutines) -> tuple[record.TeamRoutines, tuple[tuple[record.Run, ...], tuple[str, ...]]]:
        if not any(item.routine_id == routine_id for item in state.routines):
            return state, ((), ())
        state, runs = record.begin_delete(state, routine_id)
        held = tuple(
            item.incident_id
            for item in state.incidents
            if item.routine_id == routine_id and item.status == "unresolved"
        )
        for incident_id in held:
            state = routine_hold.skip_incident(state, incident_id, now, choice="delete")
        return state, (runs, held)

    runs, held = routine_state.update(self, team_id, begin)
    # No card of a deleting Routine can be answered any more, nor a proposal that would replace it.
    self.routine_cards.drop_routine(team_id, routine_id)
    self.routine_proposals.drop_routine(team_id, routine_id)
    for value in runs:
        if value.status == "leased":
            self._stop_routine_run(team_id, value.run_id)
        elif value.status == "frozen":
            # Serialized with an opening by the Team lifecycle lock, as Stop is: no challenge outlives the run.
            with self._lock(team_id):
                self._cancel_routine_challenge(team_id, value.run_id)
                end_frozen(self, team_id, value.run_id, "stopped", {"actions": []})
        # A held run is set aside as its incident is indexed; that ending completes the deletion.
    for incident_id in held:
        # A verification, recovery episode, or Recriar still in progress is stopped; it changes nothing after this.
        self._stop_routine_run(team_id, incident_id)
    for item in routine_state.load(self, team_id).incidents:
        if item.incident_id in held and item.status == "skipped":
            routine_incident.settled(self, team_id, item)
    return {"team_id": team_id, "routine_id": routine_id, "deleted": settle(self, team_id, routine_id)}


def complete_deletion(self, team_id: str, routine_id: str) -> bool:
    """Remove a deleting Routine once none of its runs remains and its set-aside runs are released; False until then.

    Its diagnostic bodies go first, while the Routine is still listed as deleting, so a failure keeps it as the
    watchdog's retry target and never leaves residue behind a removed record. The write that removes it publishes its
    ``deleted`` notice, which outlives it.
    """
    state = routine_state.load(self, team_id)
    value = next((item for item in state.routines if item.routine_id == routine_id), None)
    if value is not None and (not value.deleting or any(item.routine_id == routine_id for item in state.runs)):
        return False
    # A set-aside run still releasing what it kept, such as a stopped recovery unwinding, keeps the deletion going.
    if any(item.routine_id == routine_id and item.status == "skipped" for item in state.incidents):
        return False
    # A Routine already gone keeps nothing either: any residue a failed earlier attempt left is removed again.
    try:
        self.routine_diagnostics.delete_routine(team_id, routine_id)
    except routine_diagnostics.DiagnosticStoreError as exc:
        raise routine_state.unavailable() from exc
    now = int(time.time())

    def complete(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        if not any(item.routine_id == routine_id for item in state.routines):
            return state, True
        try:
            return record.complete_delete(state, routine_id, now), True
        except record.RoutineStateError:
            return state, False

    return routine_state.update(self, team_id, complete)
