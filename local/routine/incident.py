"""Holding a Routine run for a person, and the lifecycle of its incident (ADR-0092 sections 5 and 7).

A compiled run seals its immutable recovery snapshot before its first dispatch: the exact cursor binding (Team
incarnation, Routine and revision, run), the authorized plan, and the user's standing request. A hold is four durable
steps: the run's live lease is fenced, its incident's compact evidence is sealed with its own copy of that snapshot,
its journal batch is archived, and the incident is indexed as the run ends. Each step is idempotent and
``reconcile`` resumes from whichever came last, so every crash window recovers without dispatching anything. An
incident is not an active run or discard work; it outlives its Routine, never expires, and holds the Routine until a
person resolves it. Because it holds its snapshot independently of the Routine record and the archived journal rows,
it can still reopen the run's cursor for verification after the Routine is deleted or Team restarts. Pular (skip)
abandons the rest of the run and permits future cycles, and only then is the settled archive marker released.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from http import HTTPStatus

from action import journal as action_journal
from core import strict_json
from local.errors import ApiProblemError as ApiProblem
from local.routine import state as routine_state
from local.routine import store as routine_store
from routine import cursor as routine_cursor
from routine import plan as routine_plan
from routine import record

VERSION = 1
_OPERATION_FIELDS = ("interrupt_id", "operation_id", "state", "attempts", "origin")


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _journal_unavailable() -> ApiProblem:
    return _problem(
        HTTPStatus.SERVICE_UNAVAILABLE, "Team Action execution state is unavailable", "action-state-unavailable"
    )


@dataclass(frozen=True, slots=True)
class Recovery:
    """What recovery of one held run is authorized to use: its cursor binding, plan, and the user's request."""

    binding: routine_cursor.Binding
    quote: str
    plan: dict[str, object]

    @property
    def plan_digest(self) -> str:
        return "sha256:" + hashlib.sha256(routine_plan.canonical(self.plan)).hexdigest()

    def document(self) -> dict[str, object]:
        binding = self.binding
        return {
            "binding": [binding.incarnation, binding.routine_id, binding.revision, binding.run_id],
            "quote": self.quote,
            "plan": self.plan,
        }


@dataclass(frozen=True, slots=True)
class OpenedRecovery:
    """A held run's snapshot and the cursor reopened under exactly its binding."""

    recovery: Recovery
    cursor: routine_cursor.Cursor


def recovery(snapshot: Recovery) -> bytes:
    """The canonical bytes of one recovery snapshot, as a run seals it before its first dispatch."""
    return routine_plan.canonical({"version": VERSION, **snapshot.document()})


def read_recovery(value: object, run_id: str) -> Recovery:
    """Admit one recovery snapshot only for exactly the run it belongs to."""
    if isinstance(value, bytes):
        try:
            value = strict_json.loads(value)
        except (UnicodeDecodeError, ValueError) as exc:
            raise routine_state.unavailable() from exc
        if not isinstance(value, dict) or value.pop("version", None) != VERSION:
            raise routine_state.unavailable()
    if not isinstance(value, dict) or set(value) != {"binding", "quote", "plan"}:
        raise routine_state.unavailable()
    binding = value["binding"]
    if not isinstance(binding, list) or len(binding) != 4:
        raise routine_state.unavailable()
    snapshot = Recovery(routine_cursor.Binding(*binding), value["quote"], value["plan"])
    if (
        not routine_cursor.binding_valid(snapshot.binding)
        or snapshot.binding.run_id != run_id
        or not isinstance(snapshot.quote, str)
        or not isinstance(snapshot.plan, dict)
    ):
        raise routine_state.unavailable()
    return snapshot


def seal_recovery(self, team_id: str, snapshot: Recovery) -> None:
    """Seal a compiled run's recovery snapshot; the executor does so before the run's first dispatch."""
    payload = recovery(snapshot)
    read_recovery(payload, snapshot.binding.run_id)
    routine_state.call(lambda: self.routine_store.put_recovery(team_id, snapshot.binding.run_id, payload))


