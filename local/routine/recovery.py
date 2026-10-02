"""Verifying and continuing a held Routine run, with no model (ADR-0092 sections 4, 6, and 7).

A held run's failed step is assessed from its sealed cursor, recovery snapshot, and incident evidence. Its operation
is proven absent when Team-admitted evidence says so: a reviewed read-only Action, a journal that settled it without
effect, or a suspension before any effect. Anything else stays uncertain until the Action's fixed read-only verifier
is run under its exact descriptor: the operation's logical id or every required original input whole, and nothing a
model chose. ``occurred`` with a recovered result valid under the original output schema completes the step;
``not_occurred`` proves absence, which permits the one retry of the same operation in the whole run; anything else,
including a failure of the verifier, is inconclusive and leaves the run held for a person. Exceptions, validation
messages, HTTP status codes, expired credentials, and not-found responses are never proof by themselves.

A verifier call runs in its own journal generation, released when it ends. A continuation resumes the held run in a
fresh generation under a fresh internal lease, in the Team's execution slot, and an uncertain operation is never
passed: it is retried only after proven absence, or the run is held again.
"""

from __future__ import annotations

import contextlib
import math
import time
from dataclasses import dataclass

from action import journal as action_journal
from assistant import spec as assistant_spec
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from inference import client as brain_runtime_client
from inference import config as inference_config
from inference import recovery as inference_recovery
from local.chat.segment import RoutineSegment, SegmentRequest
from local.errors import ApiProblemError as ApiProblem
from local.routine import compiled as routine_compiled
from local.routine import diagnostics as routine_diagnostics
from local.routine import incident as routine_incident
from local.routine import run as routine_run
from local.routine import state as routine_state
from local.routine import store as routine_store
from local.routine import turn as routine_turn
from routine import cursor as routine_cursor
from routine import pin as routine_pin
from routine import plan as routine_plan
from routine import record

VERIFY_SUFFIX = "v1"
# Each recovery call's output cap, charged in full before the call (ADR-0092 section 6).
MAX_OUTPUT_TOKENS = 1024
# The episode's active-time clock.
_clock = time.monotonic
VERIFY_INTERRUPT = "routine-verify"
# What proves an operation of a mutating Action absent without a verifier: it settled without effect, or it paused
# for a person before acting.
_ABSENT_STATES = frozenset({"no_effect", "prepared"})


@dataclass(frozen=True, slots=True)
class Assessment:
    """A held run's failed step, its Action's reviewed contract, and what the incident evidence says of it."""

    opened: routine_incident.OpenedRecovery
    plan: routine_plan.Plan
    action: object
    network_id: str
    state: str | None

    @property
    def cursor(self) -> routine_cursor.Cursor:
        return self.opened.cursor

    @property
    def step(self) -> routine_plan.Step:
        return self.plan.steps[self.cursor.step]


class VerifierRuntime:
    """One fixed read-only verifier call in place of a Brain turn; it never calls a model."""

    def __init__(self, request: brain_runtime_client.ActionRequest) -> None:
        self._request = request
        self.result: object = None

    def start(self, _context, _message, *, conversation=()) -> brain_runtime_client.RuntimeTurn:
        return brain_runtime_client.RuntimeTurn("action-required", "", (self._request,))

    def resume(self, _context, results) -> brain_runtime_client.RuntimeTurn:
        self.result = results.get(self._request.interrupt_id)
        return brain_runtime_client.RuntimeTurn("completed", "", ())

    def dispatching(self, _request, _operation_id) -> None:
        return

    def failed(self, _request, _evidence, _exc) -> None:
        return

    @staticmethod
    def logical_operation(_request) -> None:
        return None

    @staticmethod
    def purpose(_context, _request, _assistant_name, _summary) -> None:
        return


def _drift() -> ApiProblem:
    return ApiProblem(409, "Routine contracts changed since the run", code="routine-drift")


