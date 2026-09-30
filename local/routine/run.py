"""Local Routine runs: a fair claim across Teams, then one isolated run segment under its lease (ADR-0086)."""

from __future__ import annotations

import base64
import dataclasses
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus

from action import human as action_human
from chat import knowledge as chat_knowledge
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from inference import config as inference_config
from local import audit as local_audit
from local import authority as local_authority
from local.chat import continuation as local_chat_continuations
from local.chat.segment import RoutineSegment, SegmentRequest
from local.chat.types import PendingLocalChat
from local.errors import ApiProblemError as ApiProblem
from local.routine import manage as routine_manage
from local.routine import state as routine_state
from local.routine import turn as routine_turn
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from routine import record


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


@dataclass(frozen=True, slots=True)
class _Registration:
    """A segment in the execution slot: what Stop needs to reach it, and when its active time runs out."""

    team_id: str
    token: str
    deadline: float
    overdue: bool = False


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _chat_busy(self, team_id: str) -> bool:
    """Chat has priority at admission: a Routine never starts beside a chat turn or a pending chat challenge."""
    return (
        self._chat_lock(team_id).locked()
        or self.human_challenges.current(team_id) is not None
        or self.integration_challenges.current(team_id) is not None
    )


def _claim(self, team_id: str, state: record.TeamRoutines, now: int, key: str):
    """Sweep, check the oldest due Routine's pinned contracts, then lease one run of it."""
    state = record.sweep(state, now)
    due = record.claimable(state, now)
    if due is None:
        return state, None
    pinned = dict(due.assistants)
    current = routine_turn.current_contracts(self, team_id, tuple(pinned)) or {}
    if current != pinned:
        changed = sorted(assistant for assistant, digest in pinned.items() if current.get(assistant) != digest)
        return record.mark_scope_changed(state, due.routine_id, now, changed), None
    return record.claim(state, now, key)


def _provider(self, team_id: str, providers: tuple[str, ...]) -> str | None:
    """The Team's configured model provider when Admin holds its key; otherwise the Team is not claimed."""
    try:
        provider = self.inference_store.load(team_id).provider
    except inference_config.InferenceConfigError:
        return None
    return provider if provider in providers else None


def claim_routine_run(self, providers: tuple[str, ...]) -> dict[str, object] | None:
    """Lease one due run, choosing the least recently served Team first; None when nothing may start now.

    Only a Team whose model provider is among ``providers``, the ones Admin holds a key for, is claimed, so no lease
    is taken for a run that could not reach its model.
    """
    try:
        key = local_authority.routine_key_fingerprint()
    except local_authority.SupervisorUnavailableError:
        return None
    now = int(time.time())
    teams = routine_state.call(self.routine_store.teams)
    states = {team_id: routine_state.load(self, team_id) for team_id in teams}
    for team_id in sorted(teams, key=lambda team: (states[team].served_at, team)):
        provider = _provider(self, team_id, providers)
        if provider is None or _chat_busy(self, team_id):
            continue
        if states[team_id].discards:
            try:
                routine_manage.drain(self, team_id)
            except ApiProblem:
                # Nothing more starts for this Team until what its ended runs hold is removed.
                continue
        # Teardown holds the Team lifecycle lock while it takes the Routine lock; the contract check inside the claim
        # needs the lifecycle lock too, so it is taken first here, in the same order, never inside the Routine lock.
        with self._lock(team_id):
            claim = routine_state.update(self, team_id, lambda state, team=team_id: _claim(self, team, state, now, key))
        if claim is not None:
            local_audit.record_request("routine-claim", result="ok", team_id=team_id, detail=claim.run.run_id)
            return {
                "team_id": team_id,
                "run_id": claim.run.run_id,
                "routine_id": claim.run.routine_id,
                "lease_token": claim.lease_token,
                "lease_expires_at": claim.run.lease_expires_at,
                "provider": provider,
            }
    return None


def _names(actions: tuple[object, ...]) -> list[list[str]]:
    """Bounded Assistant and Action identities for a notice; never an Action's input or result."""
    names: list[list[str]] = []
    for action in actions:
        pair = [action.assistant_id, action.action]
        if pair not in names:
            names.append(pair)
    return names[: http_routine.MAX_NOTICE_ACTIONS]


def _end(self, team_id: str, run_id: str, outcome: str, detail: dict[str, object], fingerprint: str = "") -> str:
    now = int(time.time())
    routine_state.update(
        self, team_id, lambda state: (record.end(state, run_id, now, outcome, detail, fingerprint), None)
    )
    return outcome


def _finish(self, run: _Run, outcome: str, detail: dict[str, object]) -> str:
    """A worker's own ending; when its lease or time ran out meanwhile, Team records the run as failed instead."""
    now = int(time.time())

    def finish(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str]:
        try:
            return record.finish(state, run.run_id, run.lease, now, outcome, detail), outcome
        except record.RoutineStateError:
            return record.end(state, run.run_id, now, "failed", {"code": "lease-expired", "actions": []}), "failed"

    return routine_state.update(self, run.team_id, finish)


