"""A Team's Routines, runs, and notices, and every transition between them, without I/O (ADR-0086).

All instants are whole UTC epoch seconds. A transition validates the current state itself and returns a new
``TeamRoutines``; the caller persists it atomically before acting on it, so a crash leaves either the old or the new
state, never a mix.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import re
import secrets
from dataclasses import dataclass

from protocol.http.v1 import routine as http_routine
from routine import schedule

MAX_ROUTINES = http_routine.MAX_ROUTINES
MAX_DAILY_STARTS = 24
MAX_FROZEN_RUNS = 8
MAX_UNDELIVERED_NOTICES = 32
LEASE_SECONDS = 900
ACTIVE_SECONDS = 600
MAX_GRACE_SECONDS = 12 * 3600
# Bounds the work of counting firings missed during a long outage; the count is reported, never replayed.
MAX_COUNTED_MISSES = 24 * 400
HUMAN_LEASE = "human"
_PERIOD_SECONDS = {"daily": 86_400, "weekly": 7 * 86_400, "monthly": 28 * 86_400}
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
# From a frozen run only these outcomes are possible: nobody answered it, or someone refused or stopped it.
_FROZEN_OUTCOMES = frozenset({"denied", "stopped", "failed"})
_RUN_OUTCOMES = http_routine.OUTCOMES - {"skipped", "scope-changed", "frozen"}
# Ended runs whose Brain thread, journal generation, and continuation Team has yet to remove. Claims stop while any
# wait, and each run ends once, so the queue never outgrows the runs a Team can hold.
MAX_DISCARDS = 2 * MAX_ROUTINES


class RoutineStateError(ValueError):
    """A requested Routine transition is not allowed in the current state."""


@dataclass(frozen=True, slots=True)
class Routine:
    routine_id: str
    quote: str
    schedule: dict[str, object]
    timezone: str
    # (assistant_id, contract digest) pinned at confirmation, sorted by assistant id.
    assistants: tuple[tuple[str, str], ...]
    anchor: int
    next_run_at: int
    needs_reconfirm: bool = False
    deleting: bool = False
    # One continuous stretch of missed firings: when it began, how many, and how many a notice already reports.
    gap_started_at: int = 0
    missed: int = 0
    reported_missed: int = 0


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str
    routine_id: str
    # "leased": may run a segment; "frozen": waits for a human; "uncertain": holds an unresolved Action batch.
    status: str
    scheduled_at: int
    lease_sha256: str = ""
    lease_key: str = ""
    lease_expires_at: int = 0
    active_seconds_left: int = ACTIVE_SECONDS
    request_kind: str = ""
    assistant_id: str = ""
    action: str = ""
    # The run's own Action journal generation, bound at its first segment, and the batch an uncertain run holds.
    generation: str = ""
    batch: tuple[str, str] = ("", "")
    # A run has one notice, keyed by its id; each freeze and its end update it, so Admin replaces one transcript row.
    notice_version: int = 0


@dataclass(frozen=True, slots=True)
class Notice:
    notice_id: str
    routine_id: str
    run_id: str
    outcome: str
    created_at: int
    detail: dict[str, object]
    # Grows when a skipped notice is updated; Admin acknowledges the exact version it delivered.
    version: int = 1


@dataclass(frozen=True, slots=True)
class TeamRoutines:
    routines: tuple[Routine, ...] = ()
    runs: tuple[Run, ...] = ()
    notices: tuple[Notice, ...] = ()
    served_at: int = 0
    starts_day: str = ""
    starts: int = 0
    # (run_id, generation) of ended runs whose Brain thread, journal generation, and continuation are still held.
    discards: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Claim:
    run: Run
    lease_token: str


@dataclass(frozen=True, slots=True, repr=False)
class Lease:
    """What a worker proves to drive a leased run: its lease digest and the key it was claimed under.

    Admin holds the token and signs its digest into each Routine assertion; Team only ever compares digests.
    """

    sha256: str
    key: str


def lease_of(token: str, key: str) -> Lease:
    return Lease(lease_sha256(token), key)


def new_id() -> str:
    return secrets.token_hex(16)


def lease_sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _instant(epoch: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC)


def next_after(routine_value: Routine, after: int) -> int:
    return int(
        schedule.next_run(
            routine_value.schedule, routine_value.timezone, _instant(routine_value.anchor), _instant(after)
        ).timestamp()
    )


def grace_seconds(routine_value: Routine) -> int:
    """How late a run may still start: one period, and at most 12 hours."""
    kind = routine_value.schedule["kind"]
    period = routine_value.schedule["every"] * 3600 if kind == "hourly" else _PERIOD_SECONDS[kind]
    return min(period, MAX_GRACE_SECONDS)


def _admitted(value: Routine) -> Routine:
    """A copy of a new Routine in the closed contract; anything else is refused before it can be persisted."""
    canonical = http_routine.canonical_schedule(value.schedule)
    assistants = tuple(value.assistants)
    try:
        schedule.zone(value.timezone)
    except schedule.ScheduleError as exc:
        raise RoutineStateError("routine-invalid") from exc
    if (
        http_routine.ROUTINE_ID_RE.fullmatch(value.routine_id) is None
        or http_routine.canonical_quote(value.quote) is None
        or canonical is None
        or not assistants
        or len(assistants) > http_routine.MAX_NOTICE_ASSISTANTS
        or list(assistants) != sorted(set(assistants))
        or len({assistant for assistant, _digest in assistants}) != len(assistants)
        or not all(
            http_routine.ASSISTANT_ID_RE.fullmatch(assistant) and _DIGEST_RE.fullmatch(digest)
            for assistant, digest in assistants
        )
        or type(value.anchor) is not int
        or value.next_run_at != next_after(dataclasses.replace(value, schedule=canonical), value.anchor)
    ):
        raise RoutineStateError("routine-invalid")
    return Routine(
        value.routine_id, value.quote, canonical, value.timezone, assistants, value.anchor, value.next_run_at
    )


def daily_rate_allows(routines: tuple[Routine, ...], schedule_value: dict[str, object]) -> bool:
    total = sum((http_routine.daily_rate(item.schedule) for item in routines), http_routine.daily_rate(schedule_value))
    return total <= http_routine.MAX_DAILY_RUNS


def add_routine(state: TeamRoutines, value: Routine) -> TeamRoutines:
    admitted = _admitted(value)
    if any(item.routine_id == admitted.routine_id for item in state.routines):
        raise RoutineStateError("routine-exists")
    if len(state.routines) >= MAX_ROUTINES:
        raise RoutineStateError("routine-limit")
    if not daily_rate_allows(state.routines, admitted.schedule):
        raise RoutineStateError("routine-rate-limit")
    return dataclasses.replace(state, routines=(*state.routines, admitted))


def routine(state: TeamRoutines, routine_id: str) -> Routine:
    for item in state.routines:
        if item.routine_id == routine_id:
            return item
    raise RoutineStateError("routine-not-found")


def run(state: TeamRoutines, run_id: str) -> Run:
    for item in state.runs:
        if item.run_id == run_id:
            return item
    raise RoutineStateError("run-not-found")


def _replace_routine(state: TeamRoutines, updated: Routine) -> TeamRoutines:
    return dataclasses.replace(
        state, routines=tuple(updated if item.routine_id == updated.routine_id else item for item in state.routines)
    )


def _replace_run(state: TeamRoutines, updated: Run) -> TeamRoutines:
    return dataclasses.replace(
        state, runs=tuple(updated if item.run_id == updated.run_id else item for item in state.runs)
    )


def _without_run(state: TeamRoutines, run_id: str) -> TeamRoutines:
    """Remove an ended run and, in the same write, queue the removal of everything it held."""
    value = run(state, run_id)
    return dataclasses.replace(
        state,
        runs=tuple(item for item in state.runs if item.run_id != run_id),
        discards=(*state.discards, (run_id, value.generation)),
    )


def discarded(state: TeamRoutines, run_id: str) -> TeamRoutines:
    """Team removed an ended run's thread, journal generation, and continuation."""
    return dataclasses.replace(state, discards=tuple(item for item in state.discards if item[0] != run_id))


