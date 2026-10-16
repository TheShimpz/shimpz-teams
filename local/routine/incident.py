"""Holding a Routine run for a person, and the lifecycle of its incident (ADR-0092 sections 5 and 7).

A run seals its immutable recovery snapshot before its first dispatch: the exact cursor binding (Team incarnation,
Routine and revision, run), the confirmed plan, and the Routine's name (ADR-0101). A hold is four durable steps: the
run's live lease is fenced, its incident's compact evidence is sealed with its own copy of that snapshot, its journal
batch is archived, and the run is indexed as its own incident. Each step is idempotent
and ``reconcile`` resumes from whichever came last, so every crash window recovers without dispatching anything. An
incident is the held run's record with its own status, not an active run or discard work; it never expires, and holds
the Routine until it is resolved. Because it holds its snapshot independently of the Routine record and the archived
journal rows, it can still reopen the run's cursor for verification after Team restarts. A person sets the run aside
through its card's Rodar, or by deleting its Routine; only then, once nothing executes for it any more, are its cursor,
evidence, and archive marker released and its record removed.
"""

import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from action import journal as action_journal
from local import audit as local_audit
from local import errors as local_errors
from local.errors import ApiProblemError as ApiProblem
from local.routine import diagnostics as routine_diagnostics
from local.routine import state as routine_state
from local.routine import store as routine_store
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import strict_json
from routine import claim as routine_claim
from routine import cursor as routine_cursor
from routine import hold as routine_hold
from routine import plan as routine_plan
from routine import record
from routine import runs as routine_runs

VERSION = 1
_OPERATION_FIELDS = ("interrupt_id", "operation_id", "state", "attempts", "origin")


@dataclass(frozen=True, slots=True)
class Recovery:
    """What recovery of one held run is authorized to use: its cursor binding, plan, and its Routine's name."""

    binding: routine_cursor.Binding
    name: str
    plan: dict[str, object]

    @property
    def plan_digest(self) -> str:
        return routine_plan.digest(self.plan)

    def of_run(self, routine_id: str, generation: str) -> bool:
        """Whether it names exactly its run's Routine and the Team incarnation the run's generation runs in."""
        binding = self.binding
        return (
            binding.routine_id == routine_id
            and routine_claim.network_of(generation, binding.run_id) == binding.incarnation
        )

    def runs(self, cursor: routine_cursor.Cursor) -> bool:
        """Whether a cursor runs exactly this snapshot's plan; one of another plan proves nothing about the run."""
        return cursor.plan == self.plan_digest

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
            raise local_errors.routine_state_unavailable() from exc
        if not isinstance(value, dict) or value.pop("version", None) != VERSION:
            raise local_errors.routine_state_unavailable()
    if not isinstance(value, dict) or set(value) != {"binding", "name", "plan"}:
        raise local_errors.routine_state_unavailable()
    binding = value["binding"]
    if not isinstance(binding, list) or len(binding) != 4:
        raise local_errors.routine_state_unavailable()
    snapshot = Recovery(routine_cursor.Binding(*binding), value["name"], value["plan"])
    if (
        not routine_cursor.binding_valid(snapshot.binding)
        or snapshot.binding.run_id != run_id
        or not http_routine.exact_name(snapshot.name)
        or not routine_plan.well_formed(snapshot.plan)
    ):
        raise local_errors.routine_state_unavailable()
    return snapshot


def seal_recovery(self, team_id: str, snapshot: Recovery) -> None:
    """Seal a replayed run's recovery snapshot; the executor does so before the run's first dispatch."""
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
        raise local_errors.routine_state_unavailable() from exc
    fields = {"version", "incident_id", "routine_id", "generation", "fingerprint", "operations", "recovery"}
    if not isinstance(value, dict) or set(value) != fields or value["version"] != VERSION:
        raise local_errors.routine_state_unavailable()
    if value["incident_id"] != incident_id or not isinstance(value["operations"], list):
        raise local_errors.routine_state_unavailable()
    if value["recovery"] is not None:
        snapshot = read_recovery(value["recovery"], incident_id)
        if not snapshot.of_run(value["routine_id"], value["generation"]):
            raise local_errors.routine_state_unavailable()
        value["recovery"] = snapshot
    return value