def _save_skill(self, team_id: str, terminal: chat_orchestrator.ChatOutcome) -> None:
    """A completed run teaches its procedure like a chat turn (ADR-0085); it never changes memory."""
    skill = chat_knowledge.learned_skill(terminal.actions)
    if skill is None:
        return
    try:
        self.inference_store.apply_knowledge(team_id, [], skill)
    except inference_config.InferenceConfigError as exc:
        raise _problem(HTTPStatus.SERVICE_UNAVAILABLE, "Team memory could not be saved", "memory-store-failed") from exc


def _complete(self, run: _Run, terminal: chat_orchestrator.ChatOutcome) -> str:
    if not self._commit_chat_terminal(run.team_id, run.token, lambda: _save_skill(self, run.team_id, terminal)):
        return _end(self, run.team_id, run.run_id, "stopped", {"actions": _names(terminal.actions)})
    if terminal.clarification is not None:
        return _finish(self, run, "needs-input", {"question": terminal.clarification["question"]})
    reply = terminal.reply.strip()[: http_routine.MAX_NOTICE_REPLY_CHARS].strip() or "Done."
    return _finish(self, run, "done", {"reply": reply})


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


def _freeze(self, run: _Run, pending: PendingLocalChat, segment) -> str:
    """Keep a paused run for a human: its continuation first, then the frozen record commits the freeze."""
    team_id, run_id = run.team_id, run.run_id
    frozen = _frozen_request(segment)
    # As in chat, a secret answer must be the last one a logical run gives: none may follow it.
    if frozen is None or any(
        response.secret for transcript in pending.transcripts for response in transcript.responses
    ):
        self._commit_chat_terminal(team_id, run.token)
        return _end(self, team_id, run_id, "failed", {"code": "request-unavailable", "actions": []})
    kind, requirements, assistant_id, action = frozen
    bindings, payload = local_chat_continuations.encode(kind, requirements, pending)
    blob = json.dumps(
        {"kind": kind, "bindings": list(bindings), "payload": base64.b64encode(payload).decode("ascii")},
        separators=(",", ":"),
    ).encode("ascii")
    routine_state.call(lambda: self.routine_store.put_continuation(team_id, run_id, blob))
    # From here, the run's end queues its continuation's removal with everything else it held.
    if not self._commit_chat_terminal(team_id, run.token):
        return _end(self, team_id, run_id, "stopped", {"actions": []})
    now = int(time.time())

    def freeze(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str]:
        try:
            return record.freeze(state, run_id, run.lease, now, kind, assistant_id, action), "frozen"
        except record.RoutineStateError:
            return record.end(state, run_id, now, "failed", {"code": "freeze-unavailable", "actions": []}), "failed"

    return routine_state.update(self, team_id, freeze)


def _failed(self, team_id: str, run_id: str, routine_segment: RoutineSegment, exc: ApiProblem) -> str:
    """A segment that failed: an uncertain batch is held for a human; otherwise it stopped or failed."""
    held = routine_segment.batches[-1] if routine_segment.batches else None
    if held is not None and held.held:
        return _end(
            self, team_id, run_id, "uncertain", {"actions": [list(pair) for pair in held.held_actions]}, held.held
        )
    code = exc.code
    if code == "chat-stopped":
        with self._active_chat_guard:
            registration = self._routine_runs.get(run_id)
        if registration is None or not registration.overdue:
            return _end(self, team_id, run_id, "stopped", {"actions": []})
        code = "active-time-exceeded"
    return _end(self, team_id, run_id, "failed", {"code": code, "actions": []})


def _live_run(self, team_id: str, run_id: str, lease: record.Lease) -> tuple[record.Run, record.Routine]:
    state = routine_state.load(self, team_id)
    try:
        value = record.run(state, run_id)
        record.require_lease(value, lease, int(time.time()))
        return value, record.routine(state, value.routine_id)
    except record.RoutineStateError as exc:
        raise _problem(HTTPStatus.CONFLICT, "Routine run lease is not live", "routine-lease-invalid") from exc


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


def run_segment(self, run: _Run, request: SegmentRequest) -> str:
    """Run one registered segment in the held execution slot and record exactly how it ended."""
    started = time.monotonic()
    try:
        segment = self._run_chat_segment(request)
    except ApiProblem as exc:
        return _failed(self, run.team_id, run.run_id, request.routine, exc)
    _spend(self, run.team_id, run.run_id, run.lease, int(time.monotonic() - started))
    return _settle(self, run, segment)


