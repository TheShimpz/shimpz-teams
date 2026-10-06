"""A claimed run's transitions, without I/O (ADR-0086, ADR-0092, ADR-0101): spending, freezing, thawing, and ending."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import secrets

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
    value = routine_claim._live(state, run_id, lease, now)
    usage = {**value.usage, "duration_ms": min(value.usage["duration_ms"] + milliseconds, MAX_USAGE_MS)}
    return record._replace_run(
        state, dataclasses.replace(value, active_seconds_left=value.active_seconds_left - seconds, usage=usage)
    )


# A run's summed active duration, the chat usage bound (ADR-0082).
MAX_USAGE_MS = http_payload.MAX_TURN_DURATION_MS


def joined_usage(first: dict[str, object], second: dict[str, object]) -> dict[str, object]:
    """Two usages summed: their durations, and each provider and model's tokens, models sorted and distinct."""
    totals: dict[tuple[str, str], list[int]] = {}
    for model in [*first["models"], *second["models"]]:
        counts = totals.setdefault((model["provider"], model["model"]), [0, 0])
        counts[0] = min(counts[0] + model["input_tokens"], http_payload.MAX_TURN_USAGE_TOKENS)
        counts[1] = min(counts[1] + model["output_tokens"], http_payload.MAX_TURN_USAGE_TOKENS)
    models = [
        {"provider": provider, "model": model, "input_tokens": counts[0], "output_tokens": counts[1]}
        for (provider, model), counts in sorted(totals.items())
    ]
    duration = min(first["duration_ms"] + second["duration_ms"], MAX_USAGE_MS)
    return {"duration_ms": duration, "models": models[: http_payload.MAX_TURN_USAGE_MODELS]}


def used(state: record.TeamRoutines, run_id: str, models: list[dict[str, object]]) -> record.TeamRoutines:
    """Add the tokens a run's model calls reported, its recovery's included, to its usage (ADR-0082, ADR-0101)."""
    value = record.run(state, run_id)
    usage = joined_usage(value.usage, {"duration_ms": 0, "models": models})
    if http_routine.canonical_run_usage(usage) != usage:
        raise record.RoutineStateError("usage-invalid")
    return record._replace_run(state, dataclasses.replace(value, usage=usage))


def lose_protection(state: record.TeamRoutines, run_id: str) -> record.TeamRoutines:
    """The run lost the protection of its secret values; every later version of its notice says so, for good."""
    value = record.run(state, run_id)
    return record._replace_run(state, dataclasses.replace(value, protection_lost=True))


def freeze(
    state: record.TeamRoutines,
    run_id: str,
    lease: record.Lease,
    now: int,
    request: tuple[str, str, str, dict[str, object]],
) -> record.TeamRoutines:
    """Park a run for a person; it keeps no lease, and the same Routine never fires while it is frozen.

    ``request`` is its kind and the Assistant Action that asked at its call's position: a replay step, which must be
    that step of the plan, or a decision call. A Routine being deleted never freezes a run: its deletion ends only the
    frozen runs it saw.
    """
    request_kind, assistant_id, action, position = request
    value = routine_claim._live(state, run_id, lease, now)
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
        or (
            placed["phase"] == "replay"
            and (steps[placed["step"] - 1]["assistant"], steps[placed["step"] - 1]["action"]) != (assistant_id, action)
        )
    ):
        raise record.RoutineStateError("freeze-invalid")
    if sum(item.status == "frozen" for item in state.runs) >= record.MAX_FROZEN_RUNS:
        raise record.RoutineStateError("frozen-limit")
    fields = {"request_kind": request_kind, "assistant_id": assistant_id, "action": action, "position": placed}
    state, value = record._run_notice(state, value, "frozen", now, {**fields, "steps": len(steps)})
    unleased = {"lease_sha256": "", "lease_key": "", "lease_expires_at": 0}
    return record._replace_run(
        state, dataclasses.replace(value, status="frozen", **unleased, **fields, steps=len(steps))
    )