def run_snapshot(self, team_id: str, value: record.Run) -> Recovery | None:
    """The run's sealed recovery snapshot, which must belong to exactly this run, or None for an unreplayed run."""
    payload = routine_state.call(lambda: self.routine_store.recovery(team_id, value.run_id))
    if payload is None:
        return None
    snapshot = read_recovery(payload, value.run_id)
    if not snapshot.of_run(value.routine_id, value.generation):
        raise local_errors.routine_state_unavailable()
    return snapshot


def hold(self, team_id: str, run_id: str, lease: record.Lease) -> None:
    """Fence a leased run's live lease, then hold it until its incident is durable and indexed."""
    now = int(time.time())

    if not routine_state.applied(self, team_id, lambda state: routine_hold.fence(state, run_id, lease, now)):
        raise local_errors.routine_lease_invalid()
    reconcile(self, team_id, run_id)


def reconcile(self, team_id: str, run_id: str) -> bool:
    """Finish one held run's hold from its last durable step; False when the run is no longer held.

    A run of a Routine being deleted is set aside as it is indexed and no card ever shows it, so evidence, a recovery
    snapshot, or a cursor of it that cannot be read is lost and never waited on; every other run stays held until it is.
    """
    state = routine_state.load(self, team_id)
    value = next((item for item in state.runs if item.run_id == run_id), None)
    if value is None or value.status != "held":
        return False
    deleting = _deleting(state, value.routine_id)
    try:
        held = _held_evidence(self, team_id, value)
    except action_journal.ActionJournalError as exc:
        raise local_errors.action_state_unavailable() from exc
    except ApiProblem:
        if not deleting:
            raise
        held = None
    now = int(time.time())

    found = None if held is None else _held_step(self, team_id, held["recovery"], deleting=deleting)
    # A loss is audited where it is met, before the write that sets the run aside: once that write lands no pass meets
    # it again, so a crash in between must never leave it unaudited. A retried or overlapping pass, or the release that
    # meets the same loss, may audit it again; a missed audit is worse than a repeated one.
    if found is None:
        _lost(team_id, run_id)
    step = routine_hold.UNKNOWN_STEP if found is None else found
    return routine_state.applied(self, team_id, lambda state: routine_hold.settle_hold(state, run_id, now, step))


def _held_evidence(self, team_id: str, value: record.Run) -> dict[str, object]:
    """The held run's sealed evidence, sealed now from its batch and snapshot when there is none; its batch archived.

    Its evidence is lost when it cannot be read, when its batch is archived and no evidence of this hold names it
    (archiving happens only after the evidence is durable, so it is never guessed), or when it names another batch than
    the generation's current one, including one the Action journal no longer holds and so can never archive.
    """
    run_id = value.run_id
    current = self.action_state.current_batch(value.generation)
    payload = routine_state.call(lambda: self.routine_store.incident(team_id, run_id))
    sealed = None if payload is None else read_evidence(payload, run_id)
    if sealed is not None and sealed["generation"] != value.generation:
        # Evidence of an earlier hold this run resumed from; its generation is gone, and this hold seals its own.
        sealed = None
    if sealed is None:
        if current is not None and current[1] == "archived":
            raise local_errors.routine_state_unavailable()
        operations = () if current is None else self.action_state.snapshot(value.generation, current[0])
        snapshot = run_snapshot(self, team_id, value)
        payload = evidence(run_id, value, None if current is None else current[0], operations, snapshot)
        routine_state.call(lambda: self.routine_store.put_incident(team_id, run_id, payload))
        sealed = read_evidence(payload, run_id)
    fingerprint = sealed["fingerprint"]
    if fingerprint != (None if current is None else current[0]):
        raise local_errors.routine_state_unavailable()
    if fingerprint is not None:
        self.action_state.archive(value.generation, fingerprint)
    return sealed


def _deleting(state: record.TeamRoutines, routine_id: str) -> bool:
    return any(item.routine_id == routine_id and item.deleting for item in state.routines)


def _lost(team_id: str, run_id: str) -> None:
    """Audit what a Routine being deleted could not read of one run, which its deletion treats as lost."""
    local_audit.record_team_error("routine-evidence-lost", run_id, team_id)