def _seal(self, team_id: str, cursor: routine_cursor.Cursor) -> routine_cursor.Cursor:
    try:
        self.routine_store.put_cursor(team_id, cursor)
    except routine_store.RoutineStoreError as exc:
        raise routine_state.unavailable() from exc
    return cursor


def _operation_state(self, team_id: str, incident_id: str, operation_id: str | None) -> str | None:
    sealed = routine_state.call(lambda: self.routine_store.incident(team_id, incident_id))
    if sealed is None:
        raise routine_state.unavailable()
    operations = routine_incident.read_evidence(sealed, incident_id)["operations"]
    return next((item[2] for item in operations if isinstance(item, list) and item[1:2] == [operation_id]), None)


def assess(self, team_id: str, incident_id: str) -> Assessment:
    """The held run's failed step under the Team's exact current contracts; any drift fails closed."""
    opened = routine_incident.open_recovery(self, team_id, incident_id)
    _name, network_id, active = self._team_assistants(team_id)
    if network_id != opened.recovery.binding.incarnation:
        raise _drift()
    try:
        plan = routine_plan.admit(
            opened.recovery.plan, routine_turn.contracts(tuple(active.values()), routine_pin.SCOPE_LOCALE)
        )
    except routine_plan.PlanError as exc:
        raise _drift() from exc
    cursor = opened.cursor
    if cursor.done(plan):
        return Assessment(opened, plan, None, network_id, None)
    step = plan.steps[cursor.step]
    action = active[step.assistant_id].spec.actions[step.action]
    state = _operation_state(self, team_id, incident_id, cursor.operation_id)
    return Assessment(opened, plan, action, network_id, state)


def proven(assessment: Assessment) -> str:
    """What Team-admitted evidence proves of the held step's operation.

    ``none``: nothing is uncertain; ``policy``: a Team-detected policy fault holds it; ``absent``: the evidence proves
    no business effect; else ``uncertain``. A policy fault, such as a secret echo or an invalid frame, is never
    admitted as absence, even of a read-only Action, so it is never verified away or retried (ADR-0092 section 6).
    """
    cursor = assessment.cursor
    if assessment.action is None or cursor.operation_id is None:
        return "none"
    if cursor.fault == "policy":
        return "policy"
    if cursor.fault == "unquiesced":
        # Team could not prove the workload stopped after an ambiguous outcome: nothing may verify or retry it yet.
        return "unquiesced"
    if cursor.absent or assessment.action.effect == "read_only":
        return "absent"
    if assessment.state in _ABSENT_STATES and not cursor.carried:
        return "absent"
    return "uncertain"


def _original_input(assessment: Assessment) -> dict[str, object] | None:
    """The exact input the operation was dispatched with, recomputed and bound by its sealed commitment."""
    cursor = assessment.cursor
    try:
        resolved = routine_plan.resolve(
            assessment.plan, assessment.step, cursor.selections(), cursor.started_at, lambda value: value
        )
    except routine_plan.PlanError:
        return None
    return resolved if routine_plan.commitment(resolved) == cursor.commitment else None


def verifier_request(assessment: Assessment) -> brain_runtime_client.ActionRequest | None:
    """The fixed verifier call the descriptor defines, or None when the original input cannot be proven."""
    verifier = assessment.action.verifier
    original = _original_input(assessment)
    if verifier is None or original is None:
        return None
    payload: dict[str, object] = {}
    for name, binding in verifier["input"].items():
        if binding == {"from": "operation_id"}:
            payload[name] = assessment.cursor.operation_id
            continue
        try:
            payload[name] = routine_plan.select(original, binding["pointer"])
        except routine_plan.PlanError:
            return None
    step = assessment.step
    return brain_runtime_client.ActionRequest(VERIFY_INTERRUPT, step.assistant_id, verifier["action"], payload)