def _bind(self, team_id: str, run_id: str, lease: record.Lease) -> str:
    """Bind the run's own journal generation under its live lease."""
    network_id = self.assistant_lifecycle._network(team_id).id
    now = int(time.time())

    def bind(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return record.bind_generation(state, run_id, lease, now, network_id), True
        except record.RoutineStateError:
            return state, False

    if not routine_state.update(self, team_id, bind):
        raise _problem(HTTPStatus.CONFLICT, "Routine run lease is not live", "routine-lease-invalid")
    return record.generation_for(network_id, run_id)


def run_routine(
    self,
    team_id: str,
    run_id: str,
    evidence: local_authority.RoutineEvidence,
    provider: str,
    api_key: str,
    progress: chat_progress.Reporter | None = None,
) -> dict[str, object]:
    """Run one segment of a leased run in the Team's execution slot and record exactly how it ended."""
    team_id = validate_team_id(team_id)
    lease = record.Lease(evidence.lease_sha256, evidence.key_fingerprint)
    value, routine = _live_run(self, team_id, run_id, lease)
    pinned = dict(routine.assistants)
    with (
        self._exclusive_chat_turn(team_id, routine.routine_id) as token,
        registered(self, team_id, run_id, token, value.active_seconds_left),
    ):
        # Rechecked in the slot: an Assistant changed since the claim never runs under a contract nobody confirmed.
        if routine_turn.current_contracts(self, team_id, tuple(pinned)) != pinned:
            outcome = _end(self, team_id, run_id, "failed", {"code": "team-context-changed", "actions": []})
        else:
            generation = _bind(self, team_id, run_id, lease)
            request = SegmentRequest(
                team_id=team_id,
                file_ids=[],
                assistant_ids=tuple(pinned),
                provider=provider,
                api_key=api_key,
                token=token,
                message=routine.quote,
                routine=RoutineSegment(run_id, generation),
                progress=progress or chat_progress.Reporter(),
            )
            run = _Run(team_id, run_id, lease, token, provider, routine)
            outcome = run_segment(self, run, request)
    _after_run(self, team_id, run_id, routine.routine_id, outcome)
    return {"team_id": team_id, "run_id": run_id, "status": outcome}


def _spend(self, team_id: str, run_id: str, lease: record.Lease, seconds: int) -> None:
    now = int(time.time())

    def spend(state: record.TeamRoutines) -> tuple[record.TeamRoutines, None]:
        try:
            return record.spend(state, run_id, lease, now, seconds), None
        except record.RoutineStateError:
            return state, None

    routine_state.update(self, team_id, spend)


def _settle(self, run: _Run, segment) -> str:
    outcome = segment.outcome
    if isinstance(outcome, chat_orchestrator.ChatOutcome):
        return _complete(self, run, outcome)
    pending = PendingLocalChat(
        continuation=outcome.continuation,
        assistant_ids=tuple(assistant for assistant, _digest in run.routine.assistants),
        file_ids=(),
        provider=run.provider,
        identity=segment.identity,
        transcripts=chat_orchestrator.retain_suspension_transcripts(run.transcripts, outcome),
        requests_used=run.requests_used,
    )
    return _freeze(self, run, pending, segment)


def _after_run(self, team_id: str, run_id: str, routine_id: str, outcome: str) -> None:
    """Remove what an ended run held and finish a deletion the run was blocking; audit how it ended."""
    local_audit.record_request(
        "routine-run", result="ok" if outcome == "done" else "error", team_id=team_id, detail=outcome
    )
    routine_manage.settle(self, team_id, routine_id)


def register_routine_run(self, team_id: str, run_id: str, token: str, active_seconds: int) -> None:
    """Register a run's worker; a Stop that already found the run unregistered fences it out instead."""
    with self._active_chat_guard:
        if run_id in self._routine_halting:
            raise _problem(HTTPStatus.CONFLICT, "Routine run was stopped", "chat-stopped")
        self._routine_runs[run_id] = _Registration(team_id, token, time.monotonic() + max(active_seconds, 0))


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
            return record.end(state, run_id, now, "stopped", {"actions": []}, status="leased"), True
        except record.RoutineStateError:
            return state, False

    try:
        return routine_state.update(self, team_id, end)
    finally:
        with self._active_chat_guard:
            self._routine_halting.discard(run_id)


def stop_routine_run(self, team_id: str, run_id: str) -> bool:
    """Stop exactly one running Routine run: cancel its turn, abort its Brain request, fail-stop its Action."""
    with self._active_chat_guard:
        running = self._routine_runs.get(run_id)
        if running is None or running.team_id != team_id:
            return False
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


def stop_overdue(self) -> None:
    """Stop every running segment whose run spent its active time; each then ends failed, not stopped."""
    now = time.monotonic()
    with self._active_chat_guard:
        overdue = [(run_id, item) for run_id, item in self._routine_runs.items() if item.deadline <= now]
        for run_id, item in overdue:
            self._routine_runs[run_id] = dataclasses.replace(item, overdue=True)
    for run_id, item in overdue:
        stop_routine_run(self, item.team_id, run_id)
