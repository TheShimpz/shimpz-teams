"""Claiming a Team's due Routine runs, without I/O (ADR-0086, ADR-0092): sweeps, readiness, leases, and generations."""

import dataclasses
import hashlib
import secrets

from action import journal as action_journal
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import routine_run as http_routine_run
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record
from routine import starts as routine_starts


def _count_before(routine_value: record.Routine, first: int, limit: int) -> tuple[int, int]:
    """The firings from ``first`` up to (not including) ``limit``, bounded, and the first firing at or after it."""
    count, current = 0, first
    while current < limit and count < record.MAX_COUNTED_MISSES:
        count += 1
        current = record.next_after(routine_value, current)
    if current < limit:
        current = record.next_after(routine_value, limit - 1)
    return count, current


def _gap_notice_id(routine_value: record.Routine) -> str:
    return hashlib.sha256(f"skipped:{routine_value.routine_id}:{routine_value.gap_started_at}".encode()).hexdigest()[
        :32
    ]


def _report_gap(
    state: record.TeamRoutines, routine_value: record.Routine, now: int, *, in_flight: bool = False
) -> record.TeamRoutines:
    """Publish or update the one skipped notice of the Routine's current gap when it has new misses and room."""
    if routine_value.missed == routine_value.reported_missed:
        return state
    notice_id = _gap_notice_id(routine_value)
    pending = any(item.notice_id == notice_id for item in state.notices)
    if not (pending or in_flight or record.undelivered(state) < record.MAX_UNDELIVERED_NOTICES):
        return state
    # The count only grows within a gap, so it doubles as the notice version Admin acknowledges.
    missed = routine_value.missed
    state = record._notice(
        state, record.Notice(notice_id, routine_value.routine_id, "", "skipped", now, {"missed": missed}, missed)
    )
    return record._replace_routine(state, dataclasses.replace(routine_value, reported_missed=routine_value.missed))


def _miss(routine_value: record.Routine, first: int, count: int, next_run_at: int) -> record.Routine:
    return dataclasses.replace(
        routine_value,
        gap_started_at=routine_value.gap_started_at or first,
        missed=routine_value.missed + count,
        next_run_at=next_run_at,
    )


def sweep(state: record.TeamRoutines, now: int) -> record.TeamRoutines:
    """Skip every firing too late to start; a Routine's continuous gap is one notice, updated as it grows."""
    for item in state.routines:
        if record.continuous(item):
            # A continuous Routine has no backlog to skip: it starts again once it may.
            continue
        cutoff = now - record.grace_seconds(item)
        if item.next_run_at < cutoff:
            count, next_run_at = _count_before(item, item.next_run_at, cutoff)
            item = _miss(item, item.next_run_at, count, next_run_at)
            state = record._replace_routine(state, item)
        state = _report_gap(state, item, now)
    return state


def free_at(state: record.TeamRoutines, routine_value: record.Routine, now: int) -> int:
    """The earliest instant a due Routine may start under the Team ceiling and, for a continuous one, its own cap.

    A scheduled Routine's own firings already bound its starts, and a late start never delays the next firing.
    """
    cap = http_routine.daily_cap(routine_value.schedule) if record.continuous(routine_value) else None
    units = routine_definition.run_units(routine_value)
    return routine_starts.free_at(state.starts, routine_value.routine_id, cap, now, units)


def _due_at(routine_value: record.Routine) -> int:
    """When the Routine is next due: its next firing, or sooner a person's pending Rodar."""
    if routine_value.run_requested:
        return min(routine_value.next_run_at, routine_value.run_requested)
    return routine_value.next_run_at


def _ready(state: record.TeamRoutines, busy: set[str], long: bool = True) -> list[record.Routine]:
    """The Routines that may start once due and under their caps: confirmed, not paused, held, running, or long."""
    return [
        item
        for item in state.routines
        if not item.needs_reconfirm
        and not item.deleting
        and not item.paused
        and item.routine_id not in busy
        and (long or not routine_plan.long_run(routine_definition.run_units(item)))
    ]


def _segment_leased(state: record.TeamRoutines) -> bool:
    """Whether one of the Team's runs is leased to drive a segment; frozen and held runs hold no slot."""
    return any(item.status == "leased" for item in state.runs)


