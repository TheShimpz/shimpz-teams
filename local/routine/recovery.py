"""Verifying and continuing a held Routine run, with no model (ADR-0092 sections 4, 6, and 7).

A held run's failed step is assessed from its sealed cursor, recovery snapshot, and incident evidence. Its operation
is proven absent when Team-admitted evidence says so: a reviewed read-only Action, a journal that settled it without
effect, or a suspension before any effect. Anything else stays uncertain until the Action's fixed read-only verifier
is run under its exact descriptor: the operation's logical id or every required original input whole, and nothing a
model chose. ``occurred`` with a recovered result valid under the original output schema completes the step;
``not_occurred`` proves absence, which retries the same operation once in the whole run; anything else, including a
failure of the verifier, is inconclusive and leaves the run held for a person. Exceptions, validation messages, HTTP
status codes, expired credentials, and not-found responses are never proof by themselves.

A verifier call runs in its own journal generation, released when it ends. A continuation resumes the held run in a
fresh generation under a fresh internal lease, in the Team's execution slot, and an uncertain operation is never
passed: it is retried only after proven absence, or the run is held again.
"""

import contextlib
import dataclasses
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from action import execution as action_execution
from action import journal as action_journal
from assistant import spec as assistant_spec
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from inference import client as brain_runtime_client
from local import audit as local_audit
from local import errors as local_errors
from local.chat.segment import RoutineSegment, SegmentRequest
from local.errors import DOCKER_ERRORS
from local.errors import ApiProblemError as ApiProblem
from local.routine import contracts as routine_contracts
from local.routine import diagnostics as routine_diagnostics
from local.routine import incident as routine_incident
from local.routine import replay as routine_replay
from local.routine import run as routine_run
from local.routine import state as routine_state
from local.routine import store as routine_store
from local.validation import docker_instant_ns
from routine import claim as routine_claim
from routine import cursor as routine_cursor
from routine import hold as routine_hold
from routine import plan as routine_plan
from routine import record, trace

VERIFY_SUFFIX = "v1"
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
    """One fixed read-only verifier call in place of a Brain turn; it never calls a model.

    The call belongs to the held run, so every value Team injects into it joins that run's protection before its RPC.
    """

    def __init__(self, request: brain_runtime_client.ActionRequest, protect: Callable[[object], object]) -> None:
        self._request = request
        self.protect = protect
        self.result: object = None

    def start(self, _context, _message, *, conversation) -> brain_runtime_client.RuntimeTurn:
        return brain_runtime_client.RuntimeTurn("action-required", "", (self._request,))

    def resume(self, _context, results) -> brain_runtime_client.RuntimeTurn:
        self.result = results.get(self._request.interrupt_id)
        return brain_runtime_client.RuntimeTurn("completed", "", ())

    def dispatching(self, _request, _operation_id, _workload="", evidence=None) -> None:
        if evidence is not None:
            self.protect(routine_diagnostics.protected(evidence))

    def failed(self, _request, _evidence, _exc) -> None:
        return

    def returned(self) -> None:
        return

    @staticmethod
    def logical_operation(_request) -> None:
        return None

    @staticmethod
    def purpose(_context, _request, _assistant_name, _summary) -> None:
        return

    @staticmethod
    def strategy() -> dict[str, object]:
        """One verifier round, and nothing kept for learning."""
        return {"max_rounds": 1, "record_invoked": False}


def _drift() -> ApiProblem:
    return ApiProblem(409, "Routine contracts changed since the run", code="routine-drift")


def _seal(self, team_id: str, cursor: routine_cursor.Cursor) -> routine_cursor.Cursor:
    try:
        self.routine_store.put_cursor(team_id, cursor)
    except routine_store.RoutineStoreError as exc:
        raise local_errors.routine_state_unavailable() from exc
    return cursor


def _operation_state(self, team_id: str, incident_id: str, operation_id: str | None) -> str | None:
    sealed = routine_state.call(lambda: self.routine_store.incident(team_id, incident_id))
    if sealed is None:
        raise local_errors.routine_state_unavailable()
    operations = routine_incident.read_evidence(sealed, incident_id)["operations"]
    return next((item[2] for item in operations if isinstance(item, list) and item[1:2] == [operation_id]), None)


