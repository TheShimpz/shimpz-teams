"""A claimed run's transitions, without I/O (ADR-0086, ADR-0092, ADR-0101): spending, freezing, thawing, and ending."""

import copy
import dataclasses
import hashlib

from protocol.http.v1 import identifiers as http_identifiers
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import routine_notice as http_routine_notice
from routine import claim as routine_claim
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record


def spend(
    state: record.TeamRoutines, run_id: str, lease: record.Lease, now: int, elapsed: tuple[int, int]
) -> record.TeamRoutines:
    """Charge one segment's active time to the run: whole seconds against its budget, milliseconds to its usage."""
    seconds, milliseconds = elapsed
    if type(seconds) is not int or seconds < 0 or type(milliseconds) is not int or milliseconds < 0:
        raise record.RoutineStateError("invalid-duration")
    value = routine_claim.live_lease(state, run_id, lease, now)
    usage = {**value.usage, "duration_ms": min(value.usage["duration_ms"] + milliseconds, MAX_USAGE_MS)}
    return record.replace_run(
        state, dataclasses.replace(value, active_seconds_left=value.active_seconds_left - seconds, usage=usage)
    )


# A run's summed active duration, the chat usage bound (ADR-0082).
MAX_USAGE_MS = http_payload.MAX_TURN_DURATION_MS


def joined_usage(first: dict[str, object], second: dict[str, object]) -> dict[str, object]:
    """Two usages summed: their active durations, within the bound."""
    return {"duration_ms": min(first["duration_ms"] + second["duration_ms"], MAX_USAGE_MS)}


def lose_protection(state: record.TeamRoutines, run_id: str) -> record.TeamRoutines:
    """The run lost the protection of its secret values; every later version of its notice says so, for good."""
    value = record.run(state, run_id)
    return record.replace_run(state, dataclasses.replace(value, protection_lost=True))


def freeze(
    state: record.TeamRoutines,
    run_id: str,
    lease: record.Lease,
    now: int,
    request: tuple[str, str, str, int],
) -> record.TeamRoutines:
    """Park a run for a person; it keeps no lease, and the same Routine never fires while it is frozen.

    ``request`` is its kind and the Assistant Action that asked at its call's position: a replay step, which must be
    that step of the plan. A Routine being deleted never freezes a run: its deletion ends only the
    frozen runs it saw.
    """
    request_kind, assistant_id, action, position = request
    value = routine_claim.live_lease(state, run_id, lease, now)
    current = record.routine(state, value.routine_id)
    if current.deleting:
        raise record.RoutineStateError("routine-deleting")
    steps = current.plan["steps"]
    placed = http_routine.canonical_position(position, len(steps))
    if (
        request_kind not in http_routine_notice.REQUEST_KINDS
        or http_identifiers.canonical_assistant_id(assistant_id) is None
        or http_identifiers.canonical_action_id(action) is None
        or placed is None
        or (steps[placed - 1]["assistant"], steps[placed - 1]["action"]) != (assistant_id, action)
    ):
        raise record.RoutineStateError("freeze-invalid")
    if sum(item.status == "frozen" for item in state.runs) >= record.MAX_FROZEN_RUNS:
        raise record.RoutineStateError("frozen-limit")
    state, value = record.run_notice(state, value, "frozen", now, {"request_kind": request_kind})
    unleased = {"lease_sha256": "", "lease_key": "", "lease_expires_at": 0}
    call = {"request_kind": request_kind, "assistant_id": assistant_id, "action": action}
    return record.replace_run(state, dataclasses.replace(value, status="frozen", **unleased, **call))


def thaw(state: record.TeamRoutines, run_id: str, now: int, requests_used: int) -> tuple[record.TeamRoutines, str]:
    """A human resumes a frozen run; it runs under a fresh internal lease that no machine assertion knows.

    A Routine being deleted never resumes a run, so its deletion ends each frozen run it saw without racing a replay.
    """
    value = record.run(state, run_id)
    if value.status != "frozen" or record.routine(state, value.routine_id).deleting:
        raise record.RoutineStateError("run-not-frozen")
    resumed, token = routine_claim.human_lease(value, now, requests_used=max(value.requests_used, requests_used))
    return record.replace_run(state, resumed), token


def finish(
    state: record.TeamRoutines, run_id: str, lease: record.Lease, now: int, shown: dict[str, object] | None = None
) -> record.TeamRoutines:
    """A worker ends its leased run, which completed every step, with a durable notice; only its live lease may."""
    return _completed(state, routine_claim.live_lease(state, run_id, lease, now), now, shown)


def _completed(
    state: record.TeamRoutines, value: record.Run, now: int, shown: dict[str, object] | None
) -> record.TeamRoutines:
    """End a run that completed every step, by its Routine's output disposition with ``shown``."""
    state = _completion(state, value, completed(value), now, shown)
    return record.without_run(state, value.run_id, now)