def _held_step(self, team_id: str, snapshot: Recovery | None, *, deleting: bool) -> routine_hold.HeldStep | None:
    """The Assistant Action the held run's sealed cursor stopped at; none when the run sealed no snapshot or cursor.

    A cursor that cannot be read now fails the hold's step, so the run stays held and the watchdog's next pass
    indexes it with its call; it is never indexed without the call its card must name. A run of a Routine being
    deleted is set aside as it is indexed, so no card ever names its call: its unreadable cursor is lost (None), and
    the run is indexed without a call rather than keep the deletion waiting on a cursor that may never be read.
    """
    if snapshot is None:
        return routine_hold.UNKNOWN_STEP
    try:
        cursor = routine_state.call(lambda: self.routine_store.cursor(team_id, snapshot.binding))
    except ApiProblem:
        if deleting:
            return None
        raise
    if cursor is None:
        return routine_hold.UNKNOWN_STEP
    return held_call(cursor, snapshot.plan["steps"])


def held_call(cursor: routine_cursor.Cursor, steps: list) -> routine_hold.HeldStep:
    """The call a sealed cursor stopped at: its current replay step, the last one once every step completed."""
    index = min(cursor.step, len(steps) - 1)
    return steps[index]["assistant"], steps[index]["action"], index + 1, len(steps)


def reconcile_team(self, team_id: str) -> tuple[str, ...]:
    """Release what every set-aside incident still keeps once nothing executes for it, then finish every held run.

    Each run is its own: one whose release or hold fails stays as it is for the next pass while the others go on, so
    a run whose state cannot be read never holds back the Team's. The ids of the runs that failed are returned.
    """
    state = routine_state.load(self, team_id)
    failed: list[str] = []
    for item in record.incidents(state):
        if item.status == "skipped":
            _isolated(failed, item.run_id, lambda item=item: release(self, team_id, item))
    for value in state.runs:
        if value.status == "held":
            _isolated(failed, value.run_id, lambda value=value: reconcile(self, team_id, value.run_id))
    return tuple(failed)


def _isolated(failed: list[str], run_id: str, step: Callable[[], object]) -> None:
    """Run one run's step, noting its id when its state cannot be read or written now."""
    try:
        step()
    except ApiProblem, record.RoutineStateError:
        failed.append(run_id)


def _transition_problem(code: str) -> ApiProblem:
    """A refused transition, with what refused it: a stale card, a Team admission limit, or an incident gone."""
    if code in {"incident-changed", "routine-revision-changed"}:
        return local_errors.routine_card_stale()
    if code.startswith("routine-") or code == "notices-full":
        return ApiProblem(HTTPStatus.CONFLICT, "the Team cannot hold this Routine change", code=code)
    return local_errors.routine_incident_unavailable()


def _sealed_loss(self, team_id: str, value: record.Run) -> bool:
    """Whether an incident's cursor, under its sealed evidence's binding, records a lost protection.

    The loss is sealed in the cursor before the run's state is marked, so this finds a mark that failed to land.
    Evidence or a cursor that cannot be read, is missing, or names another run, revision, or plan is taken as lost, as a
    deletion takes it: a notice never says a protection held that nothing could read, and the release that follows may
    erase the cursor that recorded it. Every incident's evidence is sealed before it is indexed, so none is missing.
    """
    try:
        sealed = self.routine_store.incident(team_id, value.run_id)
        held = None if sealed is None else read_evidence(sealed, value.run_id)
        snapshot = None if held is None else held["recovery"]
        cursor = None if snapshot is None else self.routine_store.cursor(team_id, snapshot.binding)
    except routine_store.RoutineStoreError, ApiProblem:
        return True
    if held is None or (held["routine_id"], held["generation"]) != (value.routine_id, value.generation):
        return True
    if snapshot is None:
        # Evidence of this run without a recovery snapshot is of a run that was never replayed, so it has no cursor.
        return False
    if cursor is None:
        return True
    # The evidence names exactly this run, so its snapshot does too; it must also bind the run's own revision.
    bound = snapshot.binding.revision == value.revision and snapshot.runs(cursor)
    return not bound or cursor.protection_lost


