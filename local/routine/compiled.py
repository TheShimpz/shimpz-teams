"""A recorded Routine run, executed step by step with no model (ADR-0092 sections 3, 5, and 6; ADR-0101).

The run drives the same Team turn loop a chat uses, through the same Action journal, egress, Integrations, Stored
Inputs, and human requests, but its turns come from the compiled plan instead of the Brain: each step is one Action
request whose input is resolved from literals, the run's one start instant, and values earlier steps of the same run
returned. Its recovery snapshot is sealed before the first dispatch; its sealed cursor records each dispatch's logical
operation and exact input before the RPC, and keeps each completed step's selected values before the journal may drop
its receipts, so a completed step never runs again. A healthy run makes no model call and needs no model-provider key.
A suspension freezes the run for a person; a Stop with nothing uncertain ends it stopped; a failure before any dispatch
fails it; every other failure holds the run as an incident for recovery.

The run's protection (``local/routine/protection.py``) is bound with its first cursor and grows before every RPC and
with every result's secret strings before anything outward sees it: a shown result, a step record's previews, and a
failure's diagnostic are redacted against all of it. A run that loses it shows nothing it produced afterwards.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from dataclasses import dataclass

from action import execution as action_execution
from action import failure as action_failure
from action import journal as action_journal
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from inference import client as brain_runtime_client
from local import audit as local_audit
from local import authority as local_authority
from local.chat.segment import RoutineSegment, SegmentRequest
from local.errors import ApiProblemError as ApiProblem
from local.routine import contracts as routine_contracts
from local.routine import diagnostics as routine_diagnostics
from local.routine import incident as routine_incident
from local.routine import protection as routine_protection
from local.routine import run as routine_run
from local.routine import state as routine_state
from local.routine import store as routine_store
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from routine import cursor as routine_cursor
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record, trace

# What a step record's duration is measured with: its own clock, apart from the active-time accounting's.
_STEP_CLOCK = time.perf_counter
# Team-detected faults that hold a run for policy, never admitting absence or a retry (ADR-0092 section 6): a secret
# echo, an invalid result or frame, or an undeclared human request, which Team refuses as an invalid result.
_POLICY_CODES = frozenset({"assistant-secret-exposure", "invalid-action-output"})
_POLICY_CONDITIONS = frozenset({"frame-invalid"})


def fault_of(exc: BaseException) -> str:
    """Team's classification of one failed attempt, from the problem it raised; never from a model or a message."""
    if getattr(exc, "code", None) == "assistant-action-blocked":
        # Team could not prove the workload stopped after an ambiguous outcome.
        return "unquiesced"
    if getattr(exc, "code", None) in _POLICY_CODES:
        return "policy"
    if action_failure.failure_of(exc) is not None:
        return "handled"
    cause = exc.__cause__
    for _depth in range(8):
        if isinstance(cause, action_execution.RpcExchangeError):
            policy = cause.kind == "invalid-result" or cause.condition in _POLICY_CONDITIONS
            # Every other transport fault fail-stopped its workload first; a stop that failed is unquiesced above.
            return "policy" if policy else "transport"
        if cause is None:
            return "other"
        cause = cause.__cause__
    return "other"


class CompiledRunError(RuntimeError):
    """A step could not be resolved or advanced; ``code`` is the stable reason. The run is never retried."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _Attempt:
    """The dispatched step's attempt, in memory only: its position, start, exact input, and the values it protects."""

    position: int
    started: float
    input: Mapping[str, object]
    protected: tuple[str, ...]
    # When the Action returned or raised, before Team sealed or projected anything of it.
    ended: float | None = None


@dataclass(frozen=True, slots=True)
class _Seal:
    """Where the run's cursor is sealed, its Team, and where its diagnostics and protection are kept.

    ``lost`` records the run's loss of its protection in the Team's state.
    """

    team_id: str
    store: routine_store.RoutineStore
    diagnostics: routine_diagnostics.DiagnosticStore
    protections: routine_protection.RunProtections
    lost: object = None