def _provider(self, team_id: str) -> str:
    try:
        return self.inference_store.load(team_id).provider
    except inference_config.InferenceConfigError as exc:
        raise ApiProblem(503, "Team model provider is not configured", code="inference-unavailable") from exc


def _call_verifier(self, team_id: str, token: str, assessment: Assessment, request) -> object:
    """Run the verifier in its own released generation; None when it failed, paused for a person, or was refused."""
    runtime = VerifierRuntime(request)
    generation = record.generation_for(assessment.network_id, assessment.cursor.binding.run_id, VERIFY_SUFFIX)
    segment = SegmentRequest(
        team_id=team_id,
        file_ids=[],
        assistant_ids=(assessment.step.assistant_id,),
        provider=_provider(self, team_id),
        api_key="",
        token=token,
        message="",
        routine=RoutineSegment(assessment.cursor.binding.run_id, generation, runtime, held=False),
        progress=chat_progress.Reporter(),
    )
    try:
        self.action_state.discard(generation)
        result = self._run_chat_segment(segment)
    except ApiProblem, routine_compiled.CompiledRunError:
        return None
    finally:
        with contextlib.suppress(action_journal.ActionJournalError):
            self.action_state.discard(generation)
    return runtime.result if isinstance(result.outcome, chat_orchestrator.ChatOutcome) else None


def _judge(self, team_id: str, assessment: Assessment, result: object) -> str:
    """Admit the verifier's evidence: complete the step on occurrence, record proven absence, else inconclusive."""
    verifier, cursor = assessment.action.verifier, assessment.cursor
    try:
        outcome = routine_plan.select(result, verifier["outcome"])
    except routine_plan.PlanError:
        return "inconclusive"
    if outcome == "not_occurred":
        _seal(self, team_id, routine_cursor.proven_absent(cursor))
        return "absent"
    if outcome != "occurred":
        return "inconclusive"
    try:
        recovered = assistant_spec.validate_action_payload(
            assessment.action, "output", routine_plan.select(result, verifier["result"])
        )
        completed = routine_cursor.complete(cursor, assessment.plan, recovered)
    except routine_plan.PlanError, routine_cursor.CursorError, ValueError:
        return "inconclusive"
    _seal(self, team_id, completed)
    return "occurred"


def verify(self, team_id: str, incident_id: str, token: str, *, budgeted: bool) -> str:
    """Verify a held run's failed step with no model.

    Returns ``occurred``, ``absent``, ``none``, ``inconclusive``, ``unverifiable``, or ``exhausted``. An automatic
    verification spends its budget before the call.
    """
    assessment = assess(self, team_id, incident_id)
    verdict = proven(assessment)
    if verdict in {"none", "policy", "unquiesced"}:
        return verdict
    if verdict == "absent":
        if not assessment.cursor.absent:
            _seal(self, team_id, routine_cursor.proven_absent(assessment.cursor))
        return "absent"
    request = verifier_request(assessment)
    if request is None:
        return "unverifiable"
    if budgeted:
        try:
            spent = routine_cursor.spend(assessment.cursor, "verifications", 1)
        except routine_cursor.CursorError:
            return "exhausted"
        assessment = Assessment(
            routine_incident.OpenedRecovery(assessment.opened.recovery, _seal(self, team_id, spent)),
            assessment.plan,
            assessment.action,
            assessment.network_id,
            assessment.state,
        )
    result = _call_verifier(self, team_id, token, assessment, request)
    return "inconclusive" if result is None else _judge(self, team_id, assessment, result)


def refusal(cursor: routine_cursor.Cursor) -> str | None:
    """Why a held run may not continue yet: an operation still uncertain, or its one retry already spent."""
    if cursor.operation_id is None:
        return None
    if cursor.fault == "policy":
        return "routine-policy-hold"
    if cursor.fault == "unquiesced":
        return "routine-workload-unquiesced"
    if not cursor.absent:
        return "routine-operation-uncertain"
    return None if cursor.remaining("retries") else "routine-retry-exhausted"