def marking_losses[T](
    self,
    team_id: str,
    chosen: Callable[[record.Run], bool],
    change: Callable[[record.TeamRoutines], tuple[record.TeamRoutines, T]],
) -> Callable[[record.TeamRoutines], tuple[record.TeamRoutines, T]]:
    """``change`` after every ``chosen`` incident whose unmarked loss is sealed or cannot be ruled out is marked.

    A live run's loss is marked by every write; an incident's matters only to the notices it publishes, so only a write
    that publishes one reads its sealed cursor, inside that write (ADR-0101 §6).
    """

    def marked(state: record.TeamRoutines) -> tuple[record.TeamRoutines, T]:
        for item in record.incidents(state):
            if not item.protection_lost and chosen(item) and _sealed_loss(self, team_id, item):
                state = routine_runs.lose_protection(state, item.run_id)
        return change(state)

    return marked


def set_aside(
    self,
    team_id: str,
    incident_id: str,
    change: Callable[[record.TeamRoutines], record.TeamRoutines],
) -> record.Run:
    """Apply a person's transition that sets the incident aside, then release what it kept.

    The transition checks the card's state in its own write and never replays or fabricates output; any effect the run
    may have had stays unresolved, which the person was told. Once it committed, a failed release only waits for the
    watchdog's next pass: it never reads as nothing having changed.
    """

    def mark(state: record.TeamRoutines) -> tuple[record.TeamRoutines, record.Run | str]:
        try:
            changed = change(state)
        except record.RoutineStateError as exc:
            return state, str(exc)
        return changed, routine_hold.incident(changed, incident_id)

    named = marking_losses(self, team_id, lambda item: item.run_id == incident_id, mark)
    skipped = routine_state.update(self, team_id, named)
    if isinstance(skipped, str):
        raise _transition_problem(skipped)
    settled(self, team_id, skipped)
    return skipped


def executing(self, incident_id: str) -> bool:
    """Whether a verification, recovery episode, or person's answer is still registered for the incident."""
    with self._active_chat_guard:
        return incident_id in self._routine_runs


def release(self, team_id: str, item: record.Run) -> None:
    """Release a set-aside incident unless something still executes for it; the watchdog's next pass retries."""
    if not executing(self, item.run_id):
        _release(self, team_id, item)


def settled(self, team_id: str, item: record.Run) -> None:
    """After a person's committed transition: release now if possible, and otherwise leave it to the watchdog."""
    with contextlib.suppress(ApiProblem):
        release(self, team_id, item)


def _release(self, team_id: str, item: record.Run) -> None:
    """Remove what a skipped incident no longer needs, then its record.

    Each step is idempotent, so a crash leaves the incident skipped and the next pass retries it. Evidence of a run of a
    Routine being deleted that cannot be read is lost and audited, and its generation goes whole, archive marker
    included; any other incident's waits until it can be read.
    """
    deleting = _deleting(routine_state.load(self, team_id), item.routine_id)
    lost = False
    try:
        sealed = routine_state.call(lambda: self.routine_store.incident(team_id, item.run_id))
        held = None if sealed is None else read_evidence(sealed, item.run_id)
    except ApiProblem:
        if not deleting:
            raise
        _lost(team_id, item.run_id)
        held, lost = None, True
    try:
        current = self.action_state.current_batch(item.generation)
        fingerprint = None if held is None else held["fingerprint"]
        if not lost and current is not None and current[1] == "archived" and current[0] != fingerprint:
            # An archived batch no readable evidence names: archiving follows durable evidence, so it was lost.
            if not deleting:
                raise local_errors.routine_state_unavailable()
            _lost(team_id, item.run_id)
            held = None
        if held is not None:
            seal_terminal(self, team_id, item.run_id, held["recovery"])
            if held["fingerprint"] is not None:
                self.action_state.release_archive(item.generation, held["fingerprint"])
        elif deleting:
            # No evidence names an archived batch of the run any more: its deletion releases the whole generation.
            self.action_state.purge(item.generation)
        # Its verification's generation goes too, whatever its own release left.
        self.action_state.purge(routine_claim.verification_of(item.generation, item.run_id))
    except action_journal.ActionJournalError as exc:
        raise local_errors.action_state_unavailable() from exc
    routine_state.call(lambda: self.routine_store.delete_cursor(team_id, item.run_id))
    routine_state.call(lambda: self.routine_store.delete_incident(team_id, item.run_id))
    routine_state.update(self, team_id, lambda state: (routine_hold.release_incident(state, item.run_id), None))
    # Nothing of the released run can show anything any more, so the values it protected leave memory with it.
    self.routine_protections.drop(item.run_id)


