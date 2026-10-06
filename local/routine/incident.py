"""Holding a Routine run for a person, and the lifecycle of its incident (ADR-0092 sections 5 and 7).

A run seals its immutable recovery snapshot before its first dispatch: the exact cursor binding (Team incarnation,
Routine and revision, run), the confirmed plan, and the Routine's name (ADR-0101). A hold is four durable steps: the
run's live lease is fenced, its incident's compact evidence is sealed with its own copy of that snapshot, its journal
batch is archived, and the incident is indexed as the run ends. Each step is idempotent
and ``reconcile`` resumes from whichever came last, so every crash window recovers without dispatching anything. An
incident is not an active run or discard work; it outlives its Routine, never expires, and holds the Routine until a
person resolves it. Because it holds its snapshot independently of the Routine record and the archived journal rows, it
can still reopen the run's cursor for verification after Team restarts. A person sets the run aside through its card's
Rodar, or by deleting its Routine; only then, once nothing executes for it any more, are its cursor, evidence, and
archive marker released.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from action import journal as action_journal
from local import audit as local_audit
from local.errors import ApiProblemError as ApiProblem
from local.routine import diagnostics as routine_diagnostics
from local.routine import state as routine_state
from local.routine import store as routine_store
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import strict_json
from routine import cursor as routine_cursor
from routine import hold as routine_hold
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
    """What recovery of one held run is authorized to use: its cursor binding, plan, and its Routine's name."""

    binding: routine_cursor.Binding
    name: str
    plan: dict[str, object]

    @property
    def plan_digest(self) -> str:
        return "sha256:" + hashlib.sha256(routine_plan.canonical(self.plan)).hexdigest()

    def document(self) -> dict[str, object]:
        binding = self.binding
        return {
            "binding": [binding.incarnation, binding.routine_id, binding.revision, binding.run_id],
            "name": self.name,
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
    if not isinstance(value, dict) or set(value) != {"binding", "name", "plan"}:
        raise routine_state.unavailable()
    binding = value["binding"]
    if not isinstance(binding, list) or len(binding) != 4:
        raise routine_state.unavailable()
    snapshot = Recovery(routine_cursor.Binding(*binding), value["name"], value["plan"])
    if (
        not routine_cursor.binding_valid(snapshot.binding)
        or snapshot.binding.run_id != run_id
        or http_routine.canonical_name(snapshot.name) != snapshot.name
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
            or record.network_of(value["generation"], incident_id) != snapshot.binding.incarnation
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
        or record.network_of(value.generation, value.run_id) != snapshot.binding.incarnation
    ):
        raise routine_state.unavailable()
    return snapshot


def hold(self, team_id: str, run_id: str, lease: record.Lease) -> None:
    """Fence a leased run's live lease, then hold it until its incident is durable and indexed."""
    now = int(time.time())

    def fence(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return routine_hold.fence(state, run_id, lease, now), True
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
        if sealed is not None and read_evidence(sealed, run_id)["generation"] != value.generation:
            # Evidence of an earlier hold this run resumed from; its generation is gone, and this hold seals its own.
            sealed = None
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

    snapshot = held["recovery"]
    revision = None if snapshot is None else snapshot.binding.revision
    step = _held_step(self, team_id, snapshot)

    def settle(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return routine_hold.settle_hold(state, run_id, now, revision, step), True
        except record.RoutineStateError:
            return state, False

    return routine_state.update(self, team_id, settle)


def _held_step(self, team_id: str, snapshot: Recovery | None) -> routine_hold.HeldStep:
    """The Assistant Action the held run's sealed cursor stopped at; none when no cursor is sealed or readable.

    It only labels the run's notice, so a cursor that cannot be read never keeps the incident from being indexed.
    """
    if snapshot is None:
        return routine_hold.UNKNOWN_STEP
    try:
        cursor = self.routine_store.cursor(team_id, snapshot.binding)
    except routine_store.RoutineStoreError:
        return routine_hold.UNKNOWN_STEP
    if cursor is None:
        return routine_hold.UNKNOWN_STEP
    return held_call(cursor, snapshot.plan["steps"])


def held_call(cursor: routine_cursor.Cursor, steps: list) -> routine_hold.HeldStep:
    """The call a sealed cursor stopped at: the last decision call, or the current replay step; none when neither."""
    if cursor.calls:
        # A decision call held the run: its last call is the one whose effect is unresolved.
        call = cursor.calls[-1]
        return call.assistant, call.action, {"phase": "decision", "call": len(cursor.calls)}, len(steps)
    if not steps:
        return routine_hold.UNKNOWN_STEP
    index = min(cursor.step, len(steps) - 1)
    return steps[index]["assistant"], steps[index]["action"], {"phase": "replay", "step": index + 1}, len(steps)


def reconcile_team(self, team_id: str) -> None:
    """Release what every set-aside incident still keeps once nothing executes for it, then finish every held run."""
    state = routine_state.load(self, team_id)
    for item in state.incidents:
        if item.status == "skipped":
            release(self, team_id, item)
    for value in state.runs:
        if value.status == "held":
            reconcile(self, team_id, value.run_id)


def _transition_problem(code: str) -> ApiProblem:
    """A refused transition, with what refused it: a stale card, a Team admission limit, or an incident gone."""
    if code in {"incident-changed", "routine-revision-changed"}:
        return _problem(HTTPStatus.CONFLICT, "the recovery card is stale; open it again", "routine-card-stale")
    if code.startswith("routine-") or code == "notices-full":
        return _problem(HTTPStatus.CONFLICT, "the Team cannot hold this Routine change", code)
    return _problem(HTTPStatus.CONFLICT, "Routine incident is not unresolved", "routine-incident-unavailable")


def set_aside(
    self,
    team_id: str,
    incident_id: str,
    change: Callable[[record.TeamRoutines], record.TeamRoutines],
) -> record.Incident:
    """Apply a person's transition that sets the incident aside, then release what it kept.

    The transition checks the card's state in its own write and never replays or fabricates output; any effect the run
    may have had stays unresolved, which the person was told. Once it committed, a failed release only waits for the
    watchdog's next pass: it never reads as nothing having changed.
    """

    def mark(state: record.TeamRoutines) -> tuple[record.TeamRoutines, record.Incident | str]:
        try:
            changed = change(state)
        except record.RoutineStateError as exc:
            return state, str(exc)
        return changed, routine_hold.incident(changed, incident_id)

    skipped = routine_state.update(self, team_id, mark)
    if isinstance(skipped, str):
        raise _transition_problem(skipped)
    settled(self, team_id, skipped)
    return skipped


def executing(self, incident_id: str) -> bool:
    """Whether a verification, recovery episode, or person's answer is still registered for the incident."""
    with self._active_chat_guard:
        return incident_id in self._routine_runs


def release(self, team_id: str, item: record.Incident) -> None:
    """Release a set-aside incident unless something still executes for it; the watchdog's next pass retries."""
    if not executing(self, item.incident_id):
        _release(self, team_id, item)


def settled(self, team_id: str, item: record.Incident) -> None:
    """After a person's committed transition: release now if possible, and otherwise leave it to the watchdog."""
    with contextlib.suppress(ApiProblem):
        release(self, team_id, item)


def _release(self, team_id: str, item: record.Incident) -> None:
    """Remove what a skipped incident no longer needs, then mark it released.

    Each step is idempotent, so a crash leaves the incident skipped and the next pass retries it.
    """
    sealed = routine_state.call(lambda: self.routine_store.incident(team_id, item.incident_id))
    if sealed is not None:
        held = read_evidence(sealed, item.incident_id)
        seal_terminal(self, team_id, item.incident_id, held["recovery"])
        fingerprint = held["fingerprint"]
        if fingerprint is not None:
            try:
                self.action_state.release_archive(item.generation, fingerprint)
            except action_journal.ActionJournalError as exc:
                raise _journal_unavailable() from exc
    routine_state.call(lambda: self.routine_store.delete_cursor(team_id, item.incident_id))
    routine_state.call(lambda: self.routine_store.delete_incident(team_id, item.incident_id))
    routine_state.update(self, team_id, lambda state: (routine_hold.release_incident(state, item.incident_id), None))
    self.routine_cards.discard(team_id, item.incident_id)


def seal_terminal(self, team_id: str, run_id: str, snapshot: Recovery | None) -> None:
    """Seal an ended run's terminal record from its sealed snapshot and cursor, before they are removed.

    It proves which steps never started (ADR-0092 amendment, 2026-10-05, scale). It is a display record: when the
    snapshot or cursor cannot prove anything, none is kept, and a failure to keep one is audited only.
    """
    if snapshot is None or snapshot.binding.run_id != run_id:
        return
    try:
        cursor = self.routine_store.cursor(team_id, snapshot.binding)
    except routine_store.RoutineStoreError:
        cursor = None
    if cursor is None or cursor.plan != snapshot.plan_digest:
        return
    binding = snapshot.binding
    run = routine_diagnostics.RunBinding(
        binding.routine_id, run_id, binding.revision, snapshot.plan_digest, len(snapshot.plan["steps"])
    )
    terminal = routine_diagnostics.RunRecord(
        run, cursor.step, cursor.operation_id is not None, int(time.time()), calls=len(cursor.calls)
    )
    try:
        self.routine_diagnostics.record_run(team_id, binding.incarnation, terminal)
    except routine_diagnostics.DiagnosticStoreError:
        local_audit.record_request("routine-run-record", result="error", team_id=team_id, detail=run_id)


def open_recovery(self, team_id: str, incident_id: str) -> OpenedRecovery:
    """Reopen an unresolved incident's cursor under its own sealed binding, for verification or a person's choice.

    It needs neither the Routine record, which deletion removes, nor the archived journal rows; a missing snapshot,
    a binding that disagrees with the index, or a cursor of another plan fails closed.
    """
    state = routine_state.load(self, team_id)
    try:
        indexed = routine_hold.incident(state, incident_id)
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


def pause(self, team_id: str, incident_id: str, reason: str) -> None:
    """Recovery pauses the Routine an unresolved incident holds, and says why on the held run's notice."""

    def change(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str | None]:
        try:
            return routine_hold.pause_incident(state, incident_id, int(time.time()), reason), None
        except record.RoutineStateError as exc:
            return state, str(exc)

    refused = routine_state.update(self, team_id, change)
    if refused is not None:
        raise _transition_problem(refused)


def set_paused(self, team_id: str, routine_id: str, paused: bool) -> None:
    """Pausing disables dispatch while every incident stays; resuming never bypasses an unresolved one."""

    def change(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return record.set_paused(state, routine_id, paused), True
        except record.RoutineStateError:
            return state, False

    if not routine_state.update(self, team_id, change):
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found")


def _set_paused_by_person(self, team_id: str, routine_id: object, paused: bool) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    if not isinstance(routine_id, str) or http_routine.ROUTINE_ID_RE.fullmatch(routine_id) is None:
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found")
    set_paused(self, team_id, routine_id, paused)
    operation = "routine-pause" if paused else "routine-resume"
    local_audit.record_request(operation, result="ok", team_id=team_id, detail=routine_id)
    return {"team_id": team_id, "routine_id": routine_id, "paused": paused}


def resume_routine(self, team_id: str, routine_id: object) -> dict[str, object]:
    """A person resumes a paused Routine; an unresolved incident still holds it until its card settles it."""
    return _set_paused_by_person(self, team_id, routine_id, False)


def pause_routine(self, team_id: str, routine_id: object) -> dict[str, object]:
    """A person pauses a whole Routine: no run of it starts until resumed; a run already going finishes."""
    return _set_paused_by_person(self, team_id, routine_id, True)