def assess(self, team_id: str, incident_id: str) -> Assessment:
    """The held run's failed step under the Team's exact current contracts; any drift fails closed."""
    opened = routine_incident.open_recovery(self, team_id, incident_id)
    _name, network_id, active = self._team_assistants(team_id)
    if network_id != opened.recovery.binding.incarnation:
        raise _drift()
    try:
        plan = routine_plan.admit(opened.recovery.plan, routine_contracts.contracts(tuple(active.values())))
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

    ``none``: nothing is uncertain; ``policy``: a Team-detected policy fault holds it; ``unquiesced``: Team could not
    prove the workload stopped after an ambiguous outcome; ``absent``: the evidence proves no business effect;
    ``unclassified``: a read-only step whose failure Team never classified; else ``uncertain``.
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
    except DOCKER_ERRORS:
        return None
    return state if isinstance(state, dict) else None


def _stopped_since(self, team_id: str, assistant_id: str, workload: str, dispatched_at: int) -> bool:
    """Whether the workload an attempt was dispatched to is proven to have stopped since it was dispatched.

    A Team crash or Stop leaves no classification, and the original Docker execution may still be running, so an
    absence observed now would prove nothing about a later effect. Proof is the attempt's container being gone or
    replaced, stopped, or started again after the attempt.
    """
    state = _workload_state(self, team_id, assistant_id, workload) if workload else None
    if state is None:
        return False
    if state.get("Running") is False:
        return True
    started = docker_instant_ns(state.get("StartedAt"))
    return started is not None and started // 10**9 > dispatched_at


def _quiesced(self, team_id: str, assessment: Assessment) -> bool:
    cursor = assessment.cursor
    return _stopped_since(self, team_id, assessment.step.assistant_id, cursor.workload, cursor.dispatched_at)


def workload_stopped(self, team_id: str, opened: routine_incident.OpenedRecovery) -> bool:
    """Whether nothing of the held attempt can still be running, so a fresh run or a replacement may start.

    No operation is open, the attempt ended in a way Team itself classified, the journal proves it never acted, or its
    workload is proven stopped since it was dispatched. An attempt with no recorded workload and no such proof stays
    unknown. It reads only sealed evidence, never the current contracts, so a held run of a Routine whose Assistants
    changed can still be settled. An already accepted provider request is beyond what any local proof can stop.
    """
    cursor = opened.cursor
    if cursor.operation_id is None or cursor.fault in _TRUSTED_FAULTS:
        return True
    if not cursor.workload:
        state = _operation_state(self, team_id, cursor.binding.run_id, cursor.operation_id)
        return state in _ABSENT_STATES and not cursor.carried
    steps = opened.recovery.plan["steps"]
    assistant_id = steps[min(cursor.step, len(steps) - 1)]["assistant"]
    return _stopped_since(self, team_id, assistant_id, cursor.workload, cursor.dispatched_at)


def _quiescence(self, team_id: str, assessment: Assessment, verdict: str) -> str:
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


def _call_verifier(self, team_id: str, token: str, assessment: Assessment, request) -> object:
    """Run the verifier in its own released generation; None when it failed, paused for a person, or was refused."""
    run_id = assessment.cursor.binding.run_id
    runtime = VerifierRuntime(request, lambda values: self.routine_protections.grow(run_id, values))
    generation = routine_claim.generation_for(assessment.network_id, run_id, VERIFY_SUFFIX)
    segment = SegmentRequest(
        team_id=team_id,
        file_ids=[],
        assistant_ids=(assessment.step.assistant_id,),
        provider=None,
        api_key="",
        token=token,
        message="",
        routine=RoutineSegment(assessment.cursor.binding.run_id, generation, runtime, held=False),
        progress=chat_progress.Reporter(),
    )
    try:
        self.action_state.discard(generation)
        result = self._run_chat_segment(segment)
    except ApiProblem, routine_replay.ReplayError:
        return None
    finally:
        with contextlib.suppress(action_journal.ActionJournalError):
            self.action_state.discard(generation)
    return runtime.result if isinstance(result.outcome, chat_orchestrator.ChatOutcome) else None