def seal_terminal(self, team_id: str, run_id: str, snapshot: Recovery | None) -> None:
    """Seal an ended run's terminal record from its sealed snapshot, admitted for exactly that run, and its cursor.

    It proves which steps never started (ADR-0092 amendment, 2026-10-05, scale). It is a display record: when the
    snapshot or cursor cannot prove anything, none is kept, and a failure to keep one is audited only.
    """
    if snapshot is None:
        return
    try:
        cursor = self.routine_store.cursor(team_id, snapshot.binding)
    except routine_store.RoutineStoreError:
        cursor = None
    if cursor is None or not snapshot.runs(cursor):
        return
    binding = snapshot.binding
    run = routine_diagnostics.RunBinding(
        binding.routine_id, run_id, binding.revision, snapshot.plan_digest, len(snapshot.plan["steps"])
    )
    terminal = routine_diagnostics.RunRecord(run, cursor.step, cursor.operation_id is not None, int(time.time()))
    try:
        self.routine_diagnostics.record_run(team_id, binding.incarnation, terminal)
    except routine_diagnostics.DiagnosticStoreError:
        local_audit.record_team_error("routine-run-record", run_id, team_id)


def unresolved(self, team_id: str, incident_id: str) -> record.Run:
    """The Team's indexed incident, which must still be unresolved."""
    try:
        value = routine_hold.incident(routine_state.load(self, team_id), incident_id)
    except record.RoutineStateError as exc:
        raise local_errors.routine_incident_unavailable() from exc
    if value.status != "unresolved":
        raise local_errors.routine_incident_unavailable()
    return value


def open_recovery(self, team_id: str, incident_id: str) -> OpenedRecovery:
    """Reopen an unresolved incident's cursor under its own sealed binding, for verification or its card.

    It needs neither the Routine record, which deletion removes, nor the archived journal rows; a missing snapshot,
    a binding that disagrees with the index, or a cursor of another plan fails closed.
    """
    indexed = unresolved(self, team_id, incident_id)
    sealed = routine_state.call(lambda: self.routine_store.incident(team_id, incident_id))
    if sealed is None:
        raise local_errors.routine_state_unavailable()
    snapshot = read_evidence(sealed, incident_id)["recovery"]
    if snapshot is None:
        raise ApiProblem(
            HTTPStatus.CONFLICT, "Routine incident cannot be verified", code="routine-incident-unverifiable"
        )
    if (snapshot.binding.routine_id, snapshot.binding.revision) != (indexed.routine_id, indexed.revision):
        raise local_errors.routine_state_unavailable()
    try:
        cursor = self.routine_store.cursor(team_id, snapshot.binding)
    except routine_store.RoutineStoreError as exc:
        raise local_errors.routine_state_unavailable() from exc
    if cursor is None or not snapshot.runs(cursor):
        raise local_errors.routine_state_unavailable()
    return OpenedRecovery(snapshot, cursor)


def pause(self, team_id: str, incident_id: str, reason: str) -> None:
    """Recovery pauses the Routine an unresolved incident holds, and says why on the held run's notice."""

    def change(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str | None]:
        try:
            return routine_hold.pause_incident(state, incident_id, int(time.time()), reason), None
        except record.RoutineStateError as exc:
            return state, str(exc)

    named = marking_losses(self, team_id, lambda item: item.run_id == incident_id, change)
    refused = routine_state.update(self, team_id, named)
    if refused is not None:
        raise _transition_problem(refused)


def set_paused(self, team_id: str, routine_id: str, paused: bool) -> None:
    """Pausing disables dispatch while every incident stays; resuming never bypasses an unresolved one."""
    if not routine_state.applied(self, team_id, lambda state: record.set_paused(state, routine_id, paused)):
        raise local_errors.routine_not_found()


def _set_paused_by_person(self, team_id: str, routine_id: object, paused: bool) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    if not http_routine.matches(routine_id, http_routine.ROUTINE_ID_RE):
        raise local_errors.routine_not_found()
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