def continue_run(self, team_id: str, incident_id: str, token: str, progress=None) -> str:
    """Resume a held run as a continuation in its next generation; returns how that continuation ended.

    It never passes an uncertain operation: only a completed step or a proven absence with its retry left continues.
    """
    opened = routine_incident.open_recovery(self, team_id, incident_id)
    refused = refusal(opened.cursor)
    if refused is not None:
        raise ApiProblem(409, "Routine run cannot continue", code=refused)
    try:
        cursor = _seal(self, team_id, routine_cursor.continued(opened.cursor))
    except routine_cursor.CursorError as exc:
        raise ApiProblem(409, "Routine run cannot continue", code=exc.code) from exc
    generation = record.generation_for(opened.recovery.binding.incarnation, incident_id, cursor.generation_suffix)
    now = int(time.time())

    def reopen(state: record.TeamRoutines):
        try:
            reopened, lease_token = record.reopen_incident(state, incident_id, now, generation)
        except record.RoutineStateError as exc:
            return state, str(exc)
        routine = record.routine(reopened, opened.recovery.binding.routine_id)
        return reopened, (record.run(reopened, incident_id), routine, lease_token)

    reopened = routine_state.update(self, team_id, reopen)
    if isinstance(reopened, str):
        raise ApiProblem(409, "Routine run cannot continue", code=reopened)
    value, routine, lease_token = reopened
    # The resolved hold's evidence goes; a later hold of this continuation seals its own.
    routine_state.call(lambda: self.routine_store.delete_incident(team_id, incident_id))
    lease = record.lease_of(lease_token, record.HUMAN_LEASE)
    run = routine_run._Run(team_id, incident_id, lease, token, _provider(self, team_id), routine)
    with routine_run.registered(self, team_id, incident_id, token, value.active_seconds_left):
        outcome = routine_compiled.execute(self, run, value, progress)
    routine_run._after_run(self, team_id, incident_id, routine.routine_id, outcome)
    return outcome


def _diagnostics(self, team_id: str, assessment: Assessment) -> list[dict[str, object]]:
    """The failed step's sanitized failure evidence, as untrusted data; unreadable diagnostics are simply absent."""
    try:
        found = self.routine_diagnostics.read(
            team_id, assessment.network_id, assessment.cursor.binding.run_id, int(time.time())
        )
    except routine_diagnostics.DiagnosticStoreError:
        return []
    operation = assessment.cursor.operation_id
    return [{"failure": item.failure, "condition": item.condition} for item in found if item.operation_id == operation][
        -inference_recovery.MAX_DIAGNOSTICS :
    ]


def _decide(self, team_id: str, incident_id: str, api_key: str, locale: str | None) -> str:
    """Ask the Brain once whether to retry, ask, or pause; its call and output are paid for before it is made."""
    assessment = assess(self, team_id, incident_id)
    try:
        spent = routine_cursor.spend(assessment.cursor, "model_calls", 1)
        spent = routine_cursor.spend(spent, "output_tokens", MAX_OUTPUT_TOKENS)
    except routine_cursor.CursorError:
        return "exhausted"
    _seal(self, team_id, spent)
    try:
        config = self.inference_store.load(team_id)
    except inference_config.InferenceConfigError:
        return "unavailable"
    routine = assessment.opened.recovery
    settled = assessment.action.effect == "read_only" or assessment.state == "no_effect"
    proof = "no_effect" if settled else "not_occurred"
    subject = {
        "routine": {"name": _routine_name(self, team_id, routine.binding.routine_id), "request": routine.quote},
        "step": {"assistant": assessment.step.assistant_id, "action": assessment.step.action},
        "proof": proof,
    }
    try:
        return inference_recovery.decide(
            self.brain_runtime,
            (config.provider, config.model, api_key),
            locale,
            subject,
            _diagnostics(self, team_id, assessment),
        )
    except brain_runtime_client.BrainRuntimeError:
        return "unavailable"