def _judge(self, team_id: str, assessment: Assessment, result: object, protected: tuple[str, ...] | None) -> str:
    """Admit the verifier's evidence: complete the step on occurrence, record proven absence, else inconclusive."""
    verifier, cursor = assessment.action.verifier, assessment.cursor
    try:
        outcome = routine_plan.select(result, verifier["outcome"])
    except routine_plan.PlanError:
        return "inconclusive"
    if outcome == "not_occurred":
        _seal(self, team_id, routine_cursor.proven_absent(cursor))
        return "absent"
    if outcome != "occurred" or protected is None:
        return "inconclusive"
    try:
        recovered = assistant_spec.validate_action_payload(
            assessment.action, "output", routine_plan.select(result, verifier["result"])
        )
        # A recovered result continues the run and may be shown, so it must pass the secret-echo check of the very
        # attempt it stands for: against the private values that attempt was given, held in memory since it failed.
        if action_execution.contains_secret(recovered, dict(enumerate(map(str, protected)))):
            return "inconclusive"
        step = assessment.plan.steps[cursor.step]
        protection = self.routine_protections.grow(
            cursor.binding.run_id, trace.secret_values(recovered, step.output_schema)
        )
        completed = routine_replay.advance(
            self.routine_store, team_id, cursor, assessment.plan, (recovered, protection)
        )
    except routine_plan.PlanError, routine_cursor.CursorError, routine_store.RoutineStoreError, ValueError:
        return "inconclusive"
    _seal(self, team_id, completed)
    routine_replay.record_recovered(self, team_id, cursor, assessment.plan, protected)
    return "occurred"


def verify(
    self, team_id: str, incident_id: str, token: str, protected: dict[str, tuple[str, ...]] | None = None
) -> str:
    """Verify a held run's failed step with no model, once, inside the run's one recovery episode.

    Returns ``occurred``, ``absent``, ``none``, ``inconclusive``, or ``unverifiable``; or ``policy``, ``unquiesced``,
    or ``unclassified`` when verification may not run.
    """
    assessment = assess(self, team_id, incident_id)
    verdict = _quiescence(self, team_id, assessment, proven(assessment))
    if verdict in {"none", "policy", "unquiesced", "unclassified"}:
        return verdict
    if verdict == "absent":
        if not assessment.cursor.absent:
            _seal(self, team_id, routine_cursor.proven_absent(assessment.cursor))
        return "absent"
    request = verifier_request(assessment)
    if request is None:
        return "unverifiable"
    result = _call_verifier(self, team_id, token, assessment, request)
    if result is None:
        return "inconclusive"
    # Without the failed attempt's own private values in memory, as after a restart, nothing proves the recovered
    # result echoes none of them: the effect stays inconclusive.
    original = None if protected is None else protected.get(assessment.cursor.operation_id)
    return _judge(self, team_id, assessment, result, original)


def blocked(cursor: routine_cursor.Cursor) -> bool:
    """Whether a held run may not continue: its operation is still uncertain, held for policy, or not proven stopped."""
    return cursor.operation_id is not None and (cursor.fault in {"policy", "unquiesced"} or not cursor.absent)


