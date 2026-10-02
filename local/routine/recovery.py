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
import dataclasses
import datetime
import math
import threading
import time
from dataclasses import dataclass

from docker.errors import DockerException

from action import journal as action_journal
from assistant import spec as assistant_spec
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from inference import client as brain_runtime_client
from inference import config as inference_config
from inference import recovery as inference_recovery
from local import audit as local_audit
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
from routine import hold as routine_hold
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
# Team's own classifications of a failed read-only attempt that admit it had no business effect: a handled failure,
# a transport fault after a proven fail-stop, or a refusal outside the RPC. A missing classification never does.
_TRUSTED_FAULTS = frozenset({"handled", "transport", "other"})


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

    def dispatching(self, _request, _operation_id, _workload="") -> None:
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
    no business effect; ``unclassified``: a read-only step whose failure Team never classified; else ``uncertain``.
    Absence needs positive trusted evidence: a verifier's proof, the journal's, or Team's own classification of a
    read-only failure. A policy fault, such as a secret echo or an invalid frame, is never admitted as absence, and a
    classification that was never sealed holds the run as evidence, so it is never verified away or retried
    (ADR-0092 section 6).
    """
    cursor = assessment.cursor
    if assessment.action is None or cursor.operation_id is None:
        return "none"
    if cursor.fault == "policy":
        return "policy"
    if cursor.fault == "unquiesced":
        # Team could not prove the workload stopped after an ambiguous outcome: nothing may verify or retry it yet.
        return "unquiesced"
    if cursor.absent or (assessment.state in _ABSENT_STATES and not cursor.carried):
        return "absent"
    if assessment.action.effect == "read_only":
        return "absent" if cursor.fault in _TRUSTED_FAULTS else "unclassified"
    return "uncertain"


def _docker_instant(value: object) -> int | None:
    """Docker's RFC 3339 instant as whole UTC epoch seconds, or None when it is not one."""
    if not isinstance(value, str) or len(value) < 20 or value[19] not in ".Z":
        return None
    try:
        return int(datetime.datetime.fromisoformat(value[:19] + "+00:00").timestamp())
    except ValueError:
        return None


def _workload_state(self, team_id: str, assistant_id: str, workload: str) -> dict[str, object] | None:
    """The attempt's container state as Docker reports it; not running when that container is gone or replaced.

    None when it cannot be read, which is never proof.
    """
    try:
        container = self.assistant_lifecycle._assistant_container(team_id, assistant_id)
        if container.id != workload:
            return {"Running": False}
        container.reload()
        state = getattr(container, "attrs", {}).get("State")
    except ApiProblem as exc:
        return {"Running": False} if exc.code == "assistant-not-found" else None
    except DockerException:
        return None
    return state if isinstance(state, dict) else None


def _quiesced(self, team_id: str, assessment: Assessment) -> bool:
    """Whether the workload of the held attempt is proven to have stopped since it was dispatched.

    A Team crash or Stop leaves no classification, and the original Docker execution may still be running, so an
    absence observed now would prove nothing about a later effect. Proof is the attempt's container being gone or
    replaced, stopped, or started again after the attempt.
    """
    cursor = assessment.cursor
    state = _workload_state(self, team_id, assessment.step.assistant_id, cursor.workload) if cursor.workload else None
    if state is None:
        return False
    if state.get("Running") is False:
        return True
    started = _docker_instant(state.get("StartedAt"))
    return started is not None and started > cursor.dispatched_at


def quiescence(self, team_id: str, assessment: Assessment, verdict: str) -> str:
    """An uncertain operation whose failure Team never classified needs its workload proven stopped first."""
    if verdict == "uncertain" and assessment.cursor.fault == "" and not _quiesced(self, team_id, assessment):
        return "unquiesced"
    return verdict


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
    verdict = quiescence(self, team_id, assessment, proven(assessment))
    if verdict in {"none", "policy", "unquiesced", "unclassified"}:
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


def continue_run(self, team_id: str, incident_id: str, token: str, progress=None, *, seconds: int | None = None) -> str:
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
        # A Stop or deletion that reached the recovery's registration is final: nothing continues the run.
        if self._chat_cancelled(token):
            return state, "routine-recovery-stopped"
        try:
            reopened, lease_token = routine_hold.reopen_incident(state, incident_id, now, generation)
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
    active = value.active_seconds_left if seconds is None else min(seconds, value.active_seconds_left)
    with routine_run.registered(self, team_id, incident_id, token, active):
        outcome = routine_compiled.execute(self, run, value, progress)
    routine_run._after_run(self, team_id, incident_id, routine.routine_id, outcome)
    return outcome