def thaw(state: record.TeamRoutines, run_id: str, now: int, requests_used: int) -> tuple[record.TeamRoutines, str]:
    """A human resumes a frozen run; it runs under a fresh internal lease that no machine assertion knows.

    A Routine being deleted never resumes a run, so its deletion ends each frozen run it saw without racing a replay.
    """
    value = record.run(state, run_id)
    if value.status != "frozen" or record.routine(state, value.routine_id).deleting:
        raise record.RoutineStateError("run-not-frozen")
    token = secrets.token_urlsafe(32)
    resumed = dataclasses.replace(
        value,
        status="leased",
        lease_sha256=record.lease_sha256(token),
        lease_key=record.HUMAN_LEASE,
        # The person's answer runs its segment at once, over the run's active time left.
        lease_expires_at=routine_claim.lease_until(value, now),
        request_kind="",
        assistant_id="",
        action="",
        position=None,
        steps=0,
        requests_used=max(value.requests_used, requests_used),
    )
    return record._replace_run(state, resumed), token


def finish(
    state: record.TeamRoutines,
    run_id: str,
    lease: record.Lease,
    now: int,
    outcome: str,
    detail: dict[str, object],
    shown: dict[str, object] | None = None,
) -> record.TeamRoutines:
    """A worker ends its leased run with a durable notice; only its live lease may.

    A completed run ends by its Routine's output disposition with ``shown``; any other outcome publishes ``detail``.
    """
    value = routine_claim._live(state, run_id, lease, now)
    if outcome not in record._RUN_OUTCOMES:
        raise record.RoutineStateError("invalid-outcome")
    if outcome in {"done", "recovered"}:
        state = _completion(state, value, outcome, now, shown)
    else:
        state = record._run_notice(state, value, outcome, now, detail)[0]
    return record._without_run(state, run_id, now)


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
        "decision": None,
    }
    if mode in routine_plan.SHOWN_MODES:
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
                state = record._replace_routine(state, dataclasses.replace(current, output_digest=digest or ""))
        return record._run_notice(state, value, outcome, now, {**detail, "output": output})[0]
    if not value.notice_version and not value.protection_lost:
        rolled = _healthy(state, value, now) if outcome == "done" else None
        return _quiet(state, current) if rolled is None else rolled
    return record._run_notice(state, value, outcome, now, detail)[0]


def _quiet(state: record.TeamRoutines, current: record.Routine) -> record.TeamRoutines:
    """A completed run that tells the person nothing still resets its Routine's failure streak."""
    return record._replace_routine(state, dataclasses.replace(current, failures=0))


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
    state = record._replace_routine(state, rolled)
    notice_id = hashlib.sha256(f"healthy:{current.routine_id}:{minute}".encode()).hexdigest()[:32]
    notice = record.Notice(notice_id, current.routine_id, "", "healthy", minute, {"runs": runs}, runs)
    return record._notice(state, dataclasses.replace(notice, usage=copy.deepcopy(usage)))


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
    allowed = {"leased": frozenset({"stopped", "failed"}), "frozen": record._FROZEN_OUTCOMES}.get(
        value.status, frozenset()
    )
    if outcome not in allowed:
        raise record.RoutineStateError("invalid-outcome")
    state, _value = record._run_notice(state, value, outcome, now, detail)
    return record._without_run(state, run_id, now)


def complete_recovered(
    state: record.TeamRoutines, run_id: str, lease_sha256: str, now: int, shown: dict[str, object] | None = None
) -> record.TeamRoutines:
    """Team's watchdog ends a leased run whose sealed cursor completed every step, as the worker would have."""
    value = routine_claim._leased(state, run_id)
    if not secrets.compare_digest(value.lease_sha256, lease_sha256):
        raise record.RoutineStateError("run-changed")
    state = _completion(state, value, completed(value), now, shown)
    return record._without_run(state, run_id, now)


def completed(value: record.Run) -> str:
    """How a run that completed every step ends: recovered when a continuation after a hold completed it."""
    first = routine_claim.generation_for(routine_claim.network_of(value.generation, value.run_id), value.run_id)
    return "done" if value.generation == first else "recovered"
