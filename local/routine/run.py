"""Local Routine runs: a fair claim across Teams, the lease and Stop of a run, and how a segment ends (ADR-0086)."""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import json
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus

from action import human as action_human
from chat import orchestrator as chat_orchestrator
from inference import config as inference_config
from local import audit as local_audit
from local import authority as local_authority
from local import errors as local_errors
from local.chat import continuation as local_chat_continuations
from local.chat.types import PendingLocalChat
from local.errors import ApiProblemError as ApiProblem
from local.routine import contracts as routine_contracts
from local.routine import manage as routine_manage
from local.routine import state as routine_state
from protocol.http.v1 import payload as http_payload
from routine import claim as routine_claim
from routine import record, trace
from routine import runs as routine_runs


@dataclass(frozen=True, slots=True)
class _Run:
    """One leased run's segment: its Team, lease, execution-slot token, provider, and Routine."""

    team_id: str
    run_id: str
    lease: record.Lease
    token: str
    provider: str
    routine: record.Routine
    # The human answers this logical run already gave, kept for deterministic replay if it freezes again.
    transcripts: tuple[action_human.ActionTranscript, ...] = ()
    requests_used: int = 0
    # The private values Team injected into each operation's last failed attempt, by operation id, held in memory only
    # for this execution's one automatic recovery episode, which checks a recovered result against them (ADR-0092).
    protected: dict[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Registration:
    """A segment in the execution slot: what Stop needs to reach it, and when its active time runs out."""

    team_id: str
    token: str
    deadline: float
    overdue: bool = False


# How long after a Routine frees the slot a person's chat message, refused while that Routine held it, keeps further
# runs of its Team waiting for the person's turn.
CHAT_PRIORITY_SECONDS = 30


def _chat_busy(self, team_id: str) -> bool:
    """Chat has priority at admission (ADR-0092 section 9).

    A Routine never starts beside a chat turn or a pending chat challenge, nor while a person who found the slot held
    by a Routine is still waiting for their turn: however short a continuous Routine's gap, chat gets the next boundary.
    """
    with self._active_chat_guard:
        demand = self._chat_demand.get(team_id)
    waiting = demand is not None and time.monotonic() - demand < CHAT_PRIORITY_SECONDS
    return (
        waiting
        or self._chat_lock(team_id).locked()
        or self.human_challenges.current(team_id) is not None
        or self.integration_challenges.current(team_id) is not None
    )


def _claim(self, team_id: str, state: record.TeamRoutines, now: int, key: str, long: bool):
    """Sweep, check the oldest due Routine's pinned contracts, then lease one run of it, a long one only if allowed."""
    state = routine_claim.sweep(state, now)
    due = routine_claim.claimable(state, now, long)
    if due is None:
        return state, None
    pinned = dict(due.assistants)
    try:
        current = routine_contracts.current_contracts(self, team_id, tuple(pinned))
    except routine_contracts.ContractsUnavailableError:
        # Nothing is proven: the Routine stays due, unchanged, and the next claim checks it again.
        return state, None
    if current != pinned:
        changed = sorted(assistant for assistant, digest in pinned.items() if current.get(assistant) != digest)
        return record.mark_scope_changed(state, due.routine_id, now, changed), None
    return routine_claim.claim(state, now, key, long)


def team_provider(self, team_id: str) -> str | None:
    """The Team's configured model provider, or None when it has none; a Team with none is not claimed.

    No key is needed to claim or run a healthy compiled run (ADR-0092); only a held run's recovery uses the Team's
    model, with the key Admin sends when it holds one.
    """
    try:
        return self.inference_store.load(team_id).provider
    except inference_config.InferenceConfigError:
        return None


def _state_unavailable(team_id: str) -> None:
    local_audit.record_request("routine-claim", result="error", team_id=team_id, detail="routine-state-unavailable")


def _readable_states(self, teams: tuple[str, ...]) -> dict[str, record.TeamRoutines]:
    """Each identified Team's Routine state; one that cannot be read is audited and left out, never blocking others."""
    states = {}
    for team_id in teams:
        try:
            states[team_id] = routine_state.load(self, team_id)
        except ApiProblem:
            _state_unavailable(team_id)
    return states


def _claim_team(self, team_id: str, now: int, key: str, long: bool):
    """Lease one due run of the Team, or None; a Team whose state cannot be changed is audited and passed over."""
    # Teardown holds the Team lifecycle lock while it takes the Routine lock; the contract check inside the claim
    # needs the lifecycle lock too, so it is taken first here, in the same order, never inside the Routine lock.
    with self._lock(team_id):
        try:
            return routine_state.update(self, team_id, lambda state: _claim(self, team_id, state, now, key, long))
        except ApiProblem, record.RoutineStateError:
            _state_unavailable(team_id)
            return None


def claim_routine_run(self, long: bool = True) -> dict[str, object] | None:
    """Lease one due run, choosing the least recently served Team first; None when nothing may start now.

    Any Team with a configured model may be claimed, whether or not Admin holds its key: a healthy run needs none.
    Without ``long``, Admin already holds a long run in a worker, so only a short run is claimed (ADR-0092 amendment,
    2026-10-05, scale).
    """
    try:
        key = local_authority.routine_key_fingerprint()
    except local_authority.SupervisorUnavailableError:
        return None
    now = int(time.time())
    states = _readable_states(self, routine_state.call(self.routine_store.teams))
    for team_id in sorted(states, key=lambda team: (states[team].served_at, team)):
        provider = team_provider(self, team_id)
        if provider is None or _chat_busy(self, team_id):
            continue
        if states[team_id].discards:
            try:
                routine_manage.drain(self, team_id)
            except ApiProblem:
                # Nothing more starts for this Team until what its ended runs hold is removed.
                continue
        claim = _claim_team(self, team_id, now, key, long)
        if claim is not None:
            local_audit.record_request("routine-claim", result="ok", team_id=team_id, detail=claim.run.run_id)
            return {
                "team_id": team_id,
                "run_id": claim.run.run_id,
                "routine_id": claim.run.routine_id,
                "lease_token": claim.lease_token,
                "lease_expires_at": claim.run.lease_expires_at,
                "provider": provider,
                "revision": claim.revision,
                "plan_digest": claim.plan_digest,
                "mode": claim.mode,
                "active_seconds": claim.active_seconds,
            }
    return None


def next_routine_due(self) -> int | None:
    """When Admin should next claim: the earliest instant a Routine of a Team it can run becomes due (ADR-0092)."""
    now = int(time.time())
    states = _readable_states(self, routine_state.call(self.routine_store.teams))
    due = [routine_claim.next_due(state, now) for team_id, state in states.items() if team_provider(self, team_id)]
    return min((item for item in due if item is not None), default=None)


def _end(self, team_id: str, run_id: str, outcome: str, detail: dict[str, object]) -> str:
    now = int(time.time())
    routine_state.update(self, team_id, lambda state: (routine_runs.end(state, run_id, now, outcome, detail), None))
    return outcome


def _finish(self, run: _Run, outcome: str, detail: dict[str, object], shown: dict[str, object] | None) -> str:
    """A worker's own ending; when its lease or time ran out meanwhile, Team records the run as failed instead."""
    now = int(time.time())

    def finish(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str]:
        try:
            return routine_runs.finish(state, run.run_id, run.lease, now, outcome, detail, shown), outcome
        except record.RoutineStateError:
            return routine_runs.end(
                state,
                run.run_id,
                now,
                "failed",
                {
                    "code": "lease-expired",
                    "actions": [],
                    "position": None,
                    "steps": None,
                },
            ), "failed"

    return routine_state.update(self, run.team_id, finish)


def finished(
    self, run: _Run, value: record.Run, sealed_done: Callable[[], bool], shown: dict[str, object] | None = None
) -> str:
    """A compiled run completed every step: commit its end exactly when Stop did not win it; no model is asked.

    Its notice names the Actions it carried out, and says recovered when a continuation after a hold completed it. A
    deadline is no person's Stop: when it, not a person, cut a run whose sealed cursor proves every step complete, the
    run is recorded complete like the watchdog would, which also resets its failure streak. ``shown`` is the result the
    run's sealed cursor kept for its Routine's output disposition.
    """
    if not self._commit_chat_terminal(run.team_id, run.token):
        if _deadline_cut(self, run) and sealed_done():
            return complete_sealed(self, run, shown)
        return _end(self, run.team_id, run.run_id, "stopped", {"actions": []})
    return complete(self, run, value, shown)


def complete_sealed(self, run: _Run, shown: dict[str, object] | None) -> str:
    """Record a run its own deadline cut after its sealed cursor completed every step, as complete.

    It uses the Team-authoritative transition the watchdog uses, bound to this run's exact lease but not to time left
    on it, because a deadline exhausts both: the run is done or recovered, names its Actions, and resets the failure
    streak. The caller has proven sealed completion; a run whose lease changed since is never touched.
    """
    now = int(time.time())

    def change(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str | None]:
        current = next((item for item in state.runs if item.run_id == run.run_id), None)
        try:
            completed = routine_runs.complete_recovered(state, run.run_id, run.lease.sha256, now, shown)
        except record.RoutineStateError:
            return state, None
        return completed, routine_runs.completed(current)

    outcome = routine_state.update(self, run.team_id, change)
    if outcome is None:
        raise local_errors.routine_lease_invalid()
    return outcome


def complete(self, run: _Run, value: record.Run, shown: dict[str, object] | None) -> str:
    """Record a run whose every step completed: done, or recovered for a continuation, by its output disposition."""
    return _finish(self, run, routine_runs.completed(value), {}, shown)


def _deadline_cut(self, run: _Run) -> bool:
    """Whether this execution was cancelled by its own deadline, not by a person."""
    with self._active_chat_guard:
        registration = self._routine_runs.get(run.run_id)
        return registration is not None and registration.token == run.token and registration.overdue


def suspended(self, run: _Run, segment, step: dict[str, object]) -> str:
    """A run paused for a person or an Integration at the call ``step``: keep its continuation, freeze it."""
    outcome = segment.outcome
    pending = PendingLocalChat(
        continuation=outcome.continuation,
        assistant_ids=tuple(assistant for assistant, _digest in run.routine.assistants),
        file_ids=(),
        provider=run.provider,
        identity=segment.identity,
        transcripts=chat_orchestrator.retain_suspension_transcripts(run.transcripts, outcome),
        requests_used=run.requests_used,
        paused_batch=segment.paused_batch,
    )
    return _freeze(self, run, pending, segment, step)


def _frozen_request(segment) -> tuple[str, tuple[object, ...], str, str] | None:
    """The one human or Integration requirement a run may freeze on, or None when the pause cannot be answered."""
    if segment.human:
        requirement = segment.human[0]
        request = requirement.request
        if (
            len(segment.human) != 1
            or request != segment.outcome.request
            or request.kind in action_human.AUTH_KINDS - {"auth:password"}
        ):
            return None
        return "human", segment.human, requirement.assistant_id, requirement.action_id
    requirement = segment.integrations[0]
    return "integrations", segment.integrations, requirement.assistant_id, requirement.action_ids[0]


REDACTED = "[redacted]"


def request_fingerprint(request: dict[str, object]) -> str:
    """The SHA-256 of a request's canonical JSON, as a public challenge descriptor names it."""
    canonical = json.dumps(request, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _redacted(value: object, pattern: re.Pattern[str] | None) -> object:
    if pattern is None:
        return value
    if isinstance(value, str):
        return pattern.sub(REDACTED, value)
    if isinstance(value, list):
        return [_redacted(item, pattern) for item in value]
    if isinstance(value, dict):
        return {key: _redacted(item, pattern) for key, item in value.items()}
    return value


def _withheld(request: dict[str, object], protection: trace.Protection) -> set[str]:
    """Withhold every copy parameter holding a protected value, or every text one after a loss; what it hid."""
    references = [request.get(field) for field in action_human.COPY_FIELDS]
    options = request.get("options")
    for option in options if isinstance(options, list) else ():
        references.extend(option.get(field) for field in action_human.OPTION_COPY_FIELDS if isinstance(option, dict))
    hidden: set[str] = set()
    for reference in references:
        params = reference.get("params") if isinstance(reference, dict) else None
        for name, value in tuple(params.items()) if isinstance(params, dict) else ():
            if isinstance(value, str) and (protection.lost or trace.exposes(value, protection.values)):
                hidden.add(value)
                del params[name]
    return hidden


def public_challenge(descriptor: dict[str, object], protection: trace.Protection) -> dict[str, object] | None:
    """A frozen run's human request as the person may see it, or None when nothing shown can hide what it protects.

    Only this public copy changes; the sealed request and its challenge stay exact (ADR-0101 section 6). A copy
    parameter holding a protected value is withheld, as is every text parameter once the run lost its protection, and
    the rendered copy shows ``[redacted]`` in its place; the request's fingerprint is that of what is shown. A value
    still shown anywhere else, such as an option's value, or copy the marker pushes past its bound, cannot be shown;
    nor, after a loss, can any option value or purpose, which no lost protection could check.
    """
    request = copy.deepcopy(descriptor["request"])
    request.pop("fingerprint", None)
    if protection.lost and ("options" in request or "purpose" in descriptor):
        # The run no longer knows what it protects: only reviewed catalog copy and Team's own fields may be shown.
        return None
    hidden = _withheld(request, protection) | protection.values
    ordered = sorted(hidden, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(item) for item in ordered)) if ordered else None
    shown = {**descriptor, "request": request, "rendered": _redacted(descriptor["rendered"], pattern)}
    if trace.exposes(shown, frozenset(hidden)) or http_payload.canonical_rendered(shown["rendered"], request) is None:
        return None
    return {**shown, "request": {**request, "fingerprint": request_fingerprint(request)}}


def _unshowable(self, run_id: str, segment) -> bool:
    """Whether a paused run's human request has no public copy that hides every value its run protects."""
    if not segment.human:
        return False
    protection = self.routine_protections.grow(run_id, ())
    return any(
        public_challenge({"request": item.request.payload(), "rendered": dict(item.copy.rendered)}, protection) is None
        for item in segment.human
    )


def _freeze(self, run: _Run, pending: PendingLocalChat, segment, step: dict[str, object]) -> str:
    """Keep a paused run for a human: its continuation first, then the frozen record commits the freeze."""
    team_id, run_id = run.team_id, run.run_id
    frozen = _frozen_request(segment)
    # As in chat, a secret answer must be the last one a logical run gives: none may follow it. A request the person
    # would be shown is checked against the run's protection now, while it is known: one with no public copy hiding
    # every protected value is never shown (ADR-0101 section 6).
    if (
        frozen is None
        or any(response.secret for transcript in pending.transcripts for response in transcript.responses)
        or _unshowable(self, run_id, segment)
    ):
        self._commit_chat_terminal(team_id, run.token)
        placed = {"position": step, "steps": len(run.routine.plan["steps"])}
        return _end(self, team_id, run_id, "failed", {"code": "request-unavailable", "actions": [], **placed})
    kind, requirements, assistant_id, action = frozen
    bindings, payload = local_chat_continuations.encode(
        kind, requirements, pending, limit=local_chat_continuations.MAX_ROUTINE_PLAINTEXT_BYTES
    )
    blob = json.dumps(
        {"kind": kind, "bindings": list(bindings), "payload": base64.b64encode(payload).decode("ascii")},
        separators=(",", ":"),
    ).encode("ascii")
    routine_state.call(lambda: self.routine_store.put_continuation(team_id, run_id, blob))
    # From here, the run's end queues its continuation's removal with everything else it held.
    now = int(time.time())

    def freeze(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str]:
        try:
            return routine_runs.freeze(state, run_id, run.lease, now, (kind, assistant_id, action, step)), "frozen"
        except record.RoutineStateError as exc:
            if str(exc) == "routine-deleting":
                # The deletion stops every run it saw leased; one reaching its pause meanwhile ends stopped.
                return routine_runs.end(state, run_id, now, "stopped", {"actions": []}), "stopped"
            placed = {"position": step, "steps": len(run.routine.plan["steps"])}
            return routine_runs.end(
                state, run_id, now, "failed", {"code": "freeze-unavailable", "actions": [], **placed}
            ), ("failed")

    outcome: list[str] = []
    # The freeze is written under the guard a Stop cancels under: a Stop either wins first and the run ends stopped,
    # or finds the run already frozen once its cancellation returns, and ends it as a frozen run.
    if not self._commit_chat_terminal(
        team_id, run.token, lambda: outcome.append(routine_state.update(self, team_id, freeze))
    ):
        return _end(self, team_id, run_id, "stopped", {"actions": []})
    return outcome[0]


def _live_run(self, team_id: str, run_id: str, lease: record.Lease) -> tuple[record.Run, record.Routine]:
    state = routine_state.load(self, team_id)
    try:
        value = record.run(state, run_id)
        routine_claim.require_lease(value, lease, int(time.time()))
        return value, record.routine(state, value.routine_id)
    except record.RoutineStateError as exc:
        raise local_errors.routine_lease_invalid() from exc


@contextmanager
def registered(self, team_id: str, run_id: str, token: str, active_seconds: int):
    """Make the run reachable by Stop from the moment its worker holds the slot until it recorded how it ended.

    Registration comes before the worker resumes or binds the run, so a Stop either finds it registered or has already
    fenced it out, and no Action runs in between.
    """
    register_routine_run(self, team_id, run_id, token, active_seconds)
    try:
        yield
    finally:
        unregister_routine_run(self, run_id)


def _bind(self, team_id: str, run_id: str, lease: record.Lease) -> str:
    """Bind the run's own journal generation under its live lease."""
    network_id = self.assistant_lifecycle._network(team_id).id
    now = int(time.time())

    def bind(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str | None]:
        try:
            bound = routine_claim.bind_generation(state, run_id, lease, now, network_id)
        except record.RoutineStateError:
            return state, None
        return bound, record.run(bound, run_id).generation

    generation = routine_state.update(self, team_id, bind)
    if generation is None:
        raise local_errors.routine_lease_invalid()
    return generation


def _context_refusal(self, team_id: str, pinned: dict[str, str]) -> str | None:
    """Why a leased run may not start under its pinned contracts, or None when they are exactly current."""
    try:
        current = routine_contracts.current_contracts(self, team_id, tuple(pinned))
    except routine_contracts.ContractsUnavailableError:
        return "team-context-unavailable"
    return None if current == pinned else "team-context-changed"


def _spend(self, team_id: str, run_id: str, lease: record.Lease, elapsed: float) -> None:
    """Charge one segment's active time: whole seconds to its budget, milliseconds to its usage."""
    now = int(time.time())

    def spend(state: record.TeamRoutines) -> tuple[record.TeamRoutines, None]:
        try:
            return routine_runs.spend(state, run_id, lease, now, (int(elapsed), int(elapsed * 1000))), None
        except record.RoutineStateError:
            return state, None

    routine_state.update(self, team_id, spend)


def _after_run(self, team_id: str, run_id: str, routine_id: str, outcome: str) -> None:
    """Remove what an ended run held and finish a deletion the run was blocking; audit how it ended."""
    local_audit.record_request(
        "routine-run", result="ok" if outcome in {"done", "recovered"} else "error", team_id=team_id, detail=outcome
    )
    routine_manage.settle(self, team_id, routine_id)


def register_routine_run(self, team_id: str, run_id: str, token: str, active_seconds: int) -> None:
    """Register a run's worker; a Stop that already found the run unregistered fences it out instead.

    The same execution registering again, as a recovery's continuation does, keeps a deadline that already cancelled
    it, so its ending is still recorded as out of time, never as stopped.
    """
    with self._active_chat_guard:
        if run_id in self._routine_halting:
            raise ApiProblem(HTTPStatus.CONFLICT, "Routine run was stopped", code="chat-stopped")
        previous = self._routine_runs.get(run_id)
        overdue = previous is not None and previous.token == token and previous.overdue
        deadline = time.monotonic() + max(active_seconds, 0)
        self._routine_runs[run_id] = _Registration(team_id, token, deadline, overdue)


def unregister_routine_run(self, run_id: str) -> None:
    with self._active_chat_guard:
        self._routine_runs.pop(run_id, None)


def halt_routine_run(self, team_id: str, run_id: str) -> bool:
    """Stop a leased run whether or not its worker holds the slot: stop a running segment, or end a run not started.

    Finding no worker and fencing one out is a single step under the guard, so a worker that registers meanwhile is
    refused; one that comes later finds the run ended and cannot bind it.
    """
    with self._active_chat_guard:
        running = run_id in self._routine_runs
        if not running:
            self._routine_halting.add(run_id)
    if running:
        return stop_routine_run(self, team_id, run_id)
    now = int(time.time())

    def end(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return routine_runs.end(state, run_id, now, "stopped", {"actions": []}, status="leased"), True
        except record.RoutineStateError:
            return state, False

    try:
        return routine_state.update(self, team_id, end)
    finally:
        with self._active_chat_guard:
            self._routine_halting.discard(run_id)


def stop_routine_run(self, team_id: str, run_id: str) -> bool:
    """Stop exactly one running Routine run: cancel its turn, abort its Brain request, fail-stop its Action."""
    return _stop_registered(self, team_id, run_id, None)


def expire_routine_run(self, team_id: str, run_id: str, token: str, mark: Callable[[], None]) -> bool:
    """A deadline's Stop of exactly the execution it was set for, identified by its token.

    Checking the registration and cancelling it is one step under the guard, so a late deadline never reaches another
    execution registered for the same run since, nor one that already ended. One a person already stopped stays a
    person's Stop: the deadline then does nothing, and ``mark`` records the deadline as the cause only when it is.
    """
    return _stop_registered(self, team_id, run_id, token, mark)


def unstopped(self, token: str, deadline: Callable[[], bool], commit: Callable[[], None]) -> bool:
    """Commit an outcome for this execution only if no person stopped it first; whether it was committed.

    The decision and ``commit`` both run under the same guard a person's Stop cancels under, as a chat reply's commit
    does, so a Stop is either before the decision, and nothing is committed, or after the commit. A cancellation the
    execution's own ``deadline`` caused is no person's Stop.
    """
    with self._active_chat_guard:
        if token in self._cancelled_chat_tokens and not deadline():
            return False
        commit()
        return True


def _stop_registered(
    self, team_id: str, run_id: str, expected: str | None, mark: Callable[[], None] | None = None
) -> bool:
    with self._active_chat_guard:
        running = self._routine_runs.get(run_id)
        if running is None or running.team_id != team_id or expected not in (None, running.token):
            return False
        if expected is not None:
            if expected in self._cancelled_chat_tokens:
                return False
            mark()
            # A deadline, not a person: the run's ending records it as out of time, never as stopped.
            self._routine_runs[run_id] = dataclasses.replace(running, overdue=True)
        token = running.token
        self._cancelled_chat_tokens.add(token)
        brain_abort = self._brain_aborts.get(token)
        active = self._active_action_containers.get(team_id)
        active_action = active[1] if active is not None and active[0] == token else None
    if brain_abort is not None:
        brain_abort.abort()
    if active_action is not None:
        self.assistant_lifecycle._fail_stop_action(active_action)
    return True


def stop_overdue(self) -> tuple[tuple[str, str], ...]:
    """Stop every running segment whose run spent its active time; each then ends failed, not stopped.

    One run whose stop fails never keeps the others running: its Team and run are returned for the caller to audit,
    and the next pass tries it again.
    """
    now = time.monotonic()
    with self._active_chat_guard:
        overdue = [(run_id, item) for run_id, item in self._routine_runs.items() if item.deadline <= now]
        for run_id, item in overdue:
            self._routine_runs[run_id] = dataclasses.replace(item, overdue=True)
    failed = []
    for run_id, item in overdue:
        try:
            stop_routine_run(self, item.team_id, run_id)
        except ApiProblem:
            failed.append((item.team_id, run_id))
    return tuple(failed)