def _run_notice(state: TeamRoutines, value: Run, outcome: str, now: int, detail: dict[str, object]):
    """Publish the next version of the run's one notice; returns the state and the run carrying that version."""
    version = value.notice_version + 1
    state = _notice(state, Notice(value.run_id, value.routine_id, value.run_id, outcome, now, detail, version))
    return state, dataclasses.replace(value, notice_version=version)


def undelivered(state: TeamRoutines) -> int:
    return len(state.notices)


def _notice(state: TeamRoutines, notice: Notice) -> TeamRoutines:
    """Add or update an undelivered notice, which is never evicted.

    Claims and new skip reports stop at MAX_UNDELIVERED_NOTICES, so the outcome of every run in flight (at most one per
    Routine) always fits above it.
    """
    detail = http_routine.canonical_notice_detail(notice.outcome, notice.detail)
    if detail is None:
        raise RoutineStateError("notice-invalid")
    notice = dataclasses.replace(notice, detail=detail)
    if type(notice.version) is not int or notice.version < 1:
        raise RoutineStateError("notice-invalid")
    kept = tuple(item for item in state.notices if item.notice_id != notice.notice_id)
    if len(kept) >= MAX_UNDELIVERED_NOTICES + MAX_ROUTINES:
        raise RoutineStateError("notices-full")
    return dataclasses.replace(state, notices=(*kept, notice))