def _diagnostics(self, team_id: str, assessment: Assessment) -> list[dict[str, object]] | None:
    """The failed step's sanitized failure evidence, as untrusted data.

    Diagnostics that were never kept or have expired are simply absent; unreadable or corrupt ones are None, which no
    model is ever asked about.
    """
    try:
        found = self.routine_diagnostics.read(
            team_id, assessment.network_id, assessment.cursor.binding.run_id, int(time.time())
        )
    except routine_diagnostics.DiagnosticStoreError:
        return None
    operation = assessment.cursor.operation_id
    return [{"failure": item.failure, "condition": item.condition} for item in found if item.operation_id == operation][
        -inference_recovery.MAX_DIAGNOSTICS :
    ]


def _decide(self, team_id: str, incident_id: str, api_key: str, locale: str | None) -> str:
    """Ask the Brain once whether to retry, ask, or pause; its call and output are paid for before it is made.

    Recovery evidence that cannot be read is ``evidence``: the Routine pauses and the model is never asked.
    """
    assessment = assess(self, team_id, incident_id)
    diagnostics = _diagnostics(self, team_id, assessment)
    if diagnostics is None:
        return "evidence"
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
            diagnostics,
        )
    except brain_runtime_client.BrainRuntimeError:
        return "unavailable"


def _routine_name(self, team_id: str, routine_id: str) -> str:
    state = routine_state.load(self, team_id)
    found = next((item for item in state.routines if item.routine_id == routine_id), None)
    return "Routine" if found is None else found.name


# What pauses the Routine when the episode ends on it, and the reason its notice gives.
_PAUSES = {
    "policy": "policy",
    "exhausted": "exhausted",
    "pause": "decided",
    "unavailable": "unavailable",
    "evidence": "evidence",
    "unclassified": "evidence",
}
# Evidence that lets the already-authorized run go on.
_GO_ON = frozenset({"occurred", "none", "retry"})


@dataclass(frozen=True, slots=True)
class _Reservation:
    """The time an episode reserved from both the run's recovery budget and its remaining active time."""

    seconds: int
    generation: str
    started: float
    # Set by the deadline timer when the reservation ran out, whatever the episode was doing.
    expired: threading.Event = dataclasses.field(default_factory=threading.Event)

    @property
    def deadline(self) -> float:
        return self.started + self.seconds


def _reserve(self, team_id: str, incident_id: str) -> _Reservation | None:
    """Spend the run's one episode and reserve its time from both balances, durably, before any work.

    The reservation is the smaller of the recovery budget and the held run's remaining active time, so recovery never
    outlasts the run. None when no episode may open. A crash keeps it spent, so a restart never refills either.
    """
    try:
        cursor = routine_incident.open_recovery(self, team_id, incident_id).cursor
        held = routine_hold.incident(routine_state.load(self, team_id), incident_id)
        seconds = max(0, min(cursor.remaining("recovery_seconds"), held.active_seconds_left))
        cursor = routine_cursor.spend(cursor, "episodes", 1)
        _seal(self, team_id, routine_cursor.spend(cursor, "recovery_seconds", seconds) if seconds else cursor)
    except ApiProblem, routine_cursor.CursorError, record.RoutineStateError:
        return None
    if seconds:
        routine_state.update(
            self, team_id, lambda state: (routine_hold.charge_incident(state, incident_id, seconds), None)
        )
    return _Reservation(seconds, held.generation, _clock())


def _release(self, team_id: str, incident_id: str, reservation: _Reservation) -> None:
    """Return the reserved time the episode did not use to both balances; at least one second is always charged.

    The run's active time goes back only to the same held run; once it continued, its continuation was charged.
    """
    unused = reservation.seconds - min(reservation.seconds, max(1, math.ceil(_clock() - reservation.started)))
    if unused <= 0:
        return
    try:
        cursor = routine_incident.open_recovery(self, team_id, incident_id).cursor
        _seal(self, team_id, routine_cursor.refund(cursor, "recovery_seconds", unused))
    except ApiProblem, routine_cursor.CursorError:
        return
    routine_state.update(
        self,
        team_id,
        lambda state: (routine_hold.refund_incident(state, incident_id, reservation.generation, unused), None),
    )