class CompiledRuntime:
    """The Brain's turn interface, answered from the plan; it never calls a model.

    ``start`` and ``resume`` return the current step as one Action request, or a completed turn after the last step.
    """

    def __init__(
        self,
        seal: _Seal,
        plan: routine_plan.Plan,
        cursor: routine_cursor.Cursor,
        reply: str,
        protected: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._seal = seal
        self._plan = plan
        self.cursor = cursor
        self._reply = reply
        # Where each failed attempt's injected private values are kept in memory for this execution's recovery.
        self._protected = {} if protected is None else protected
        # The dispatched step's attempt, which its step record describes once it ends (ADR-0092, 2026-10-05, scale).
        self._attempt: _Attempt | None = None

    @property
    def plan(self) -> routine_plan.Plan:
        return self._plan

    def protection(self) -> trace.Protection:
        """The run's protection now; one that is not held in this process is lost."""
        return self.protect(())

    def protect(self, values) -> trace.Protection:
        """Protect more of the run's values; a protection that saturates or is missing is lost, and the run says so."""
        found = self._seal.protections.grow(self.cursor.binding.run_id, values)
        if found.lost and not self.cursor.protection_lost:
            self.seal(routine_cursor.lose_protection(self.cursor))
            self._seal.lost()
        return found

    @staticmethod
    def interrupt(index: int) -> str:
        return f"routine-step-{index}"

    def strategy(self) -> dict[str, object]:
        """How the turn loop drives this run: one Action round per step, and no invoked Actions kept for learning."""
        return {"max_rounds": len(self._plan.steps), "record_invoked": False}

    def _turn(self) -> brain_runtime_client.RuntimeTurn:
        if self.cursor.done(self._plan):
            return brain_runtime_client.RuntimeTurn("completed", self._reply, ())
        step = self._plan.steps[self.cursor.step]
        try:
            # The orchestrator then validates this exact input against the Action's reviewed schema before dispatch.
            resolved = routine_plan.resolve(
                self._plan, step, self.cursor.selections(), self.cursor.started_at, lambda value: value
            )
        except routine_plan.PlanError as exc:
            raise CompiledRunError(exc.code) from exc
        request = brain_runtime_client.ActionRequest(
            self.interrupt(self.cursor.step), step.assistant_id, step.action, resolved
        )
        return brain_runtime_client.RuntimeTurn("action-required", "", (request,))

    def seal(self, cursor: routine_cursor.Cursor) -> None:
        try:
            self._seal.store.put_cursor(self._seal.team_id, cursor)
        except routine_store.RoutineStoreError as exc:
            raise CompiledRunError("routine-cursor-unavailable") from exc
        self.cursor = cursor

    def start(self, _context, _message, *, conversation=()) -> brain_runtime_client.RuntimeTurn:
        """The segment's first turn; a carried operation is retried only after proven absence, once per run."""
        if self.cursor.carried:
            try:
                retried = routine_cursor.retry(self.cursor)
            except routine_cursor.CursorError as exc:
                raise CompiledRunError(exc.code) from exc
            self.seal(retried)
        return self._turn()

    def logical_operation(self, request: brain_runtime_client.ActionRequest) -> str | None:
        """The carried operation a permitted retry repeats; every other request lets the journal mint its own."""
        if self.cursor.carried and request.interrupt_id == self.interrupt(self.cursor.step):
            return self.cursor.operation_id
        return None

    def returned(self) -> None:
        """The dispatched Action returned or raised: its duration ends here, before any Team work on its result."""
        if self._attempt is not None and self._attempt.ended is None:
            self._attempt = dataclasses.replace(self._attempt, ended=_STEP_CLOCK())

    def resume(self, _context, results: Mapping[str, object]) -> brain_runtime_client.RuntimeTurn:
        """Seal the completed step's selected values, before the journal may drop its receipts, then go on."""
        result = results.get(self.interrupt(self.cursor.step))
        dispatched = self.cursor
        step = self._plan.steps[self.cursor.step]
        # Every secret string the result holds is protected before anything of it is kept or shown.
        protection = self.protect(trace.secret_values(result, step.output_schema))
        try:
            advanced = advance(self._seal.store, self._seal.team_id, self.cursor, self._plan, (result, protection))
        except routine_cursor.CursorError as exc:
            raise CompiledRunError(exc.code) from exc
        except routine_store.RoutineStoreError as exc:
            raise CompiledRunError("routine-cursor-unavailable") from exc
        self.seal(advanced)
        self.record_step("done", dispatched)
        return self._turn()

    def record_step(self, status: str, cursor: routine_cursor.Cursor | None = None) -> None:
        """Seal what the dispatched attempt did, once, after it ended; a record that cannot be kept is audited only.

        A step record is a display record, never safety evidence: a failure to keep one never changes the run. A
        ``stopped`` attempt was cut by Stop or the run's deadline, and says nothing about whether it acted.
        """
        attempt, cursor = self._attempt, cursor or self.cursor
        if attempt is None:
            return
        step = self._plan.steps[attempt.position - 1]
        protection = self.protection()
        protected = (*attempt.protected, *protection.values)
        inputs = (
            None
            if protection.lost
            else routine_plan.input_preview(self._plan, step, attempt.input, cursor.selections(), protected)
        )
        binding = cursor.binding
        run = routine_diagnostics.RunBinding(
            binding.routine_id, binding.run_id, binding.revision, self._plan.digest, len(self._plan.steps)
        )
        # A wait has no end yet, and a verified occurrence's time is verification's, never the Action's own.
        ended = _STEP_CLOCK() if attempt.ended is None else attempt.ended
        elapsed = None if status in ("waiting", "recovered") else int((ended - attempt.started) * 1000)
        self._attempt = None
        position = {"phase": "replay", "step": attempt.position}
        made = (position, step.assistant_id, step.action, status, max(cursor.attempts, 1), elapsed)
        record = routine_diagnostics.StepRecord(run, *made, int(time.time()), inputs)
        if http_routine.encoded_bytes(record.view()) > http_routine.MAX_STEP_VIEW_BYTES:
            record = dataclasses.replace(record, inputs=None)
        try:
            self._seal.diagnostics.record_step(self._seal.team_id, binding.incarnation, record, protected)
        except routine_diagnostics.DiagnosticStoreError:
            local_audit.record_request(
                "routine-step-record", result="error", team_id=self._seal.team_id, detail=binding.run_id
            )

    def dispatching(
        self, request: brain_runtime_client.ActionRequest, operation_id: str, workload: str = "", evidence=None
    ) -> None:
        """Seal the dispatch's logical operation, exact input, and workload before its RPC; a replay keeps both."""
        if request.interrupt_id != self.interrupt(self.cursor.step):
            raise CompiledRunError("routine-step-changed")
        try:
            dispatched = routine_cursor.dispatch(
                self.cursor,
                self._plan,
                operation_id,
                routine_plan.commitment(request.input),
                workload=workload,
                dispatched_at=int(time.time()),
            )
        except routine_cursor.CursorError as exc:
            raise CompiledRunError(exc.code) from exc
        self.seal(dispatched)
        if evidence is not None:
            self.protect(routine_diagnostics.protected(evidence))
        self._attempt = _Attempt(self.cursor.step + 1, _STEP_CLOCK(), dict(request.input), ())

    def failed(
        self,
        request: brain_runtime_client.ActionRequest,
        evidence: action_execution.ActionInvocationEvidence,
        exc: BaseException,
    ) -> None:
        """Keep a failed attempt's sanitized failure or safe transport condition before the segment unwinds.

        It is bound to the run's logical operation and attempt and sealed under the Team's incarnation, so the run's
        execution details survive a restart and an archived journal. A diagnostic is never safety evidence: one that
        cannot be kept is audited and the failure goes on unchanged. Team's classification of the failure is safety
        evidence and is sealed in the cursor first: a cursor that cannot keep it fails the run closed.
        """
        self.returned()
        try:
            classified = routine_cursor.failed(self.cursor, fault_of(exc))
        except routine_cursor.CursorError as error:
            raise CompiledRunError(error.code) from error
        self.seal(classified)
        self.record_step("stopped" if getattr(exc, "code", "") == "chat-stopped" else "failed")
        # Only in memory, and only for this execution's automatic recovery: a recovered result is checked against the
        # exact private values this attempt was given, never against values a later call may have instead.
        self._protected[evidence.operation_id] = routine_diagnostics.protected(evidence)
        protection = self.protect(routine_diagnostics.protected(evidence))
        found = routine_diagnostics.evidence(exc, protection)
        if found is None:
            return
        binding = self.cursor.binding
        diagnostic = routine_diagnostics.Diagnostic(
            binding.routine_id,
            binding.run_id,
            evidence.operation_id,
            self.cursor.attempts,
            request.assistant_id,
            request.action,
            {"phase": "replay", "step": self.cursor.step + 1},
            int(time.time()),
            *found,
        )
        try:
            self._seal.diagnostics.record(self._seal.team_id, binding.incarnation, diagnostic, protection.values)
        except routine_diagnostics.DiagnosticStoreError:
            local_audit.record_request(
                "routine-diagnostic", result="error", team_id=self._seal.team_id, detail=binding.run_id
            )

    @staticmethod
    def purpose(_context, _request, _assistant_name, _summary) -> None:
        """A compiled run asks no model why it pauses."""
        return


def _sealed_by(self, team_id: str, run_id: str) -> _Seal:
    """Where one run's cursor, records, and protection live, and how its loss of protection reaches its state."""

    def lost() -> None:
        routine_state.update(self, team_id, lambda state: (record.lose_protection(state, run_id), None))

    return _Seal(team_id, self.routine_store, self.routine_diagnostics, self.routine_protections, lost)


def record_recovered(self, team_id: str, cursor: routine_cursor.Cursor, plan: routine_plan.Plan, protected) -> None:
    """A verified occurrence completed the held step: its record says recovered, with the inputs it was given."""
    runtime = CompiledRuntime(_sealed_by(self, team_id, cursor.binding.run_id), plan, cursor, "")
    try:
        step = plan.steps[cursor.step]
        resolved = routine_plan.resolve(plan, step, cursor.selections(), cursor.started_at, lambda value: value)
    except routine_plan.PlanError:
        resolved = {}
    runtime._attempt = _Attempt(cursor.step + 1, _STEP_CLOCK(), resolved, tuple(protected))
    runtime.record_step("recovered", cursor)


def advance(
    store: routine_store.RoutineStore,
    team_id: str,
    cursor: routine_cursor.Cursor,
    plan: routine_plan.Plan,
    completed: tuple[object, trace.Protection],
) -> routine_cursor.Cursor:
    """The cursor past its completed step; the plan's shown step also keeps its bounded result and keyed digest."""
    result, protection = completed
    shown_step = plan.shown()
    slot = None
    if not cursor.replayed(plan) and shown_step is not None and plan.steps[cursor.step].step_id == shown_step.step_id:
        slot = _slot(store, team_id, cursor.binding, (shown_step, cursor.step + 1), (result, protection))
    return routine_cursor.complete(cursor, plan, result, slot)


def _slot(
    store: routine_store.RoutineStore,
    team_id: str,
    binding: routine_cursor.Binding,
    shown: tuple[routine_plan.Step, int],
    completed: tuple[object, trace.Protection],
) -> dict[str, object]:
    """One shown step's result as its notice will show it, with the keyed digest a change is compared on.

    The output names the step by its position, as the wire does. A result that cannot be projected is kept as
    unavailable, so the run still completes and says so; so is every result after the run lost its protection, whose
    digest is never kept, so the ``changes`` baseline never moves.
    """
    step, position = shown
    result, protection = completed
    try:
        if protection.lost:
            raise routine_plan.OutputError("routine-protection-lost")
        node = routine_plan.output_safe(result, step.output_schema, protection.values)
    except routine_plan.OutputError:
        return {"step": step.step_id, "output": routine_plan.output_state(position, "unavailable"), "digest": None}
    material = routine_plan.output_compared(node)
    digest = None if material is None else store.output_digest(team_id, binding, step.step_id, material)
    return {"step": step.step_id, "output": routine_plan.output_shown(position, node), "digest": digest}


def sealed_shown(self, team_id: str, value: record.Run) -> dict[str, object] | None:
    """The shown result a run's sealed cursor kept, or None when it kept none or cannot be read."""
    try:
        _batch, _snapshot, cursor = _sealed(self, team_id, value)
    except action_journal.ActionJournalError, routine_store.RoutineStoreError, ApiProblem:
        return None
    return None if cursor is None else cursor.shown


def protection_lost(self, team_id: str, value: record.Run) -> bool:
    """Whether a run has lost its protection, sealing the loss in its cursor first.

    Protection is bound with a run's first cursor and lives only in the boot that bound it: a cursor sealed in another
    boot, one this process no longer holds protection for, or one that cannot be read has lost it, and the loss is
    never undone. A run with no cursor has dispatched nothing, since every dispatch is sealed before its values are
    injected, so it has nothing to protect.
    """
    if not value.generation:
        return False
    try:
        _batch, _snapshot, cursor = _sealed(self, team_id, value)
    except action_journal.ActionJournalError, routine_store.RoutineStoreError, ApiProblem:
        return True
    if cursor is None:
        return False
    if cursor.protection_lost:
        return True
    if not self.routine_protections.current(value.run_id, cursor.boot).lost:
        return False
    self.routine_store.lose_cursor(team_id, cursor.binding)
    return True


def lost_runs(self, team_id: str) -> frozenset[str]:
    """The Team's runs that lost their protection, each loss sealed in its run's cursor first (ADR-0101 section 6)."""
    return frozenset(
        item.run_id for item in self.routine_store.load(team_id).runs if protection_lost(self, team_id, item)
    )


def _plan(self, team_id: str, routine: record.Routine) -> routine_plan.Plan:
    """The Routine's plan, admitted again against the exact current contracts, so drift never runs."""
    _name, _network, active_by_id = self._team_assistants(team_id)
    contracts = routine_contracts.contracts(tuple(active_by_id.values()))
    try:
        return routine_plan.admit(routine.plan, contracts)
    except routine_plan.PlanError as exc:
        raise CompiledRunError(exc.code) from exc


def runtime(
    self,
    team_id: str,
    value: record.Run,
    routine: record.Routine,
    protected: dict[str, tuple[str, ...]] | None = None,
) -> CompiledRuntime:
    """The run's runtime: its recovery snapshot sealed, and its cursor reopened or started with its protection.

    A fresh run binds its protection in this Team boot with its first cursor, before anything can dispatch, freeze,
    or decide; a reopened one keeps it only when it was bound in this boot and is still held here.
    """
    network_id = record.network_of(value.generation, value.run_id)
    binding = routine_cursor.Binding(network_id, routine.routine_id, routine.revision, value.run_id)
    plan = _plan(self, team_id, routine)
    # Write-once and sealed before the run's first dispatch; every later segment reseals the exact same bytes.
    snapshot = routine_incident.Recovery(binding, routine.name, routine.plan)
    routine_incident.seal_recovery(self, team_id, snapshot)
    try:
        cursor = self.routine_store.cursor(team_id, binding)
    except routine_store.RoutineStoreError as exc:
        raise CompiledRunError("routine-cursor-unavailable") from exc
    if cursor is not None and cursor.plan != plan.digest:
        raise CompiledRunError("cursor-plan-changed")
    protections = self.routine_protections
    if cursor is None:
        protections.bind(value.run_id)
        started = routine_cursor.start(plan, binding, int(time.time()), protections.boot)
    else:
        protections.current(value.run_id, cursor.boot)
        started = cursor
    compiled = CompiledRuntime(_sealed_by(self, team_id, value.run_id), plan, started, routine.name, protected)
    if cursor is None:
        compiled.seal(started)
    else:
        compiled.protection()
    return compiled


def request(
    self,
    run: routine_run._Run,
    value: record.Run,
    progress: chat_progress.Reporter | None,
    pending: object | None = None,
) -> SegmentRequest:
    """A compiled segment's request, fresh or resumed from a frozen continuation: it carries no model key."""
    resumed = (
        {"message": ""}
        if pending is None
        else {
            "continuation": pending.continuation,
            "expected_identity": pending.identity,
            "transcripts": run.transcripts,
            "requests_used": run.requests_used,
            "locale": pending.locale,
        }
    )
    return SegmentRequest(
        team_id=run.team_id,
        file_ids=[],
        assistant_ids=tuple(assistant for assistant, _pin in run.routine.assistants),
        provider=run.provider,
        api_key="",
        token=run.token,
        routine=RoutineSegment(
            value.run_id, value.generation, runtime(self, run.team_id, value, run.routine, run.protected)
        ),
        progress=progress or chat_progress.Reporter(),
        **resumed,
    )


def _sealed(self, team_id: str, value: record.Run):
    """The run's journal batch, sealed recovery snapshot, and cursor; raises when any is unreadable or not the run's.

    The cursor must name exactly the snapshot's plan before its step can say anything.
    """
    batch = self.action_state.current_batch(value.generation)
    payload = self.routine_store.recovery(team_id, value.run_id)
    if payload is None:
        return batch, None, None
    snapshot = routine_incident.read_recovery(payload, value.run_id)
    if (
        snapshot.binding.routine_id != value.routine_id
        or record.network_of(value.generation, value.run_id) != snapshot.binding.incarnation
    ):
        raise routine_store.RoutineStoreError("Routine recovery snapshot names another run")
    cursor = self.routine_store.cursor(team_id, snapshot.binding)
    if cursor is not None and cursor.plan != snapshot.plan_digest:
        # A cursor of another plan proves nothing about this run's steps, whether clean or complete.
        raise routine_store.RoutineStoreError("Routine cursor names another plan")
    return batch, snapshot, cursor


def progress(self, team_id: str, value: record.Run) -> str:
    """What durable state proves about one compiled run, never what a segment remembers in memory.

    ``none``: nothing of it can have been dispatched, because every dispatch is sealed in its cursor before the RPC, and
    its cursor names none. ``done``: its sealed cursor completed every step of its sealed plan. ``partial``:
    anything else, including state that cannot be read, so evidence of an effect is never cleaned up as a failure.
    """
    if not value.generation:
        # A run binds its journal generation before anything of it can run.
        return "none"
    try:
        batch, snapshot, cursor = _sealed(self, team_id, value)
    except action_journal.ActionJournalError, routine_store.RoutineStoreError, ApiProblem:
        return "partial"
    if cursor is None:
        return "none" if batch is None else "partial"
    if cursor.step == 0 and cursor.operation_id is None:
        # Every dispatch is sealed in the cursor before its RPC: whatever the journal began never reached an Assistant.
        return "none"
    finished = cursor.operation_id is None and cursor.step == len(snapshot.plan["steps"])
    return "done" if finished else "partial"


def _sealed_done(self, team_id: str, value: record.Run) -> bool:
    """Whether the run's sealed cursor proves every step of its sealed plan complete."""
    return progress(self, team_id, value) == "done"


def _uncertain(self, value: record.Run, batches: list) -> bool:
    """Whether a dispatch of the run may have acted without its outcome being known; unreadable counts as yes."""
    if batches and batches[-1].held:
        return True
    if not value.generation:
        return False
    try:
        return self.action_state.uncertain_fingerprint(value.generation) is not None
    except action_journal.ActionJournalError:
        return True


def _ended(
    self, run: routine_run._Run, value: record.Run, segment: RoutineSegment | None, exc: ApiProblem | CompiledRunError
) -> str:
    """How a segment that raised ends: stopped, failed with nothing dispatched, or held for recovery.

    Whether anything was dispatched is read from the run's sealed cursor, snapshot, and journal, never from a runtime
    that may have failed to open them, so a reopened run that already acted is never cleaned up as a failure.
    """
    uncertain = _uncertain(self, value, [] if segment is None else segment.batches)
    code = exc.code
    if code == "chat-stopped" and segment is not None:
        # An attempt still in flight when Stop or the deadline cut the segment is recorded as stopped.
        segment.runtime.record_step("stopped")
    if code == "chat-stopped":
        with self._active_chat_guard:
            registration = self._routine_runs.get(run.run_id)
        if registration is not None and registration.overdue:
            code = "active-time-exceeded"
            if progress(self, run.team_id, value) == "done":
                # Out of time, not stopped by a person, after its sealed cursor completed every step: it is complete.
                return routine_run.complete_sealed(self, run, sealed_shown(self, run.team_id, value))
        elif not uncertain:
            return routine_run._end(self, run.team_id, run.run_id, "stopped", {"actions": []})
    if not uncertain and (
        progress(self, run.team_id, value) == "none"
        or (code in UNRESOLVED_INPUTS and _read_only_prefix(self, run.team_id, value))
    ):
        return routine_run._end(self, run.team_id, run.run_id, "failed", {"code": code, "actions": [], **_at(segment)})
    routine_incident.hold(self, run.team_id, run.run_id, run.lease)
    return "held"


# Team's own resolution of a step's input from earlier results, refused before that step can dispatch (ADR-0101).
UNRESOLVED_INPUTS = frozenset({"plan-reference-missing", "plan-reference-ambiguous", "plan-reference-invalid"})


def _read_only_prefix(self, team_id: str, value: record.Run) -> bool:
    """Whether every step the run's sealed cursor dispatched is one the Routine's revision permits as read-only.

    The caller already knows no dispatch's outcome is uncertain, so such a run has nothing for a person to settle and
    an input it cannot resolve fails it; anything unreadable or a Routine since changed proves nothing.
    """
    try:
        _batch, snapshot, cursor = _sealed(self, team_id, value)
        current = record.routine(routine_state.load(self, team_id), value.routine_id)
    except action_journal.ActionJournalError, routine_store.RoutineStoreError, record.RoutineStateError, ApiProblem:
        return False
    if cursor is None or current.revision != snapshot.binding.revision:
        return False
    dispatched = snapshot.plan["steps"][: cursor.step + (cursor.operation_id is not None)]
    read_only = {(item["assistant"], item["action"]) for item in current.permitted if item["read_only"]}
    return all((step["assistant"], step["action"]) in read_only for step in dispatched)


def _at(segment: RoutineSegment | None) -> dict[str, object]:
    """The step a failed run stopped at, by position among its plan's steps, only once that step was dispatched."""
    if segment is None or segment.runtime.cursor.operation_id is None:
        return {"position": None, "steps": None}
    runtime = segment.runtime
    return {"position": {"phase": "replay", "step": runtime.cursor.step + 1}, "steps": len(runtime.plan.steps)}


def execute(
    self, run: routine_run._Run, value: record.Run, progress: chat_progress.Reporter | None, pending: object = None
) -> str:
    """Run one registered segment of a compiled run in the held execution slot and record exactly how it ended."""
    started = time.monotonic()
    segment: RoutineSegment | None = None
    try:
        segment_request = request(self, run, value, progress, pending)
        segment = segment_request.routine
        outcome = self._run_chat_segment(segment_request)
    except (ApiProblem, CompiledRunError) as exc:
        # A failed segment's active time is charged under its live lease before it is fenced, so a hold carries the
        # balance the run really has left.
        routine_run._spend(self, run.team_id, run.run_id, run.lease, time.monotonic() - started)
        return _ended(self, run, value, segment, exc)
    routine_run._spend(self, run.team_id, run.run_id, run.lease, time.monotonic() - started)
    if isinstance(outcome.outcome, chat_orchestrator.ChatOutcome):
        shown = segment.runtime.cursor.shown
        return routine_run.finished(self, run, value, lambda: _sealed_done(self, run.team_id, value), shown)
    # The run waits at its current step, which its notice and its step record name by position.
    segment.runtime.record_step("waiting")
    return routine_run.suspended(self, run, outcome, {"phase": "replay", "step": segment.runtime.cursor.step + 1})


def run_routine(
    self,
    team_id: str,
    run_id: str,
    evidence: local_authority.RoutineEvidence,
    claimed: tuple[int, str, str],
    credentials: tuple[str, str],
    progress: chat_progress.Reporter | None = None,
) -> dict[str, object]:
    """Run one segment of a leased compiled run in the Team's execution slot.

    The signed request names the Routine revision and plan digest the run was claimed at; any other is refused before
    anything runs. A healthy run never uses the model key of ``credentials``; only a held run's one automatic recovery
    may ask the Brain with it.
    """
    team_id = validate_team_id(team_id)
    provider, api_key = credentials
    # Without a key Admin holds, the run uses the Team's configured provider; it needs no key unless it is held.
    provider = provider or routine_run.team_provider(self, team_id) or ""
    lease = record.Lease(evidence.lease_sha256, evidence.key_fingerprint)
    value, routine = routine_run._live_run(self, team_id, run_id, lease)
    current = (routine.revision, routine_definition.plan_digest(routine.plan), http_routine.run_mode(routine.schedule))
    if claimed != current:
        raise ApiProblem(409, "Routine revision changed since the claim", code="routine-revision-stale")
    with (
        self._exclusive_chat_turn(team_id, routine.routine_id) as token,
        routine_run.registered(self, team_id, run_id, token, value.active_seconds_left),
    ):
        # Rechecked in the slot: an Assistant changed since the claim never runs under a contract nobody pinned.
        refused = routine_run._context_refusal(self, team_id, dict(routine.assistants))
        if refused is not None:
            outcome = routine_run._end(
                self, team_id, run_id, "failed", {"code": refused, "actions": [], "position": None, "steps": None}
            )
        else:
            generation = routine_run._bind(self, team_id, run_id, lease)
            bound = dataclasses.replace(value, generation=generation)
            run = routine_run._Run(team_id, run_id, lease, token, provider, routine)
            outcome = execute(self, run, bound, progress)
            if outcome == "held":
                outcome = self._recover_routine_run(run, api_key, progress)
    routine_run._after_run(self, team_id, run_id, routine.routine_id, outcome)
    return {"team_id": team_id, "run_id": run_id, "status": outcome}
