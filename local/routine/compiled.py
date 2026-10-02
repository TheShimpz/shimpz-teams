"""A compiled Routine run, executed step by step with no model (ADR-0092 sections 3, 5, and 6).

The run drives the same Team turn loop a chat uses, through the same Action journal, egress, Integrations, Stored
Inputs, and human requests, but its turns come from the compiled plan instead of the Brain: each step is one Action
request whose input is resolved from literals, the run's one start instant, and values earlier steps of the same run
returned. Its recovery snapshot is sealed before the first dispatch; its sealed cursor records each dispatch's logical
operation and exact input before the RPC, and keeps each completed step's selected values before the journal may drop
its receipts, so a completed step never runs again. A healthy run makes no model call and needs no model-provider key.
A suspension freezes the run for a person; a Stop with nothing uncertain ends it stopped; a failure before any dispatch
fails it; every other failure holds the run as an incident for recovery.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from dataclasses import dataclass

from action import execution as action_execution
from action import journal as action_journal
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from inference import client as brain_runtime_client
from local import audit as local_audit
from local import authority as local_authority
from local.chat.segment import RoutineSegment, SegmentRequest
from local.errors import ApiProblemError as ApiProblem
from local.routine import diagnostics as routine_diagnostics
from local.routine import incident as routine_incident
from local.routine import run as routine_run
from local.routine import store as routine_store
from local.routine import turn as routine_turn
from local.validation import validate_team_id
from routine import cursor as routine_cursor
from routine import pin as routine_pin
from routine import plan as routine_plan
from routine import record


class CompiledRunError(RuntimeError):
    """A step could not be resolved or advanced; ``code`` is the stable reason. The run is never retried."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _Seal:
    """Where the run's cursor is sealed, its Team, and where each failed attempt's diagnostic is kept."""

    team_id: str
    store: routine_store.RoutineStore
    diagnostics: routine_diagnostics.DiagnosticStore


class CompiledRuntime:
    """The Brain's turn interface, answered from the plan; it never calls a model.

    ``start`` and ``resume`` return the current step as one Action request, or a completed turn after the last step.
    """

    def __init__(self, seal: _Seal, plan: routine_plan.Plan, cursor: routine_cursor.Cursor, reply: str) -> None:
        self._seal = seal
        self._plan = plan
        self.cursor = cursor
        self._reply = reply

    @staticmethod
    def interrupt(index: int) -> str:
        return f"routine-step-{index}"

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

    def resume(self, _context, results: Mapping[str, object]) -> brain_runtime_client.RuntimeTurn:
        """Seal the completed step's selected values, before the journal may drop its receipts, then go on."""
        try:
            advanced = routine_cursor.complete(self.cursor, self._plan, results.get(self.interrupt(self.cursor.step)))
        except routine_cursor.CursorError as exc:
            raise CompiledRunError(exc.code) from exc
        self.seal(advanced)
        return self._turn()

    def dispatching(self, request: brain_runtime_client.ActionRequest, operation_id: str) -> None:
        """Seal the dispatch's logical operation and exact input before its RPC; a replay keeps both."""
        if request.interrupt_id != self.interrupt(self.cursor.step):
            raise CompiledRunError("routine-step-changed")
        try:
            dispatched = routine_cursor.dispatch(
                self.cursor, self._plan, operation_id, routine_plan.commitment(request.input)
            )
        except routine_cursor.CursorError as exc:
            raise CompiledRunError(exc.code) from exc
        self.seal(dispatched)

    def failed(
        self,
        request: brain_runtime_client.ActionRequest,
        evidence: action_execution.ActionInvocationEvidence,
        exc: BaseException,
    ) -> None:
        """Keep a failed attempt's sanitized failure or safe transport condition before the segment unwinds.

        It is bound to the run's logical operation and attempt and sealed under the Team's incarnation, so the run's
        execution details survive a restart and an archived journal. A diagnostic is never safety evidence: one that
        cannot be kept is audited and the failure goes on unchanged.
        """
        found = routine_diagnostics.evidence(exc)
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
            int(time.time()),
            *found,
        )
        try:
            self._seal.diagnostics.record(
                self._seal.team_id, binding.incarnation, diagnostic, routine_diagnostics.protected(evidence)
            )
        except routine_diagnostics.DiagnosticStoreError:
            local_audit.record_request(
                "routine-diagnostic", result="error", team_id=self._seal.team_id, detail=binding.run_id
            )

    @staticmethod
    def purpose(_context, _request, _assistant_name, _summary) -> None:
        """A compiled run asks no model why it pauses."""
        return