def _routine_name(self, team_id: str, routine_id: str) -> str:
    state = routine_state.load(self, team_id)
    found = next((item for item in state.routines if item.routine_id == routine_id), None)
    return "Routine" if found is None else found.name


# What pauses the Routine when the episode ends on it, and the reason its notice gives.
_PAUSES = {"policy": "policy", "exhausted": "exhausted", "pause": "decided", "unavailable": "unavailable"}
# Evidence that lets the already-authorized run go on.
_GO_ON = frozenset({"occurred", "none", "retry"})


def _reserve(self, team_id: str, incident_id: str) -> int | None:
    """Spend the run's one episode and reserve all its remaining active time, durably, before any work.

    None when no episode may open. Whatever a crash leaves reserved stays spent, so a restart never refills it.
    """
    try:
        cursor = routine_incident.open_recovery(self, team_id, incident_id).cursor
        reserved = cursor.remaining("recovery_seconds")
        cursor = routine_cursor.spend(routine_cursor.spend(cursor, "episodes", 1), "recovery_seconds", reserved)
        _seal(self, team_id, cursor)
    except ApiProblem, routine_cursor.CursorError:
        return None
    return reserved


def _release(self, team_id: str, incident_id: str, reserved: int, started: float) -> None:
    """Return the reserved time the episode did not use; at least one second is always charged."""
    unused = reserved - min(reserved, max(1, math.ceil(_clock() - started)))
    if unused <= 0:
        return
    try:
        cursor = routine_incident.open_recovery(self, team_id, incident_id).cursor
        _seal(self, team_id, routine_cursor.refund(cursor, "recovery_seconds", unused))
    except ApiProblem, routine_cursor.CursorError:
        return


def _episode(self, run: routine_run._Run, api_key: str, deadline: float) -> bool:
    """Verify first, and ask the Brain only on proven absence; True when the run may go on.

    Past its deadline, the episode stops asking and pauses the Routine as exhausted; a policy fault, an exhausted
    budget, a pause decision, or a decision that could not be made pauses it too, with that reason on the notice.
    """
    team_id, incident_id = run.team_id, run.run_id
    verdict = verify(self, team_id, incident_id, run.token, budgeted=True)
    if verdict == "absent":
        verdict = _decide(self, team_id, incident_id, api_key, None) if _clock() < deadline else "exhausted"
    if verdict in _GO_ON and _clock() >= deadline:
        verdict = "exhausted"
    reason = _PAUSES.get(verdict)
    if reason is not None:
        routine_incident.pause(self, team_id, incident_id, reason)
    return verdict in _GO_ON and not self._chat_cancelled(run.token)


def automatic(self, run: routine_run._Run, api_key: str, progress=None) -> str:
    """The run's one automatic recovery episode, right after its hold, in the same execution slot (ADR-0092).

    The episode and its whole time budget are reserved durably first, and it runs registered with that deadline, so
    Stop, deletion, and the watchdog reach it and a restart never refills it. Linked verification comes first and
    needs no model; only a proven absence asks the Brain, once, whether the same step should be retried. The unused
    time is returned before the already-authorized run goes on under its own active time.
    """
    team_id, incident_id = run.team_id, run.run_id
    reserved = _reserve(self, team_id, incident_id)
    if reserved is None:
        return "held"
    started = _clock()
    try:
        with routine_run.registered(self, team_id, incident_id, run.token, reserved):
            try:
                go_on = _episode(self, run, api_key, started + reserved)
            finally:
                _release(self, team_id, incident_id, reserved, started)
            if not go_on or refusal(routine_incident.open_recovery(self, team_id, incident_id).cursor) is not None:
                return "held"
            return continue_run(self, team_id, incident_id, run.token, progress)
    except ApiProblem:
        return "held"