def continue_run(self, team_id: str, incident_id: str, token: str, progress=None, *, seconds: int) -> str:
    """Resume a held run as its one continuation, in its next generation; how it ended, or ``held`` when it cannot.

    It never passes an uncertain operation: only a completed step or a proven absence continues, and the continuation
    runs within ``seconds`` of the episode's deadline and the run's own active time.
    """
    opened = routine_incident.open_recovery(self, team_id, incident_id)
    if blocked(opened.cursor):
        return "held"
    try:
        cursor = _seal(self, team_id, routine_cursor.continued(opened.cursor))
    except routine_cursor.CursorError:
        return "held"
    generation = routine_claim.generation_for(
        opened.recovery.binding.incarnation, incident_id, cursor.generation_suffix
    )
    now = int(time.time())

    def reopen(state: record.TeamRoutines):
        try:
            reopened, lease_token = routine_hold.reopen_incident(state, incident_id, now, generation)
        except record.RoutineStateError:
            return state, None
        routine = record.routine(reopened, opened.recovery.binding.routine_id)
        return reopened, (record.run(reopened, incident_id), routine, lease_token)

    # A Stop, deletion, or deadline that reached the recovery's registration is final: the reopening commits under the
    # same guard they cancel under, so nothing continues the run once any of them landed.
    results: list[object] = []
    if not routine_run.unstopped(
        self, token, lambda: False, lambda: results.append(routine_state.update(self, team_id, reopen))
    ):
        return "held"
    (reopened,) = results
    if reopened is None:
        return "held"
    value, routine, lease_token = reopened
    # The resolved hold's evidence goes; a later hold of this continuation seals its own.
    routine_state.call(lambda: self.routine_store.delete_incident(team_id, incident_id))
    lease = record.lease_of(lease_token, record.HUMAN_LEASE)
    # The logical run's answered human requests carry on; a continuation never gets a fresh allowance.
    run = routine_run.LeasedRun(team_id, incident_id, lease, token, routine, (), value.requests_used)
    with routine_run.registered(self, team_id, incident_id, token, min(seconds, value.active_seconds_left)):
        # Its caller, the run that held, audits and settles how the whole run ended.
        return routine_replay.execute(self, run, value, progress)


# The most active time the run's one recovery episode may use, within the run's own time left.
RECOVERY_SECONDS = 60
# What pauses the Routine when the episode ends on it, and the reason its notice gives.
_PAUSES = {"policy": "policy", "exhausted": "exhausted", "unclassified": "evidence"}
# Evidence that lets the already-authorized run go on: a proven absence retries the same operation once.
_GO_ON = frozenset({"occurred", "none", "absent"})


@dataclass(slots=True)
class _Episode:
    """The run's one recovery episode in this execution: its time, from when, and how it ended."""

    seconds: int
    started: float
    # Set by the deadline timer when the episode's time ran out, whatever the episode was doing.
    expired: threading.Event = dataclasses.field(default_factory=threading.Event)
    # Whether the episode already published why it paused the Routine, so its ending never publishes a second reason.
    published: bool = False

    @property
    def deadline(self) -> float:
        return self.started + self.seconds


def _open(self, team_id: str, incident_id: str) -> _Episode | None:
    """Open the run's one episode durably, before any work, with its deadline in memory; None when none may open.

    Its time is the smaller of the recovery bound and the held run's remaining active time, so recovery never outlasts
    the run. The episode is sealed in the run's cursor first, so a crash or restart never opens another.
    """
    try:
        cursor = routine_incident.open_recovery(self, team_id, incident_id).cursor
        held = routine_hold.incident(routine_state.load(self, team_id), incident_id)
        _seal(self, team_id, routine_cursor.open_episode(cursor))
    except ApiProblem, routine_cursor.CursorError, record.RoutineStateError:
        return None
    return _Episode(max(0, min(RECOVERY_SECONDS, held.active_seconds_left)), _clock())


@contextlib.contextmanager
def _deadline(self, team_id: str, incident_id: str, token: str, episode: _Episode):
    """A direct timer that cancels the episode at its absolute deadline, whatever it is doing.

    At the deadline it marks the episode expired and stops the registered recovery: its token and any Action in
    flight. The watchdog still reconciles a timer that could not act.
    """

    def expire() -> None:
        try:
            # Bound to this execution's token: a late timer never stops a later recovery of the same incident, and the
            # deadline is marked as the cause only if no person stopped it first.
            routine_run.expire_routine_run(self, team_id, incident_id, token, episode.expired.set)
        except ApiProblem:
            local_audit.record_team_error("routine-recovery", "deadline-stop", team_id)

    # The absolute deadline, not a fresh delay: the time since the episode opened is already gone.
    timer = threading.Timer(max(0.0, episode.deadline - _clock()), expire)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def _stopped(self, token: str, episode: _Episode) -> bool:
    """Whether a person stopped the recovery: it was cancelled, and not by its own deadline."""
    return self._chat_cancelled(token) and not episode.expired.is_set()