@contextlib.contextmanager
def _deadline(self, team_id: str, incident_id: str, token: str, reservation: _Reservation):
    """A direct timer that cancels the episode at its absolute deadline, whatever it is doing.

    At the deadline it marks the reservation expired and stops the registered recovery: its token, its Brain request,
    and any Action in flight. The watchdog still reconciles a timer that could not act.
    """

    def expire() -> None:
        try:
            # Bound to this execution's token: a late timer never stops a later recovery of the same incident, and the
            # deadline is marked as the cause only if no person stopped it first.
            routine_run.expire_routine_run(self, team_id, incident_id, token, reservation.expired.set)
        except ApiProblem:
            local_audit.record_request("routine-recovery", result="error", team_id=team_id, detail="deadline-stop")

    # The absolute deadline, not a fresh delay: the time since the reservation was taken is already gone.
    timer = threading.Timer(max(0.0, reservation.deadline - _clock()), expire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def _stopped(self, token: str, reservation: _Reservation) -> bool:
    """Whether a person stopped the recovery: it was cancelled, and not by its own deadline."""
    return self._chat_cancelled(token) and not reservation.expired.is_set()


def _episode(self, run: routine_run._Run, api_key: str, reservation: _Reservation) -> bool:
    """Verify first, and ask the Brain only on proven absence; True when the run may go on.

    Exhaustion is judged on the reservation's own clock, never on what the episode found: once its deadline passed,
    the Routine pauses as exhausted. Otherwise a policy fault, missing evidence, an exhausted budget, a pause decision,
    or a decision that could not be made pauses it, with that reason on the notice.
    """
    team_id, incident_id = run.team_id, run.run_id

    def expired() -> bool:
        return reservation.expired.is_set() or _clock() >= reservation.deadline

    # Nothing is dispatched once the reservation has run out, not even the verifier.
    verdict = "exhausted" if expired() else verify(self, team_id, incident_id, run.token, budgeted=True)
    if verdict == "absent" and not expired() and not self._chat_cancelled(run.token):
        verdict = _decide(self, team_id, incident_id, api_key, None)
    if _stopped(self, run.token, reservation):
        # A person's Stop is no failure and outranks anything later: an aborted Brain call is not unavailable, a
        # deadline passing afterwards is not exhaustion, nothing is published, and the incident stays for the card.
        return False
    if expired():
        verdict = "exhausted"
    reason = _PAUSES.get(verdict)
    if reason is not None:
        routine_incident.pause(self, team_id, incident_id, reason)
    return verdict in _GO_ON and not self._chat_cancelled(run.token)


def _go_on(self, run: routine_run._Run, reservation: _Reservation, progress) -> str:
    """Continue the already-authorized run inside what is left of the reservation, or hold it.

    The retry is never dispatched without time left for it, and a continuation the deadline cuts, even between
    steps, is held again with its partial evidence while the Routine pauses as exhausted.
    """
    team_id, incident_id = run.team_id, run.run_id
    if refusal(routine_incident.open_recovery(self, team_id, incident_id).cursor):
        return "held"
    left = max(0, math.floor(reservation.deadline - _clock()))
    if _stopped(self, run.token, reservation):
        return "held"
    if not left:
        routine_incident.pause(self, team_id, incident_id, "exhausted")
        return "held"
    outcome = continue_run(self, team_id, incident_id, run.token, progress, seconds=left)
    if outcome == "held" and reservation.expired.is_set():
        routine_incident.pause(self, team_id, incident_id, "exhausted")
    return outcome


def automatic(self, run: routine_run._Run, api_key: str, progress=None) -> str:
    """The run's one automatic recovery episode, right after its hold, in the same execution slot (ADR-0092).

    The episode and its time are reserved durably from both the recovery budget and the run's active time first, and
    it runs registered with that deadline, so Stop, deletion, and the watchdog reach it and a restart never refills
    it. Linked verification comes first and needs no model; only a proven absence asks the Brain, once, whether the
    same step should be retried, and that retry runs inside the same allowance. Unused time is returned at the end. A
    person's Stop outranks every other outcome: it publishes nothing.
    """
    team_id, incident_id = run.team_id, run.run_id
    # A person who stopped the run before its episode opened: nothing is reserved, spent, or published.
    reservation = None if self._chat_cancelled(run.token) else _reserve(self, team_id, incident_id)
    if reservation is None:
        return "held"
    try:
        if not reservation.seconds:
            # No time is left in either balance: the episode is exhausted before it starts.
            routine_incident.pause(self, team_id, incident_id, "exhausted")
            return "held"
        with (
            routine_run.registered(self, team_id, incident_id, run.token, reservation.seconds),
            _deadline(self, team_id, incident_id, run.token, reservation),
        ):
            try:
                return _go_on(self, run, reservation, progress) if _episode(self, run, api_key, reservation) else "held"
            finally:
                _release(self, team_id, incident_id, reservation)
    except ApiProblem:
        return "held"