def _count_before(routine_value: Routine, first: int, limit: int) -> tuple[int, int]:
    """The firings from ``first`` up to (not including) ``limit``, bounded, and the first firing at or after it."""
    count, current = 0, first
    while current < limit and count < MAX_COUNTED_MISSES:
        count += 1
        current = next_after(routine_value, current)
    if current < limit:
        current = next_after(routine_value, limit - 1)
    return count, current


def _gap_notice_id(routine_value: Routine) -> str:
    return hashlib.sha256(f"skipped:{routine_value.routine_id}:{routine_value.gap_started_at}".encode()).hexdigest()[
        :32
    ]


def _report_gap(state: TeamRoutines, routine_value: Routine, now: int, *, in_flight: bool = False) -> TeamRoutines:
    """Publish or update the one skipped notice of the Routine's current gap when it has new misses and room."""
    if routine_value.missed == routine_value.reported_missed:
        return state
    notice_id = _gap_notice_id(routine_value)
    pending = any(item.notice_id == notice_id for item in state.notices)
    if not (pending or in_flight or undelivered(state) < MAX_UNDELIVERED_NOTICES):
        return state
    # The count only grows within a gap, so it doubles as the notice version Admin acknowledges.
    missed = routine_value.missed
    state = _notice(state, Notice(notice_id, routine_value.routine_id, "", "skipped", now, {"missed": missed}, missed))
    return _replace_routine(state, dataclasses.replace(routine_value, reported_missed=routine_value.missed))


def _miss(routine_value: Routine, first: int, count: int, next_run_at: int) -> Routine:
    return dataclasses.replace(
        routine_value,
        gap_started_at=routine_value.gap_started_at or first,
        missed=routine_value.missed + count,
        next_run_at=next_run_at,
    )


def sweep(state: TeamRoutines, now: int) -> TeamRoutines:
    """Skip every firing too late to start; a Routine's continuous gap is one notice, updated as it grows."""
    for item in state.routines:
        cutoff = now - grace_seconds(item)
        if item.next_run_at < cutoff:
            count, next_run_at = _count_before(item, item.next_run_at, cutoff)
            item = _miss(item, item.next_run_at, count, next_run_at)
            state = _replace_routine(state, item)
        state = _report_gap(state, item, now)
    return state


def _utc_day(now: int) -> str:
    return _instant(now).date().isoformat()