def _backpressured(state: record.TeamRoutines) -> bool:
    """Whether the Team must catch up before any run starts: undelivered notices, cleanup, or incident room."""
    return (
        record.undelivered(state) >= record.MAX_UNDELIVERED_NOTICES
        or len(state.discards) >= record.MAX_ROUTINES
        or not incident_capacity(state)
    )


def claimable(state: record.TeamRoutines, now: int, long: bool = True) -> record.Routine | None:
    """The Team's oldest due Routine that may start now, or None; the caller has already swept.

    A Team leases one run at a time (ADR-0092 section 9); without ``long``, only a short Routine may start.
    """
    if _backpressured(state) or _segment_leased(state):
        return None
    busy = {item.routine_id for item in state.runs} | held_routines(state)
    due = [item for item in _ready(state, busy, long) if _due_at(item) <= now and free_at(state, item, now) <= now]
    return min(due, key=lambda item: (_due_at(item), item.routine_id)) if due else None


def next_due(state: record.TeamRoutines, now: int) -> int | None:
    """The earliest instant after ``now`` one of the Team's Routines becomes due to start, or None.

    A capped Routine is due when its earliest start leaves the window; a paused, held, busy, deleting, or unconfirmed
    one wakes nothing until its own resolution does.
    """
    if _backpressured(state) or _segment_leased(state):
        # Nothing starts until notices are delivered or ended runs are cleaned up; the next reconciliation retries.
        return None
    busy = {item.routine_id for item in state.runs} | held_routines(state)
    due = [max(_due_at(item), free_at(state, item, now)) for item in _ready(state, busy)]
    return min((item for item in due if item > now), default=None)


def held_routines(state: record.TeamRoutines) -> set[str]:
    """Routines an unresolved incident holds: no cycle of theirs starts, whatever else resumes them."""
    return {item.routine_id for item in state.incidents if item.status == "unresolved"}


def incident_capacity(state: record.TeamRoutines) -> bool:
    """Whether one more run may start, reserving the room its incident would need.

    Each run that could be held reserves an unresolved incident's and a record's room, so a hold never displaces one.
    """
    unresolved = sum(item.status == "unresolved" for item in state.incidents)
    retained = sum(item.status != "released" for item in state.incidents)
    runs = len(state.runs) + 1
    return unresolved + runs <= record.MAX_UNRESOLVED_INCIDENTS and retained + runs <= record.MAX_INCIDENTS


def claim(
    state: record.TeamRoutines, now: int, key_fingerprint: str, long: bool = True
) -> tuple[record.TeamRoutines, record.Claim | None]:
    """Lease one run of the Team's oldest claimable Routine, rechecked on this exact state.

    Its next firing moves past ``now`` so it can never be claimed twice; only this late firing is made up, and any
    others since it join the Routine's gap, which ends here.
    """
    if http_payload.SHA256_RE.fullmatch(key_fingerprint) is None:
        raise record.RoutineStateError("routine-key-invalid")
    # Sweep first, so a firing too late to start is skipped here even if no sweep ran since it was due.
    state = sweep(state, now)
    due = claimable(state, now, long)
    if due is None:
        # The swept state is still returned: its skipped notices and advanced schedules must be persisted.
        return state, None
    if due.run_requested and due.next_run_at > now:
        # A person's Rodar starts on its own: the standing cadence and any gap it reports stay as they are.
        return _lease(state, dataclasses.replace(due, run_requested=0), due.run_requested, now, key_fingerprint)
    scheduled_at = due.next_run_at
    # A firing due now also serves any pending Rodar: one run, never two.
    due = dataclasses.replace(due, run_requested=0)
    if record.continuous(due):
        # Provisional: the run's end sets the next one its gap after it, and nothing starts while it runs.
        due = dataclasses.replace(due, next_run_at=record.next_after(due, now))
    else:
        following = record.next_after(due, scheduled_at)
        extra, next_run_at = _count_before(due, following, now + 1)
        due = _miss(due, following, extra, next_run_at) if extra else dataclasses.replace(due, next_run_at=next_run_at)
    state = _report_gap(record._replace_routine(state, due), due, now, in_flight=True)
    ended = dataclasses.replace(record.routine(state, due.routine_id), gap_started_at=0, missed=0, reported_missed=0)
    return _lease(state, ended, scheduled_at, now, key_fingerprint)