def _episode(self, run: routine_run.LeasedRun, episode: _Episode) -> bool:
    """Verify first; True when the run may go on: a proven occurrence, or a proven absence that retries once.

    Exhaustion is judged on the episode's own clock, never on what the episode found: once its deadline passed, the
    Routine pauses as exhausted. Otherwise a policy fault or missing evidence pauses it, with that reason on the notice,
    and anything still uncertain is held for the person's card.
    """
    team_id, incident_id = run.team_id, run.run_id

    def expired() -> bool:
        return episode.expired.is_set() or _clock() >= episode.deadline

    # Nothing is dispatched once the episode's time has run out, not even the verifier.
    verdict = "exhausted" if expired() else verify(self, team_id, incident_id, run.token, run.protected)
    # Verification runs outside any segment: its whole seconds leave the run's balance, as a segment's do, so neither
    # the continuation nor a person's later resume reuses them.
    spent = int(_clock() - episode.started)
    routine_state.update(self, team_id, lambda state: (routine_hold.spend_incident(state, incident_id, spent), None))
    if _stopped(self, run.token, episode):
        # A person's Stop is no failure and outranks anything later: a deadline passing afterwards is not exhaustion,
        # nothing is published, and the incident stays for the card.
        return False
    if expired():
        verdict = "exhausted"
    reason = _PAUSES.get(verdict)
    if reason is not None:
        _publish(self, run, episode, reason)
    return verdict in _GO_ON and not self._chat_cancelled(run.token)


def _go_on(self, run: routine_run.LeasedRun, episode: _Episode, progress) -> str:
    """Continue the already-authorized run inside what is left of the episode, or hold it.

    The retry is never dispatched without time left for it, and a continuation the deadline cuts, even between
    steps, is held again with its partial evidence while the Routine pauses as exhausted.
    """
    team_id, incident_id = run.team_id, run.run_id
    left = max(0, math.floor(episode.deadline - _clock()))
    if _stopped(self, run.token, episode):
        return "held"
    if not left:
        _publish(self, run, episode, "exhausted")
        return "held"
    return continue_run(self, team_id, incident_id, run.token, progress, seconds=left)


def _publish(self, run: routine_run.LeasedRun, episode: _Episode, reason: str) -> None:
    """Pause the Routine the held run belongs to and say why, once per episode, unless a person stopped it first."""
    if routine_run.unstopped(
        self,
        run.token,
        episode.expired.is_set,
        lambda: routine_incident.pause(self, run.team_id, run.run_id, reason),
    ):
        episode.published = True


def _finish(self, run: routine_run.LeasedRun, episode: _Episode, outcome: str) -> str:
    """End the episode: a held run its deadline cut, however it got there, pauses the Routine as exhausted.

    Its evidence stays held either way. A failure to publish leaves the incident for the card.
    """
    if outcome == "held" and episode.expired.is_set() and not episode.published:
        with contextlib.suppress(ApiProblem):
            _publish(self, run, episode, "exhausted")
    return outcome


def automatic(self, run: routine_run.LeasedRun, progress=None) -> str:
    """The run's one automatic recovery episode, right after its hold, in the same execution slot (ADR-0092).

    The episode is sealed in the run's cursor first, so a restart never opens another, and it runs registered with its
    deadline in memory, so Stop, deletion, and the watchdog reach it. Linked verification comes first and needs no
    model; a proven absence retries the same step once, inside the same deadline. A person's Stop outranks every other
    outcome: it publishes nothing.
    """
    team_id, incident_id = run.team_id, run.run_id
    # A person who stopped the run before its episode opened: nothing is opened or published.
    episode = None if self._chat_cancelled(run.token) else _open(self, team_id, incident_id)
    if episode is None:
        return "held"
    try:
        if not episode.seconds:
            # The run has no active time left: the episode is exhausted before it starts.
            _publish(self, run, episode, "exhausted")
            return "held"
        with (
            routine_run.registered(self, team_id, incident_id, run.token, episode.seconds),
            _deadline(self, team_id, incident_id, run.token, episode),
        ):
            try:
                outcome = _go_on(self, run, episode, progress) if _episode(self, run, episode) else "held"
            except ApiProblem:
                # A refusal on the way, such as a continuation whose run cannot be read, still ends the episode as
                # held, with its evidence.
                outcome = "held"
            return _finish(self, run, episode, outcome)
    except ApiProblem:
        return "held"
