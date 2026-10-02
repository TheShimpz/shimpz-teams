"""A Local Supervisor previews, confirms, lists, and deletes a Team's Routines (ADR-0086)."""

from __future__ import annotations

import dataclasses
import datetime
import time
from collections.abc import Callable
from http import HTTPStatus

from action import journal as action_journal
from inference import client as brain_runtime_client
from local.errors import ApiProblemError as ApiProblem
from local.routine import proposal as proposal_book
from local.routine import state as routine_state
from local.routine import turn as routine_turn
from local.validation import routine_thread_id, validate_team_id
from protocol.http.v1 import routine as http_routine
from routine import record, schedule

PREVIEW_RUNS = 3


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _instant(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).isoformat().replace("+00:00", "Z")


def routine_view(value: record.Routine) -> dict[str, object]:
    return {
        "routine_id": value.routine_id,
        "quote": value.quote,
        "schedule": dict(value.schedule),
        "timezone": value.timezone,
        "assistant_ids": [assistant for assistant, _digest in value.assistants],
        "next_run_at": _instant(value.next_run_at),
        "needs_reconfirm": value.needs_reconfirm,
        "deleting": value.deleting,
    }


def run_view(value: record.Run) -> dict[str, object]:
    """What a Supervisor may act on: a frozen run's request, or an uncertain run's exact batch to resolve."""
    return {
        "run_id": value.run_id,
        "routine_id": value.routine_id,
        "status": value.status,
        "scheduled_at": _instant(value.scheduled_at),
        "request_kind": value.request_kind or None,
        "assistant_id": value.assistant_id or None,
        "action": value.action or None,
        "batch_fingerprint": value.batch[1] or None,
        "actions": [list(pair) for pair in value.held_actions],
    }


def _timezone(proposal: proposal_book.Proposal, body_timezone: object) -> str:
    """The timezone the user named in the request, or else the one the browser reports; it must load."""
    name = proposal.change["timezone"] or body_timezone
    try:
        schedule.zone(name)
    except schedule.ScheduleError as exc:
        raise _problem(HTTPStatus.UNPROCESSABLE_ENTITY, "timezone is invalid", "invalid-timezone") from exc
    return name


def _proposal(self, team_id: str, proposal_id: object, *, take: bool) -> proposal_book.Proposal:
    try:
        return (self.routine_proposals.take if take else self.routine_proposals.peek)(team_id, proposal_id)
    except proposal_book.ProposalError as exc:
        raise _problem(HTTPStatus.NOT_FOUND, "Routine proposal is unavailable", "routine-proposal-unavailable") from exc


def _candidate(proposal: proposal_book.Proposal, timezone: str, now: int) -> record.Routine:
    value = record.Routine(
        routine_id=record.new_id(),
        quote=proposal.change["quote"],
        schedule=dict(proposal.change["schedule"]),
        timezone=timezone,
        assistants=proposal.contracts,
        anchor=now,
        next_run_at=0,
    )
    return dataclasses.replace(value, next_run_at=record.next_after(value, now))


def _require_proposed_contracts(self, team_id: str, proposal: proposal_book.Proposal) -> None:
    """Unattended runs must use exactly the Assistant contracts the user saw proposed; an unread Team is retryable."""
    try:
        current = routine_turn.current_contracts(self, team_id, proposal.assistant_ids)
    except routine_turn.ContractsUnavailableError as exc:
        raise routine_turn.context_unavailable() from exc
    if current != dict(proposal.contracts):
        raise _problem(HTTPStatus.CONFLICT, "Team capabilities changed; ask again", "team-context-changed")