def _lease(
    state: record.TeamRoutines, due: record.Routine, scheduled_at: int, now: int, key_fingerprint: str
) -> tuple[record.TeamRoutines, record.Claim]:
    """Start one run of the claimed Routine under every start cap, its lease covering only its segment's start."""
    token = secrets.token_urlsafe(32)
    units = routine_definition.run_units(due)
    leased = record.Run(
        run_id=record.new_id(),
        routine_id=due.routine_id,
        status="leased",
        scheduled_at=scheduled_at,
        lease_sha256=record.lease_sha256(token),
        lease_key=key_fingerprint,
        lease_expires_at=now + record.LEASE_SECONDS,
        active_seconds_left=routine_plan.active_seconds(units),
    )
    state = dataclasses.replace(
        record._replace_routine(state, due),
        runs=(*state.runs, leased),
        served_at=now,
        starts=routine_starts.started(state.starts, due.routine_id, now, units),
    )
    mode = http_routine_run.run_mode(due.schedule)
    digest = routine_definition.plan_digest(due.plan)
    return state, record.Claim(leased, token, due.revision, digest, mode, leased.active_seconds_left)


def lease_until(value: record.Run, now: int) -> int:
    """A running segment's lease: the run's active time left, and a margin, so a deadline cuts it before the lease."""
    return now + max(value.active_seconds_left, 0) + routine_plan.LEASE_MARGIN_SECONDS


def require_lease(value: record.Run, lease: record.Lease, now: int) -> None:
    """Only the live lease of a leased run, claimed under the current routine key, may drive it."""
    if (
        value.status != "leased"
        or not secrets.compare_digest(value.lease_sha256, lease.sha256)
        or value.lease_key != lease.key
        or value.lease_expires_at <= now
        or value.active_seconds_left <= 0
    ):
        raise record.RoutineStateError("lease-invalid")


def _leased(state: record.TeamRoutines, run_id: str) -> record.Run:
    value = record.run(state, run_id)
    if value.status != "leased":
        raise record.RoutineStateError("run-not-running")
    return value


def _live(state: record.TeamRoutines, run_id: str, lease: record.Lease, now: int) -> record.Run:
    """A worker transition: only the live lease of that exact run may make it."""
    value = _leased(state, run_id)
    require_lease(value, lease, now)
    return value


def generation_for(network_id: str, run_id: str, suffix: str = "") -> str:
    """A run's journal generation in the Team's network.

    A continuation (``s<n>``) or verification (``v<n>``) after a hold gets its own: the held one is archived.
    """
    if suffix and record._SUFFIX_RE.fullmatch(suffix) is None:
        raise record.RoutineStateError("generation-invalid")
    return f"{network_id}:routine:{run_id}" + (f":{suffix}" if suffix else "")


def network_of(generation: object, run_id: str) -> str | None:
    """The Team network a run's generation, or one of its continuation or verification generations, belongs to."""
    match = record._GENERATION_RE.fullmatch(generation) if isinstance(generation, str) else None
    return match["network"] if match is not None and match["run"] == run_id else None


def bind_generation(
    state: record.TeamRoutines, run_id: str, lease: record.Lease, now: int, network_id: str
) -> record.TeamRoutines:
    """Bind the run's own journal generation, derived from the Team's trusted network id, once; it never changes.

    A continuation after a hold already names its own generation, which must belong to the same network.
    """
    value = _live(state, run_id, lease, now)
    generation = value.generation or generation_for(network_id, run_id)
    if action_journal.SAFE_ID_RE.fullmatch(generation) is None or network_of(generation, run_id) != network_id:
        raise record.RoutineStateError("generation-invalid")
    # The claimed run's segment has started: its lease now covers the run's active time left, once; a later bind of the
    # same run (a retried request) never renews it.
    extended = value.lease_expires_at if value.generation else max(value.lease_expires_at, lease_until(value, now))
    return record._replace_run(state, dataclasses.replace(value, generation=generation, lease_expires_at=extended))