def _completion(
    state: record.TeamRoutines, value: record.Run, outcome: str, now: int, shown: dict | None
) -> record.TeamRoutines:
    """How a completed run tells the person, by its Routine's output disposition (ADR-0092 amendment, ADR-0101).

    ``show`` publishes the result every run; ``changes`` only when its keyed digest differs from the last shown one,
    recorded in the same write; ``none`` publishes nothing, a continuous Routine's healthy run rolling up into its
    minute's notice. A result not kept is unavailable, and so is every result after the run lost its protection, which
    never moves the ``changes`` baseline; a run with a notice of its own always gets its terminal version.
    """
    current = record.routine(state, value.routine_id)
    disposition = current.plan["output"]
    mode, step = disposition["mode"], disposition["step"]
    detail: dict[str, object] = {
        "plan": routine_definition.summary(current.plan, current.revision),
        "output": None,
    }
    if mode in http_routine.SHOWN_MODES:
        # On the wire the shown step is its position (ADR-0092 amendment, 2026-10-05, scale).
        position = routine_definition.disposition(current.plan)["step"]
        valid = shown is not None and shown.get("step") == step and not value.protection_lost
        output = shown["output"] if valid else routine_plan.output_state(position, "unavailable")
        digest = shown["digest"] if valid else None
        if mode == "changes" and output["state"] == "shown":
            if digest is not None and digest == current.output_digest:
                if not value.notice_version:
                    return _quiet(state, current)
                output = routine_plan.output_state(position, "unchanged")
            else:
                state = record.replace_routine(state, dataclasses.replace(current, output_digest=digest or ""))
        return record.run_notice(state, value, outcome, now, {**detail, "output": output})[0]
    if not value.notice_version and not value.protection_lost:
        rolled = _healthy(state, value, now) if outcome == "done" else None
        return _quiet(state, current) if rolled is None else rolled
    return record.run_notice(state, value, outcome, now, detail)[0]


def _quiet(state: record.TeamRoutines, current: record.Routine) -> record.TeamRoutines:
    """A completed run that tells the person nothing still resets its Routine's failure streak."""
    return record.replace_routine(state, dataclasses.replace(current, failures=0))


def _healthy(state: record.TeamRoutines, value: record.Run, now: int) -> record.TeamRoutines | None:
    """Roll a continuous Routine's healthy run into the one versioned notice of the minute it ended in, or None.

    Its count doubles as the notice version Admin acknowledges, so a notice acknowledged mid-minute is replaced by the
    next version, which carries the minute's summed usage (ADR-0092 section 9, ADR-0101).
    """
    current = record.routine(state, value.routine_id)
    if not record.continuous(current):
        return None
    minute = now - now % 60
    same = current.rollup_minute == minute
    runs = current.rollup_runs + 1 if same else 1
    # The rollup minute only moves forward: a clock stepped back into an earlier minute, which may already be
    # delivered, or past the count gaps allow, gives the run no rollup and leaves the counters as they are.
    if minute < current.rollup_minute or runs > http_routine.MAX_ROLLUP_RUNS:
        return None
    usage = joined_usage(current.rollup_usage, value.usage) if same and current.rollup_usage else value.usage
    rolled = dataclasses.replace(current, rollup_minute=minute, rollup_runs=runs, rollup_usage=usage, failures=0)
    state = record.replace_routine(state, rolled)
    notice_id = hashlib.sha256(f"healthy:{current.routine_id}:{minute}".encode()).hexdigest()[:32]
    notice = record.Notice(notice_id, current.routine_id, "", "healthy", minute, {"runs": runs}, runs)
    return record.put_notice(state, dataclasses.replace(notice, usage=copy.deepcopy(usage)))


def end(
    state: record.TeamRoutines,
    run_id: str,
    now: int,
    outcome: str,
    detail: dict[str, object],
    *,
    status: str = "",
) -> record.TeamRoutines:
    """Team itself ends a run without its lease: a human Stop or answer, an expired lease or deadline, or recovery.

    A leased run ends stopped or failed; a frozen run denied, stopped, or failed; one that may have acted is held
    instead (``fence``). With ``status``, the run must still be in it, so a stale decision never lands on a changed run.
    """
    value = record.run(state, run_id)
    if status and value.status != status:
        raise record.RoutineStateError("run-changed")
    allowed = {"leased": frozenset({"stopped", "failed"}), "frozen": record.FROZEN_OUTCOMES}.get(
        value.status, frozenset()
    )
    if outcome not in allowed:
        raise record.RoutineStateError("invalid-outcome")
    state, _value = record.run_notice(state, value, outcome, now, detail)
    return record.without_run(state, run_id, now)


def complete_recovered(
    state: record.TeamRoutines, run_id: str, lease_sha256: str, now: int, shown: dict[str, object] | None = None
) -> record.TeamRoutines:
    """Team's watchdog ends a leased run whose sealed cursor completed every step, as the worker would have."""
    return _completed(state, routine_claim.read_lease(state, run_id, lease_sha256), now, shown)


def completed(value: record.Run) -> str:
    """How a run that completed every step ends: recovered when a continuation after a hold completed it."""
    first = routine_claim.generation_for(routine_claim.network_of(value.generation, value.run_id), value.run_id)
    return "done" if value.generation == first else "recovered"