def starts_today(state: TeamRoutines, now: int) -> int:
    return state.starts if state.starts_day == _utc_day(now) else 0


def claimable(state: TeamRoutines, now: int) -> Routine | None:
    """The Team's oldest due Routine that may start now, or None; the caller has already swept."""
    if (
        undelivered(state) >= MAX_UNDELIVERED_NOTICES
        or starts_today(state, now) >= MAX_DAILY_STARTS
        or len(state.discards) >= MAX_ROUTINES
    ):
        return None
    busy = {item.routine_id for item in state.runs}
    due = [
        item
        for item in state.routines
        if item.next_run_at <= now and not item.needs_reconfirm and not item.deleting and item.routine_id not in busy
    ]
    return min(due, key=lambda item: (item.next_run_at, item.routine_id)) if due else None


def claim(state: TeamRoutines, now: int, key_fingerprint: str) -> tuple[TeamRoutines, Claim | None]:
    """Lease one run of the Team's oldest claimable Routine, rechecked on this exact state.

    Its next firing moves past ``now`` so it can never be claimed twice; only this late firing is made up, and any
    others since it join the Routine's gap, which ends here.
    """
    if _FINGERPRINT_RE.fullmatch(key_fingerprint) is None:
        raise RoutineStateError("routine-key-invalid")
    # Sweep first, so a firing too late to start is skipped here even if no sweep ran since it was due.
    state = sweep(state, now)
    due = claimable(state, now)
    if due is None:
        # The swept state is still returned: its skipped notices and advanced schedules must be persisted.
        return state, None
    scheduled_at = due.next_run_at
    following = next_after(due, scheduled_at)
    extra, next_run_at = _count_before(due, following, now + 1)
    due = _miss(due, following, extra, next_run_at) if extra else dataclasses.replace(due, next_run_at=next_run_at)
    state = _report_gap(_replace_routine(state, due), due, now, in_flight=True)
    ended = dataclasses.replace(routine(state, due.routine_id), gap_started_at=0, missed=0, reported_missed=0)
    token = secrets.token_urlsafe(32)
    leased = Run(
        run_id=new_id(),
        routine_id=due.routine_id,
        status="leased",
        scheduled_at=scheduled_at,
        lease_sha256=lease_sha256(token),
        lease_key=key_fingerprint,
        lease_expires_at=now + LEASE_SECONDS,
    )
    state = dataclasses.replace(
        _replace_routine(state, ended),
        runs=(*state.runs, leased),
        served_at=now,
        starts_day=_utc_day(now),
        starts=starts_today(state, now) + 1,
    )
    return state, Claim(leased, token)


def require_lease(value: Run, lease: Lease, now: int) -> None:
    """Only the live lease of a leased run, claimed under the current routine key, may drive it."""
    if (
        value.status != "leased"
        or not secrets.compare_digest(value.lease_sha256, lease.sha256)
        or value.lease_key != lease.key
        or value.lease_expires_at <= now
        or value.active_seconds_left <= 0
    ):
        raise RoutineStateError("lease-invalid")


def _leased(state: TeamRoutines, run_id: str) -> Run:
    value = run(state, run_id)
    if value.status != "leased":
        raise RoutineStateError("run-not-running")
    return value


def _live(state: TeamRoutines, run_id: str, lease: Lease, now: int) -> Run:
    """A worker transition: only the live lease of that exact run may make it."""
    value = _leased(state, run_id)
    require_lease(value, lease, now)
    return value


def generation_for(network_id: str, run_id: str) -> str:
    return f"{network_id}:routine:{run_id}"


def bind_generation(state: TeamRoutines, run_id: str, lease: Lease, now: int, network_id: str) -> TeamRoutines:
    """Bind the run's own journal generation, derived from the Team's trusted network id, once; it never changes."""
    value = _live(state, run_id, lease, now)
    generation = generation_for(network_id, run_id)
    if _SAFE_ID_RE.fullmatch(generation) is None or value.generation not in {"", generation}:
        raise RoutineStateError("generation-invalid")
    return _replace_run(state, dataclasses.replace(value, generation=generation))


