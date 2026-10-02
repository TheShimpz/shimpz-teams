"""Holding a Routine run for a person, and the lifecycle of its incident (ADR-0092 sections 5 and 7).

A hold is four durable steps: the run's live lease is fenced, its incident's compact evidence is sealed, its journal
batch is archived, and the incident is indexed as the run ends. Each step is idempotent and ``reconcile`` resumes from
whichever came last, so every crash window recovers without dispatching anything. An incident is not an active run or
discard work; it outlives its Routine, never expires, and holds the Routine until a person resolves it. Pular (skip)
abandons the rest of the run and permits future cycles, and only then is the settled archive marker released.
"""

from __future__ import annotations

import json
import time
from http import HTTPStatus

from action import journal as action_journal
from core import strict_json
from local.errors import ApiProblemError as ApiProblem
from local.routine import state as routine_state
from routine import record

VERSION = 1
_OPERATION_FIELDS = ("interrupt_id", "operation_id", "state", "attempts", "origin")


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _journal_unavailable() -> ApiProblem:
    return _problem(
        HTTPStatus.SERVICE_UNAVAILABLE, "Team Action execution state is unavailable", "action-state-unavailable"
    )


def evidence(
    incident_id: str, value: record.Run, fingerprint: str | None, operations: tuple[action_journal.OperationRecord, ...]
) -> bytes:
    """The compact, result-free safety evidence of one held run's batch: which operations may have acted."""
    return json.dumps(
        {
            "version": VERSION,
            "incident_id": incident_id,
            "routine_id": value.routine_id,
            "generation": value.generation,
            "fingerprint": fingerprint,
            "operations": [[getattr(item, field) for field in _OPERATION_FIELDS] for item in operations],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def read_evidence(payload: bytes, incident_id: str) -> dict[str, object]:
    """Admit sealed evidence only for exactly the incident it was sealed for."""
    try:
        value = strict_json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise routine_state.unavailable() from exc
    fields = {"version", "incident_id", "routine_id", "generation", "fingerprint", "operations"}
    if not isinstance(value, dict) or set(value) != fields or value["version"] != VERSION:
        raise routine_state.unavailable()
    if value["incident_id"] != incident_id or not isinstance(value["operations"], list):
        raise routine_state.unavailable()
    return value


def hold(self, team_id: str, run_id: str, lease: record.Lease) -> None:
    """Fence a leased run's live lease, then hold it until its incident is durable and indexed."""
    now = int(time.time())

    def fence(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return record.fence(state, run_id, lease, now), True
        except record.RoutineStateError:
            return state, False

    if not routine_state.update(self, team_id, fence):
        raise _problem(HTTPStatus.CONFLICT, "Routine run lease is not live", "routine-lease-invalid")
    reconcile(self, team_id, run_id)


def reconcile(self, team_id: str, run_id: str) -> bool:
    """Finish one held run's hold from its last durable step; False when the run is no longer held."""
    value = next((item for item in routine_state.load(self, team_id).runs if item.run_id == run_id), None)
    if value is None or value.status != "held":
        return False
    try:
        current = self.action_state.current_batch(value.generation)
        sealed = routine_state.call(lambda: self.routine_store.incident(team_id, run_id))
        if sealed is None:
            if current is not None and current[1] == "archived":
                # Archiving happens only after the evidence is durable, so this is lost evidence: never guess it.
                raise routine_state.unavailable()
            operations = () if current is None else self.action_state.snapshot(value.generation, current[0])
            sealed = evidence(run_id, value, None if current is None else current[0], operations)
            routine_state.call(lambda: self.routine_store.put_incident(team_id, run_id, sealed))
        fingerprint = read_evidence(sealed, run_id)["fingerprint"]
        if current is not None and current[0] != fingerprint:
            raise routine_state.unavailable()
        if fingerprint is not None:
            self.action_state.archive(value.generation, fingerprint)
    except action_journal.ActionJournalError as exc:
        raise _journal_unavailable() from exc
    now = int(time.time())

    def settle(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return record.settle_hold(state, run_id, now), True
        except record.RoutineStateError:
            return state, False

    return routine_state.update(self, team_id, settle)


def reconcile_team(self, team_id: str) -> None:
    """Finish every held run's hold, then release what every skipped incident still keeps."""
    state = routine_state.load(self, team_id)
    for value in state.runs:
        if value.status == "held":
            reconcile(self, team_id, value.run_id)
    for item in state.incidents:
        if item.status == "skipped":
            _release(self, team_id, item)


def skip(self, team_id: str, incident_id: str) -> None:
    """Pular: the incident stops holding its Routine, then its cursor, evidence, and archive marker are released.

    It never replays or fabricates output, and never recreates a deleted Routine; any effect the run may have had
    stays unresolved, which the person was told.
    """

    def mark(state: record.TeamRoutines) -> tuple[record.TeamRoutines, record.Incident | None]:
        try:
            skipped = record.skip_incident(state, incident_id)
        except record.RoutineStateError:
            return state, None
        return skipped, record.incident(skipped, incident_id)

    skipped = routine_state.update(self, team_id, mark)
    if skipped is None:
        raise _problem(HTTPStatus.CONFLICT, "Routine incident is not unresolved", "routine-incident-unavailable")
    _release(self, team_id, skipped)


def _release(self, team_id: str, item: record.Incident) -> None:
    """Remove what a skipped incident no longer needs; each removal is idempotent, so a crash is retried."""
    sealed = routine_state.call(lambda: self.routine_store.incident(team_id, item.incident_id))
    if sealed is not None:
        fingerprint = read_evidence(sealed, item.incident_id)["fingerprint"]
        if fingerprint is not None:
            try:
                self.action_state.release_archive(item.generation, fingerprint)
            except action_journal.ActionJournalError as exc:
                raise _journal_unavailable() from exc
    routine_state.call(lambda: self.routine_store.delete_cursor(team_id, item.incident_id))
    routine_state.call(lambda: self.routine_store.delete_incident(team_id, item.incident_id))


def set_paused(self, team_id: str, routine_id: str, paused: bool) -> None:
    """Pausar disables dispatch while every incident stays; resuming never bypasses an unresolved one."""

    def change(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return record.set_paused(state, routine_id, paused), True
        except record.RoutineStateError:
            return state, False

    if not routine_state.update(self, team_id, change):
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found")