def _plan(self, team_id: str, routine: record.Routine) -> routine_plan.Plan:
    """The Routine's plan, admitted again against the exact current contracts, so drift never runs."""
    _name, _network, active_by_id = self._team_assistants(team_id)
    contracts = routine_turn.contracts(tuple(active_by_id.values()), routine_pin.SCOPE_LOCALE)
    try:
        return routine_plan.admit(routine.plan, contracts)
    except routine_plan.PlanError as exc:
        raise CompiledRunError(exc.code) from exc


def runtime(self, team_id: str, value: record.Run, routine: record.Routine) -> CompiledRuntime:
    """The run's compiled runtime: its recovery snapshot sealed, and its cursor reopened or started."""
    network_id = record.network_of(value.generation, value.run_id)
    binding = routine_cursor.Binding(network_id, routine.routine_id, routine.revision, value.run_id)
    plan = _plan(self, team_id, routine)
    # Write-once and sealed before the run's first dispatch; every later segment reseals the exact same bytes.
    routine_incident.seal_recovery(self, team_id, routine_incident.Recovery(binding, routine.quote, routine.plan))
    try:
        cursor = self.routine_store.cursor(team_id, binding)
    except routine_store.RoutineStoreError as exc:
        raise CompiledRunError("routine-cursor-unavailable") from exc
    if cursor is not None and cursor.plan != plan.digest:
        raise CompiledRunError("cursor-plan-changed")
    started = cursor or routine_cursor.start(plan, binding, int(time.time()))
    compiled = CompiledRuntime(
        _Seal(team_id, self.routine_store, self.routine_diagnostics), plan, started, routine.name
    )
    if cursor is None:
        compiled.seal(started)
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
        routine=RoutineSegment(value.run_id, value.generation, runtime(self, run.team_id, value, run.routine)),
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


def _ended(self, run: routine_run._Run, value: record.Run, batches: list, exc: ApiProblem | CompiledRunError) -> str:
    """How a segment that raised ends: stopped, failed with nothing dispatched, or held for recovery.

    Whether anything was dispatched is read from the run's sealed cursor, snapshot, and journal, never from a runtime
    that may have failed to open them, so a reopened run that already acted is never cleaned up as a failure.
    """
    uncertain = _uncertain(self, value, batches)
    code = exc.code
    if code == "chat-stopped":
        with self._active_chat_guard:
            registration = self._routine_runs.get(run.run_id)
        if registration is not None and registration.overdue:
            code = "active-time-exceeded"
        elif not uncertain:
            return routine_run._end(self, run.team_id, run.run_id, "stopped", {"actions": []})
    if not uncertain and progress(self, run.team_id, value) == "none":
        return routine_run._end(self, run.team_id, run.run_id, "failed", {"code": code, "actions": []})
    routine_incident.hold(self, run.team_id, run.run_id, run.lease)
    return "held"


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
        return _ended(self, run, value, [] if segment is None else segment.batches, exc)
    routine_run._spend(self, run.team_id, run.run_id, run.lease, int(time.monotonic() - started))
    if isinstance(outcome.outcome, chat_orchestrator.ChatOutcome):
        return routine_run.finished(self, run)
    return routine_run.suspended(self, run, outcome)


def run_routine(
    self,
    team_id: str,
    run_id: str,
    evidence: local_authority.RoutineEvidence,
    provider: str,
    _api_key: str,
    progress: chat_progress.Reporter | None = None,
) -> dict[str, object]:
    """Run one segment of a leased compiled run in the Team's execution slot; the model key is never used."""
    team_id = validate_team_id(team_id)
    lease = record.Lease(evidence.lease_sha256, evidence.key_fingerprint)
    value, routine = routine_run._live_run(self, team_id, run_id, lease)
    with (
        self._exclusive_chat_turn(team_id, routine.routine_id) as token,
        routine_run.registered(self, team_id, run_id, token, value.active_seconds_left),
    ):
        # Rechecked in the slot: an Assistant changed since the claim never runs under a contract nobody pinned.
        refused = routine_run._context_refusal(self, team_id, dict(routine.assistants))
        if refused is not None:
            outcome = routine_run._end(self, team_id, run_id, "failed", {"code": refused, "actions": []})
        else:
            generation = routine_run._bind(self, team_id, run_id, lease)
            bound = dataclasses.replace(value, generation=generation)
            run = routine_run._Run(team_id, run_id, lease, token, provider, routine)
            outcome = execute(self, run, bound, progress)
    routine_run._after_run(self, team_id, run_id, routine.routine_id, outcome)
    return {"team_id": team_id, "run_id": run_id, "status": outcome}