def spend(state: TeamRoutines, run_id: str, lease: Lease, now: int, seconds: int) -> TeamRoutines:
    if type(seconds) is not int or seconds < 0:
        raise RoutineStateError("invalid-duration")
    value = _live(state, run_id, lease, now)
    return _replace_run(state, dataclasses.replace(value, active_seconds_left=value.active_seconds_left - seconds))


def freeze(
    state: TeamRoutines, run_id: str, lease: Lease, now: int, request_kind: str, assistant_id: str, action: str
) -> TeamRoutines:
    """Park a run for a human; it keeps no lease, and the same Routine never fires while it is frozen."""
    value = _live(state, run_id, lease, now)
    if (
        request_kind not in {"human", "integrations"}
        or http_routine.ASSISTANT_ID_RE.fullmatch(assistant_id) is None
        or http_routine.ACTION_ID_RE.fullmatch(action) is None
    ):
        raise RoutineStateError("freeze-invalid")
    if sum(item.status == "frozen" for item in state.runs) >= MAX_FROZEN_RUNS:
        raise RoutineStateError("frozen-limit")
    detail = {"request_kind": request_kind, "assistant_id": assistant_id, "action": action}
    state, value = _run_notice(state, value, "frozen", now, detail)
    frozen = dataclasses.replace(
        value,
        status="frozen",
        lease_sha256="",
        lease_key="",
        lease_expires_at=0,
        request_kind=request_kind,
        assistant_id=assistant_id,
        action=action,
    )
    return _replace_run(state, frozen)


def thaw(state: TeamRoutines, run_id: str, now: int) -> tuple[TeamRoutines, str]:
    """A human resumes a frozen run; it runs under a fresh internal lease that no machine assertion knows.

    A Routine being deleted never resumes a run, so its deletion ends each frozen run it saw without racing a replay.
    """
    value = run(state, run_id)
    if value.status != "frozen" or routine(state, value.routine_id).deleting:
        raise RoutineStateError("run-not-frozen")
    token = secrets.token_urlsafe(32)
    resumed = dataclasses.replace(
        value,
        status="leased",
        lease_sha256=lease_sha256(token),
        lease_key=HUMAN_LEASE,
        lease_expires_at=now + LEASE_SECONDS,
        request_kind="",
        assistant_id="",
        action="",
    )
    return _replace_run(state, resumed), token


def _hold(state: TeamRoutines, value: Run, fingerprint: str, now: int, detail: dict[str, object]) -> TeamRoutines:
    if not value.generation or _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise RoutineStateError("batch-invalid")
    state, value = _run_notice(state, value, "uncertain", now, detail)
    held = dataclasses.replace(
        value,
        status="uncertain",
        lease_sha256="",
        lease_key="",
        lease_expires_at=0,
        batch=(value.generation, fingerprint),
    )
    return _replace_run(state, held)


def finish(
    state: TeamRoutines, run_id: str, lease: Lease, now: int, outcome: str, detail: dict[str, object]
) -> TeamRoutines:
    """A worker ends its leased run with a durable notice; only its live lease may."""
    value = _live(state, run_id, lease, now)
    if outcome not in _RUN_OUTCOMES - {"uncertain"}:
        raise RoutineStateError("invalid-outcome")
    state, _value = _run_notice(state, value, outcome, now, detail)
    return _without_run(state, run_id)


def hold_uncertain(
    state: TeamRoutines, run_id: str, lease: Lease, now: int, fingerprint: str, detail: dict[str, object]
) -> TeamRoutines:
    """A worker's batch in the run's own generation may have acted: hold the run until a human resolves it."""
    return _hold(state, _live(state, run_id, lease, now), fingerprint, now, detail)