def evidence(
    incident_id: str,
    value: record.Run,
    fingerprint: str | None,
    operations: tuple[action_journal.OperationRecord, ...],
    snapshot: Recovery | None = None,
) -> bytes:
    """The compact, result-free safety evidence of one held run's batch, with its own copy of the recovery snapshot."""
    return json.dumps(
        {
            "version": VERSION,
            "incident_id": incident_id,
            "routine_id": value.routine_id,
            "generation": value.generation,
            "fingerprint": fingerprint,
            "operations": [[getattr(item, field) for field in _OPERATION_FIELDS] for item in operations],
            "recovery": None if snapshot is None else snapshot.document(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def read_evidence(payload: bytes, incident_id: str) -> dict[str, object]:
    """Admit sealed evidence only for exactly the incident it was sealed for."""
    try:
        value = strict_json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise routine_state.unavailable() from exc
    fields = {"version", "incident_id", "routine_id", "generation", "fingerprint", "operations", "recovery"}
    if not isinstance(value, dict) or set(value) != fields or value["version"] != VERSION:
        raise routine_state.unavailable()
    if value["incident_id"] != incident_id or not isinstance(value["operations"], list):
        raise routine_state.unavailable()
    if value["recovery"] is not None:
        snapshot = read_recovery(value["recovery"], incident_id)
        if (
            snapshot.binding.routine_id != value["routine_id"]
            or record.generation_for(snapshot.binding.incarnation, incident_id) != value["generation"]
        ):
            raise routine_state.unavailable()
        value["recovery"] = snapshot
    return value


def _snapshot(self, team_id: str, value: record.Run) -> Recovery | None:
    """The run's sealed recovery snapshot, which must belong to exactly this run, or None for an uncompiled run."""
    payload = routine_state.call(lambda: self.routine_store.recovery(team_id, value.run_id))
    if payload is None:
        return None
    snapshot = read_recovery(payload, value.run_id)
    if (
        snapshot.binding.routine_id != value.routine_id
        or record.generation_for(snapshot.binding.incarnation, value.run_id) != value.generation
    ):
        raise routine_state.unavailable()
    return snapshot


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
            snapshot = _snapshot(self, team_id, value)
            sealed = evidence(run_id, value, None if current is None else current[0], operations, snapshot)
            routine_state.call(lambda: self.routine_store.put_incident(team_id, run_id, sealed))
        held = read_evidence(sealed, run_id)
        fingerprint = held["fingerprint"]
        if current is not None and current[0] != fingerprint:
            raise routine_state.unavailable()
        if fingerprint is not None:
            self.action_state.archive(value.generation, fingerprint)
    except action_journal.ActionJournalError as exc:
        raise _journal_unavailable() from exc
    now = int(time.time())

    revision = None if held["recovery"] is None else held["recovery"].binding.revision

    def settle(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return record.settle_hold(state, run_id, now, revision), True
        except record.RoutineStateError:
            return state, False

    return routine_state.update(self, team_id, settle)


def reconcile_team(self, team_id: str) -> None:
    """Release what every skipped incident still keeps, so its record may give way, then finish every held run."""
    state = routine_state.load(self, team_id)
    for item in state.incidents:
        if item.status == "skipped":
            _release(self, team_id, item)
    for value in state.runs:
        if value.status == "held":
            reconcile(self, team_id, value.run_id)


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
    """Remove what a skipped incident no longer needs, then mark it released.

    Each step is idempotent, so a crash leaves the incident skipped and the next pass retries it.
    """
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
    routine_state.update(self, team_id, lambda state: (record.release_incident(state, item.incident_id), None))


def open_recovery(self, team_id: str, incident_id: str) -> OpenedRecovery:
    """Reopen an unresolved incident's cursor under its own sealed binding, for verification or a person's choice.

    It needs neither the Routine record, which deletion removes, nor the archived journal rows; a missing snapshot,
    a binding that disagrees with the index, or a cursor of another plan fails closed.
    """
    state = routine_state.load(self, team_id)
    try:
        indexed = record.incident(state, incident_id)
    except record.RoutineStateError as exc:
        raise _problem(HTTPStatus.NOT_FOUND, "Routine incident is unavailable", "routine-incident-unavailable") from exc
    if indexed.status != "unresolved":
        raise _problem(HTTPStatus.CONFLICT, "Routine incident is not unresolved", "routine-incident-unavailable")
    sealed = routine_state.call(lambda: self.routine_store.incident(team_id, incident_id))
    if sealed is None:
        raise routine_state.unavailable()
    snapshot = read_evidence(sealed, incident_id)["recovery"]
    if snapshot is None:
        raise _problem(HTTPStatus.CONFLICT, "Routine incident cannot be verified", "routine-incident-unverifiable")
    if (snapshot.binding.routine_id, snapshot.binding.revision) != (indexed.routine_id, indexed.revision):
        raise routine_state.unavailable()
    try:
        cursor = self.routine_store.cursor(team_id, snapshot.binding)
    except routine_store.RoutineStoreError as exc:
        raise routine_state.unavailable() from exc
    if cursor is None or cursor.plan != snapshot.plan_digest:
        raise routine_state.unavailable()
    return OpenedRecovery(snapshot, cursor)


def set_paused(self, team_id: str, routine_id: str, paused: bool) -> None:
    """Pausar disables dispatch while every incident stays; resuming never bypasses an unresolved one."""

    def change(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return record.set_paused(state, routine_id, paused), True
        except record.RoutineStateError:
            return state, False

    if not routine_state.update(self, team_id, change):
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found")