def _body(body: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(body, dict) or set(body) != fields:
        raise _problem(HTTPStatus.UNPROCESSABLE_ENTITY, "Routine request body is invalid", "invalid-body")
    return body


def preview_routine(self, team_id: str, proposal_id: str, body: object) -> dict[str, object]:
    """The confirmation card's facts: the timezone, the next firings, and the Team's daily run budget."""
    team_id = validate_team_id(team_id)
    timezone = _body(body, {"timezone"})["timezone"]
    proposal = _proposal(self, team_id, proposal_id, take=False)
    view = proposal.view(time.time())
    if proposal.change["op"] != "propose":
        return {**view, "timezone": None, "next_runs": [], "daily_runs": None, "max_daily_runs": None, "fits": True}
    now = int(time.time())
    candidate = _candidate(proposal, _timezone(proposal, timezone), now)
    routines = routine_state.load(self, team_id).routines
    runs = schedule.upcoming(
        candidate.schedule, candidate.timezone, record._instant(now), record._instant(now), PREVIEW_RUNS
    )
    total = sum(
        (http_routine.daily_rate(item.schedule) for item in routines), http_routine.daily_rate(candidate.schedule)
    )
    return {
        **view,
        "timezone": candidate.timezone,
        "next_runs": [run.isoformat().replace("+00:00", "Z") for run in runs],
        "daily_runs": str(total),
        "max_daily_runs": http_routine.MAX_DAILY_RUNS,
        "fits": len(routines) < record.MAX_ROUTINES and total <= http_routine.MAX_DAILY_RUNS,
    }


def _add(state: record.TeamRoutines, candidate: record.Routine) -> tuple[record.TeamRoutines, str]:
    try:
        return record.add_routine(state, candidate), ""
    except record.RoutineStateError as exc:
        return state, str(exc)


def confirm_routine(self, team_id: str, body: object) -> dict[str, object]:
    """A Local Supervisor's confirmation: the only way a Routine is created or a cancellation is carried out."""
    team_id = validate_team_id(team_id)
    body = _body(body, {"proposal_id", "timezone"})
    proposal = _proposal(self, team_id, body["proposal_id"], take=False)
    if proposal.change["op"] == "cancel":
        # Spent inside the deletion's own write, only once the Routine is found and no uncertain run refuses it.
        return delete_routine(
            self,
            team_id,
            proposal.change["routine_id"],
            admit=lambda: _proposal(self, team_id, proposal.proposal_id, take=True),
        )
    # Every recoverable check only peeks, so a wrong timezone or a Team that cannot be read now leaves the proposal
    # for a retry.
    timezone = _timezone(proposal, body["timezone"])
    # Held from the contract check to the write, before the Routine lock as teardown takes them, so a Team removed
    # meanwhile can never have its Routine state recreated.
    with self._lock(team_id):
        _require_proposed_contracts(self, team_id, proposal)
        # Spent only now, before the write: of two concurrent confirmations, the one that takes it first creates it.
        _proposal(self, team_id, proposal.proposal_id, take=True)
        candidate = _candidate(proposal, timezone, int(time.time()))
        refused = routine_state.update(self, team_id, lambda state: _add(state, candidate))
    if refused:
        raise _problem(HTTPStatus.CONFLICT, "the Team cannot hold this Routine", refused)
    return {"team_id": team_id, "routine": routine_view(candidate)}


def list_routines(self, team_id: str) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    state = routine_state.load(self, team_id)
    return {
        "team_id": team_id,
        "routines": [routine_view(item) for item in state.routines],
        "runs": [run_view(item) for item in state.runs],
    }


def _discard(self, team_id: str, run_id: str, generation: str) -> None:
    """Remove one ended run's Brain thread, live journal batch, and continuation; each removal is idempotent.

    An archive marker its unresolved evidence still needs stays in the journal (ADR-0092).
    """
    if generation:
        network_id = generation.removesuffix(f":routine:{run_id}")
        try:
            self.brain_runtime.delete_thread(routine_thread_id(self.space_id, team_id, network_id, run_id))
            self.action_state.discard(generation)
        except (brain_runtime_client.BrainRuntimeError, action_journal.ActionJournalError) as exc:
            raise _problem(
                HTTPStatus.SERVICE_UNAVAILABLE, "Routine run state could not be removed", "routine-state-unavailable"
            ) from exc
    routine_state.call(lambda: self.routine_store.delete_continuation(team_id, run_id))


def drain(self, team_id: str) -> None:
    """Remove what the Team's ended runs still hold; a failure keeps the rest queued for the next drain.

    A run's end queues its removal in the same write that ends it, so nothing it held is lost to a crash, and nothing
    is removed while the run could still resume.
    """
    for run_id, generation in routine_state.load(self, team_id).discards:
        _discard(self, team_id, run_id, generation)
        routine_state.update(self, team_id, lambda state, run=run_id: (record.discarded(state, run), None))


def settle(self, team_id: str, routine_id: str) -> bool:
    """After a run ended: remove what it held, then finish a deletion it was blocking."""
    drain(self, team_id)
    return complete_deletion(self, team_id, routine_id)


def settle_team(self, team_id: str) -> None:
    """Retry every pending removal and deletion of a Team, as the watchdog does after a failure or a restart."""
    drain(self, team_id)
    for item in routine_state.load(self, team_id).routines:
        if item.deleting:
            complete_deletion(self, team_id, item.routine_id)


def end_frozen(self, team_id: str, run_id: str, outcome: str, detail: dict[str, object]) -> bool:
    """End a run only while it is still frozen; False when a replay or another ending reached it first."""
    now = int(time.time())

    def end(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return record.end(state, run_id, now, outcome, detail, status="frozen"), True
        except record.RoutineStateError:
            return state, False

    return routine_state.update(self, team_id, end)


def delete_routine(
    self, team_id: str, routine_id: object, *, admit: Callable[[], object] | None = None
) -> dict[str, object]:
    """Delete a Routine: stop its running run and end a frozen one; an uncertain run must be resolved first.

    Marking the Routine deleting and reading its runs is one write, and a deleting Routine never resumes a run, so each
    frozen run seen here stays frozen until it is ended. A running segment is stopped and ends itself, and its end
    completes the deletion. ``admit`` runs inside that write once the deletion is valid; if it raises, nothing changes.
    """
    team_id = validate_team_id(team_id)
    if not isinstance(routine_id, str) or http_routine.ROUTINE_ID_RE.fullmatch(routine_id) is None:
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found")

    def begin(state: record.TeamRoutines) -> tuple[record.TeamRoutines, tuple[record.Run, ...] | str]:
        try:
            deleting = record.begin_delete(state, routine_id)
        except record.RoutineStateError as exc:
            return state, str(exc)
        if admit is not None:
            admit()
        return deleting

    runs = routine_state.update(self, team_id, begin)
    if runs == "routine-run-uncertain":
        raise _problem(HTTPStatus.CONFLICT, "Resolve the Routine's uncertain run first", "routine-run-uncertain")
    if isinstance(runs, str):
        raise _problem(HTTPStatus.NOT_FOUND, "Routine is unavailable", "routine-not-found")
    for value in runs:
        if value.status == "leased":
            self._stop_routine_run(team_id, value.run_id)
            continue
        self._cancel_routine_challenge(team_id, value.run_id)
        end_frozen(self, team_id, value.run_id, "stopped", {"actions": []})
    return {"team_id": team_id, "routine_id": routine_id, "deleted": settle(self, team_id, routine_id)}


def complete_deletion(self, team_id: str, routine_id: str) -> bool:
    """Remove a deleting Routine once none of its runs remains; False while a run still ends."""

    def complete(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        value = (
            record.routine(state, routine_id) if any(item.routine_id == routine_id for item in state.routines) else None
        )
        if value is None or not value.deleting:
            return state, value is None
        try:
            return record.complete_delete(state, routine_id), True
        except record.RoutineStateError:
            return state, False

    return routine_state.update(self, team_id, complete)