def end(
    state: TeamRoutines,
    run_id: str,
    now: int,
    outcome: str,
    detail: dict[str, object],
    fingerprint: str = "",
    *,
    status: str = "",
) -> TeamRoutines:
    """Team itself ends a run without its lease: a human Stop or answer, an expired lease or deadline, or recovery.

    A leased run ends stopped or failed, or is held uncertain when ``fingerprint`` names a batch that may have acted in
    its generation; a frozen run ends denied, stopped, or failed. With ``status``, the run must still be in it, so an
    ending decided on an earlier read never lands on a run that changed since.
    """
    value = run(state, run_id)
    if status and value.status != status:
        raise RoutineStateError("run-changed")
    if value.status == "leased" and fingerprint:
        if outcome != "uncertain":
            raise RoutineStateError("invalid-outcome")
        return _hold(state, value, fingerprint, now, detail)
    allowed = {"leased": frozenset({"stopped", "failed"}), "frozen": _FROZEN_OUTCOMES}.get(value.status, frozenset())
    if outcome not in allowed:
        raise RoutineStateError("invalid-outcome")
    state, _value = _run_notice(state, value, outcome, now, detail)
    return _without_run(state, run_id)


def resolve_uncertain(state: TeamRoutines, run_id: str, fingerprint: str) -> TeamRoutines:
    """A Supervisor's informed resolution of that exact batch: the only transition that releases its Routine."""
    value = run(state, run_id)
    if value.status != "uncertain" or value.batch != (value.generation, fingerprint):
        raise RoutineStateError("run-not-uncertain")
    return _without_run(state, run_id)


def acknowledge(state: TeamRoutines, delivered: frozenset[tuple[str, int]]) -> TeamRoutines:
    """Admin delivered these exact notice versions; a notice updated since stays. Delivery never changes a run."""
    return dataclasses.replace(
        state, notices=tuple(item for item in state.notices if (item.notice_id, item.version) not in delivered)
    )


def expired(state: TeamRoutines, now: int) -> tuple[Run, ...]:
    """Leased runs whose lease or active time ran out; the caller stops each and decides its outcome."""
    return tuple(
        item
        for item in state.runs
        if item.status == "leased" and (item.lease_expires_at <= now or item.active_seconds_left <= 0)
    )


def rekeyed(state: TeamRoutines, key_fingerprint: str) -> tuple[Run, ...]:
    """Machine-leased runs claimed under a routine key that is no longer current."""
    return tuple(
        item for item in state.runs if item.status == "leased" and item.lease_key not in {key_fingerprint, HUMAN_LEASE}
    )


def begin_delete(state: TeamRoutines, routine_id: str) -> tuple[TeamRoutines, tuple[Run, ...]]:
    """Mark a Routine as deleting, so it is never claimed or resumed again; its runs are returned for the caller to end.

    An uncertain run refuses the deletion: only a Supervisor's informed resolution of its exact batch releases it.
    """
    value = routine(state, routine_id)
    runs = tuple(item for item in state.runs if item.routine_id == routine_id)
    if any(item.status == "uncertain" for item in runs):
        raise RoutineStateError("routine-run-uncertain")
    return _replace_routine(state, dataclasses.replace(value, deleting=True)), runs


def complete_delete(state: TeamRoutines, routine_id: str) -> TeamRoutines:
    """Remove a deleting Routine once none of its runs remains; its undelivered notices stay."""
    value = routine(state, routine_id)
    if not value.deleting or any(item.routine_id == routine_id for item in state.runs):
        raise RoutineStateError("routine-busy")
    return dataclasses.replace(state, routines=tuple(item for item in state.routines if item.routine_id != routine_id))


def mark_scope_changed(state: TeamRoutines, routine_id: str, now: int, assistants: list[str]) -> TeamRoutines:
    """The Routine's Assistants no longer match what the user confirmed: record it and stop claiming it."""
    value = routine(state, routine_id)
    state = _notice(state, Notice(new_id(), routine_id, "", "scope-changed", now, {"assistants": assistants}))
    return _replace_routine(state, dataclasses.replace(value, needs_reconfirm=True))
