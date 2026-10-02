"""A Team's Routines, runs, and notices, and every transition between them, without I/O (ADR-0086).

All instants are whole UTC epoch seconds. A transition validates the current state itself and returns a new
``TeamRoutines``; the caller persists it atomically before acting on it, so a crash leaves either the old or the new
state, never a mix.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import hashlib
import re
import secrets
from dataclasses import dataclass

from protocol.http.v1 import routine as http_routine
from routine import grant as routine_grant
from routine import plan as routine_plan
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
_SUFFIX_RE = re.compile(r"[sv][1-9][0-9]{0,2}\Z")
_GENERATION_RE = re.compile(
    r"(?P<network>[A-Za-z0-9][A-Za-z0-9._/-]{0,127}):routine:(?P<run>[0-9a-f]{32})(?::[sv][1-9][0-9]{0,2})?\Z"
)
# From a frozen run only these outcomes are possible: nobody answered it, or someone refused or stopped it.
_FROZEN_OUTCOMES = frozenset({"denied", "stopped", "failed"})
# A worker ends its run done or failed; a completed continuation after a hold ends recovered.
_RUN_OUTCOMES = frozenset({"done", "recovered", "failed", "denied", "stopped"})
# Why recovery paused a held run's Routine; each is published on the run's notice.
PAUSE_REASONS = http_routine.PAUSE_REASONS
# Ended runs whose journal generation, continuation, and cursor Team has yet to remove. Claims stop while any
# wait, and each run ends once, so the queue never outgrows the runs a Team can hold.
MAX_DISCARDS = 2 * MAX_ROUTINES
# Unresolved incidents a Team may hold (ADR-0092); a claim reserves one for every run that could still be held.
MAX_UNRESOLVED_INCIDENTS = http_routine.MAX_UNRESOLVED_INCIDENTS
# Incident records kept in all; a released one gives way, oldest first, but an unresolved one, or a skipped one whose
# cleanup is still pending, never does.
MAX_INCIDENTS = 2 * MAX_UNRESOLVED_INCIDENTS
# Live receipts of requests that changed a Routine (ADR-0092); saturation refuses a change, never evicts a receipt.
MAX_RECEIPTS = 256
# A new or changed Routine never fires sooner than this after it is durable.
INITIAL_DELAY_SECONDS = 30
# Consecutive no-effect failures that pause a Routine (ADR-0092 section 6).
MAX_FAILURE_STREAK = 3


class RoutineStateError(ValueError):
    """A requested Routine transition is not allowed in the current state."""


@dataclass(frozen=True, slots=True)
class Routine:
    routine_id: str
    name: str
    # The user's own words that state the standing request (ADR-0092).
    quote: str
    schedule: dict[str, object]
    timezone: str
    # (assistant_id, scope pin) of each Assistant the plan uses, pinned at creation, sorted by assistant id.
    assistants: tuple[tuple[str, str], ...]
    # The canonical compiled plan document: its ordered steps, each Action's complete pin, and every input source.
    plan: dict[str, object]
    anchor: int
    next_run_at: int
    needs_reconfirm: bool = False
    deleting: bool = False
    # One continuous stretch of missed firings: when it began, how many, and how many a notice already reports.
    gap_started_at: int = 0
    missed: int = 0
    reported_missed: int = 0
    # Each authenticated change of the Routine is a new revision, which a compiled cursor binds (ADR-0092).
    revision: int = 1
    # Pausar: no dispatch until resumed; an unresolved incident still holds the Routine after that.
    paused: bool = False
    # The evidence of the request that granted this revision, bound to its receipt, revision, and plan (grant.py).
    grant: dict[str, object] | None = None
    # Consecutive runs that failed with no effect; a success resets it, and three pause the Routine (ADR-0092).
    failures: int = 0


@dataclass(frozen=True, slots=True)
class Run:
    run_id: str
    routine_id: str
    # "leased": may run a segment; "frozen": waits for a human; "held": fenced for an incident that recovery or a
    # person must resolve (ADR-0092).
    status: str
    scheduled_at: int
    lease_sha256: str = ""
    lease_key: str = ""
    lease_expires_at: int = 0
    active_seconds_left: int = ACTIVE_SECONDS
    request_kind: str = ""
    assistant_id: str = ""
    action: str = ""
    # The run's own Action journal generation, bound at its first segment, or its continuation's after a hold.
    generation: str = ""
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
    # The Routine's quoted request, so the transcript names the work even after the Routine is deleted.
    quote: str = ""


@dataclass(frozen=True, slots=True)
class Incident:
    """The compact index of one held run's incident; its evidence is sealed apart, and it never expires.

    It names its Routine even after that Routine is deleted, so it stays resolvable, but resolving it never recreates
    the Routine or dispatches a cycle.
    """

    incident_id: str
    routine_id: str
    generation: str
    created_at: int
    # The Routine revision the held run executed, which its cursor binding names (ADR-0092).
    revision: int = 1
    # "unresolved" holds its Routine; "skipped" (Pular) permits future cycles while its possible effects stay unknown
    # and its archive marker, cursor, and evidence are still being released; "released" has nothing left to release.
    status: str = "unresolved"
    # The held run's last notice version, which its continuation goes on from so Admin replaces the same row.
    notice_version: int = 0
    # The Routine's quoted request, so the incident's notices still name the work after the Routine is deleted.
    quote: str = ""
    # The step the run was held at, as its sealed cursor names it; both empty when no snapshot was sealed.
    assistant_id: str = ""
    action: str = ""


@dataclass(frozen=True, slots=True)
class TeamRoutines:
    routines: tuple[Routine, ...] = ()
    runs: tuple[Run, ...] = ()
    notices: tuple[Notice, ...] = ()
    served_at: int = 0
    starts_day: str = ""
    starts: int = 0
    # (run_id, generation) of ended runs whose journal generation, continuation, and cursor are still held.
    discards: tuple[tuple[str, str], ...] = ()
    incidents: tuple[Incident, ...] = ()
    # (receipt, expires_at) of each request that changed a Routine; a receipt outlives the Routine it changed.
    receipts: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class Claim:
    run: Run
    lease_token: str
    # The Routine revision and plan digest the run was claimed at; its segment request must name exactly these.
    revision: int = 1
    plan_digest: str = ""


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


def _admitted(value: Routine, revision: int = 1) -> Routine:
    """A copy of a new Routine revision in the closed contract; anything else is refused before it can be persisted."""
    canonical = http_routine.canonical_schedule(value.schedule)
    assistants = tuple(value.assistants)
    try:
        schedule.zone(value.timezone)
    except schedule.ScheduleError as exc:
        raise RoutineStateError("routine-invalid") from exc
    if (
        http_routine.ROUTINE_ID_RE.fullmatch(value.routine_id) is None
        or http_routine.canonical_name(value.name) is None
        or http_routine.canonical_quote(value.quote) is None
        or not routine_plan.well_formed(value.plan)
        or value.plan["timezone"] != value.timezone
        or sorted({step["assistant"] for step in value.plan["steps"]}) != [item for item, _pin in assistants]
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
        or not routine_grant.valid(value.grant, value.plan, revision)
    ):
        raise RoutineStateError("routine-invalid")
    return Routine(
        value.routine_id,
        value.name,
        value.quote,
        canonical,
        value.timezone,
        assistants,
        copy.deepcopy(value.plan),
        value.anchor,
        value.next_run_at,
        revision=revision,
        grant=copy.deepcopy(value.grant),
    )


def scheduled(value: Routine, now: int) -> Routine:
    """A defined Routine scheduled from ``now``: its first firing comes no sooner than 30 s after it is durable."""
    anchored = dataclasses.replace(value, anchor=now + INITIAL_DELAY_SECONDS)
    try:
        return dataclasses.replace(anchored, next_run_at=next_after(anchored, anchored.anchor))
    except (KeyError, TypeError, schedule.ScheduleError) as exc:
        raise RoutineStateError("routine-invalid") from exc


def definition(value: Routine) -> dict[str, object]:
    """What a created or changed notice says the Routine does: its name, its plan's safe projection, and when."""
    return {
        "name": value.name,
        "steps": routine_grant.steps(value.plan, value.grant),
        "schedule": dict(value.schedule),
        "timezone": value.timezone,
    }


def _receipt(state: TeamRoutines, receipt: str, expires_at: int, now: int) -> tuple[TeamRoutines, bool]:
    """Record one request's receipt; False when that request already changed a Routine, so it never acts twice.

    Expired receipts go first: their requests can no longer change anything. Saturation refuses the change and never
    evicts a live receipt.
    """
    if _FINGERPRINT_RE.fullmatch(receipt) is None or type(expires_at) is not int:
        raise RoutineStateError("routine-receipt-invalid")
    if expires_at <= now:
        # An expired request can no longer be told apart from a replay once its receipt is gone: it never acts.
        raise RoutineStateError("routine-request-expired")
    live = tuple(item for item in state.receipts if item[1] > now)
    if any(key == receipt for key, _expires in live):
        return dataclasses.replace(state, receipts=live), False
    if len(live) >= MAX_RECEIPTS:
        raise RoutineStateError("routine-receipts-full")
    return dataclasses.replace(state, receipts=(*live, (receipt, expires_at))), True


def create(state: TeamRoutines, value: Routine, now: int, receipt: str, expires_at: int) -> tuple[TeamRoutines, bool]:
    """Add a Routine with its created notice and the receipt of the request that made it, in one transition.

    A request whose receipt is already live changes nothing, so a resend never creates a second Routine, and a deleted
    Routine's receipt never recreates it.
    """
    state, fresh = _receipt(state, receipt, expires_at, now)
    if not fresh:
        return state, False
    state = add_routine(
        state, dataclasses.replace(value, grant=routine_grant.complete(value.grant, receipt, 1, value.plan))
    )
    admitted = routine(state, value.routine_id)
    return _notice(state, Notice(new_id(), admitted.routine_id, "", "created", now, definition(admitted))), True


def update(
    state: TeamRoutines, value: Routine, expected_revision: int, now: int, receipt: str, expires_at: int
) -> tuple[TeamRoutines, bool]:
    """Replace a Routine's definition as its next revision, with its changed notice and the request's receipt.

    Only the revision the request saw changes, never while one of its runs is live or it is being deleted; an
    authenticated change clears a scope hold and keeps a pause. Its schedule restarts from the change.
    """
    state, fresh = _receipt(state, receipt, expires_at, now)
    if not fresh:
        return state, False
    current = routine(state, value.routine_id)
    if current.deleting:
        raise RoutineStateError("routine-not-found")
    if current.revision != expected_revision:
        raise RoutineStateError("routine-revision-changed")
    if any(item.routine_id == current.routine_id for item in state.runs):
        raise RoutineStateError("routine-busy")
    revision = current.revision + 1
    granted = dataclasses.replace(value, grant=routine_grant.complete(value.grant, receipt, revision, value.plan))
    admitted = _admitted(granted, revision)
    others = tuple(item for item in state.routines if item.routine_id != current.routine_id)
    if not daily_rate_allows(others, admitted.schedule):
        raise RoutineStateError("routine-rate-limit")
    changed = dataclasses.replace(admitted, paused=current.paused)
    state = _replace_routine(state, changed)
    return _notice(state, Notice(new_id(), changed.routine_id, "", "changed", now, definition(changed))), True


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


def discarded(state: TeamRoutines, run_id: str, generation: str) -> TeamRoutines:
    """Team removed what one ended generation of a run held; a resumed run may queue more than one."""
    return dataclasses.replace(state, discards=tuple(item for item in state.discards if item != (run_id, generation)))


def _run_notice(state: TeamRoutines, value: Run, outcome: str, now: int, detail: dict[str, object]):
    """Publish the next version of the run's one notice; returns the state and the run carrying that version.

    A completed or recovered run resets its Routine's failure streak; a failed one, which had no effect, extends it, and
    the third in a row pauses the Routine.
    """
    version = value.notice_version + 1
    state = _notice(state, Notice(value.run_id, value.routine_id, value.run_id, outcome, now, detail, version))
    if outcome in {"done", "recovered", "failed"}:
        current = routine(state, value.routine_id)
        failures = current.failures + 1 if outcome == "failed" else 0
        paused = current.paused or failures >= MAX_FAILURE_STREAK
        state = _replace_routine(state, dataclasses.replace(current, failures=failures, paused=paused))
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
    notice = dataclasses.replace(notice, detail=detail, quote=notice.quote or routine(state, notice.routine_id).quote)
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
        or not incident_capacity(state)
    ):
        return None
    busy = {item.routine_id for item in state.runs} | held_routines(state)
    due = [
        item
        for item in state.routines
        if item.next_run_at <= now
        and not item.needs_reconfirm
        and not item.deleting
        and not item.paused
        and item.routine_id not in busy
    ]
    return min(due, key=lambda item: (item.next_run_at, item.routine_id)) if due else None


def next_due(state: TeamRoutines, now: int) -> int | None:
    """The earliest instant after ``now`` one of the Team's Routines becomes due to start, or None.

    A paused, held, busy, deleting, or unconfirmed Routine never wakes anything; its own resolution does.
    """
    busy = {item.routine_id for item in state.runs} | held_routines(state)
    due = [
        item.next_run_at
        for item in state.routines
        if item.next_run_at > now
        and not item.needs_reconfirm
        and not item.deleting
        and not item.paused
        and item.routine_id not in busy
    ]
    return min(due, default=None)


def held_routines(state: TeamRoutines) -> set[str]:
    """Routines an unresolved incident holds: no cycle of theirs starts, whatever else resumes them."""
    return {item.routine_id for item in state.incidents if item.status == "unresolved"}


def incident_capacity(state: TeamRoutines) -> bool:
    """Whether one more run may start, reserving the room its incident would need.

    Each run that could still be held reserves an unresolved incident's room and a record's room, so a hold never has
    to displace an unresolved incident or one whose cleanup is still pending.
    """
    unresolved = sum(item.status == "unresolved" for item in state.incidents)
    retained = sum(item.status != "released" for item in state.incidents)
    runs = len(state.runs) + 1
    return unresolved + runs <= MAX_UNRESOLVED_INCIDENTS and retained + runs <= MAX_INCIDENTS


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
    return state, Claim(leased, token, due.revision, routine_grant.plan_digest(due.plan))


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


def generation_for(network_id: str, run_id: str, suffix: str = "") -> str:
    """A run's journal generation in the Team's network.

    A continuation (``s<n>``) or verification (``v<n>``) after a hold gets its own, since the held generation is
    archived (ADR-0092).
    """
    if suffix and _SUFFIX_RE.fullmatch(suffix) is None:
        raise RoutineStateError("generation-invalid")
    return f"{network_id}:routine:{run_id}" + (f":{suffix}" if suffix else "")


def network_of(generation: object, run_id: str) -> str | None:
    """The Team network a run's generation, or one of its continuation or verification generations, belongs to."""
    match = _GENERATION_RE.fullmatch(generation) if isinstance(generation, str) else None
    return match["network"] if match is not None and match["run"] == run_id else None


def bind_generation(state: TeamRoutines, run_id: str, lease: Lease, now: int, network_id: str) -> TeamRoutines:
    """Bind the run's own journal generation, derived from the Team's trusted network id, once; it never changes.

    A continuation after a hold already names its own generation, which must belong to the same network.
    """
    value = _live(state, run_id, lease, now)
    generation = value.generation or generation_for(network_id, run_id)
    if _SAFE_ID_RE.fullmatch(generation) is None or network_of(generation, run_id) != network_id:
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


def finish(
    state: TeamRoutines, run_id: str, lease: Lease, now: int, outcome: str, detail: dict[str, object]
) -> TeamRoutines:
    """A worker ends its leased run with a durable notice; only its live lease may."""
    value = _live(state, run_id, lease, now)
    if outcome not in _RUN_OUTCOMES:
        raise RoutineStateError("invalid-outcome")
    state, _value = _run_notice(state, value, outcome, now, detail)
    return _without_run(state, run_id)


def end(
    state: TeamRoutines,
    run_id: str,
    now: int,
    outcome: str,
    detail: dict[str, object],
    *,
    status: str = "",
) -> TeamRoutines:
    """Team itself ends a run without its lease: a human Stop or answer, an expired lease or deadline, or recovery.

    A leased run ends stopped or failed; a frozen run ends denied, stopped, or failed. A run that may have acted is
    held for recovery instead (``fence``). With ``status``, the run must still be in it, so an ending decided on an
    earlier read never lands on a run that changed since.
    """
    value = run(state, run_id)
    if status and value.status != status:
        raise RoutineStateError("run-changed")
    allowed = {"leased": frozenset({"stopped", "failed"}), "frozen": _FROZEN_OUTCOMES}.get(value.status, frozenset())
    if outcome not in allowed:
        raise RoutineStateError("invalid-outcome")
    state, _value = _run_notice(state, value, outcome, now, detail)
    return _without_run(state, run_id)


def fence(state: TeamRoutines, run_id: str, lease: Lease, now: int) -> TeamRoutines:
    """Stop the live lease of a run that must be held (ADR-0092): no worker may advance it, nothing ends it yet."""
    value = _live(state, run_id, lease, now)
    if not value.generation:
        raise RoutineStateError("generation-invalid")
    held = dataclasses.replace(value, status="held", lease_sha256="", lease_key="", lease_expires_at=0)
    return _replace_run(state, held)


def hold_recovered(state: TeamRoutines, run_id: str, lease_sha256: str) -> TeamRoutines:
    """Team's watchdog holds a leased run nothing runs any more whose durable state shows it may have acted.

    Only the exact lease the watchdog read is fenced, so a run that ended or was claimed again meanwhile is untouched.
    """
    value = _leased(state, run_id)
    if not secrets.compare_digest(value.lease_sha256, lease_sha256):
        raise RoutineStateError("run-changed")
    if not value.generation:
        raise RoutineStateError("generation-invalid")
    return _replace_run(
        state, dataclasses.replace(value, status="held", lease_sha256="", lease_key="", lease_expires_at=0)
    )


def complete_recovered(state: TeamRoutines, run_id: str, lease_sha256: str, now: int) -> TeamRoutines:
    """Team's watchdog ends a leased run whose sealed cursor completed every step before its end was recorded."""
    value = _leased(state, run_id)
    if not secrets.compare_digest(value.lease_sha256, lease_sha256):
        raise RoutineStateError("run-changed")
    actions = plan_actions(routine(state, value.routine_id).plan)
    state, _value = _run_notice(state, value, completed(value), now, {"actions": actions})
    return _without_run(state, run_id)


def plan_actions(plan: dict[str, object]) -> list[list[str]]:
    """The ordered Assistant Actions of a plan's steps, which a completed run's notice names; never their data."""
    return [[step["assistant"], step["action"]] for step in plan["steps"]]


def completed(value: Run) -> str:
    """How a run that completed every step ends: recovered when a continuation after a hold completed it."""
    first = generation_for(network_of(value.generation, value.run_id), value.run_id)
    return "done" if value.generation == first else "recovered"


def settle_hold(
    state: TeamRoutines, run_id: str, now: int, revision: int | None = None, step: tuple[str, str] = ("", "")
) -> TeamRoutines:
    """A held run's incident is durable and its batch archived: index the incident and end the run in one write.

    The run's live state is queued for removal like any ended run's; its archived journal marker stays with the
    incident. A claim reserved this incident's room, so it never displaces an unresolved one. ``revision`` is the one
    the run's recovery snapshot binds; without one, the run executed the Routine's current revision. ``step`` is the
    Assistant Action its sealed cursor stopped at, which the run's held notice names.
    """
    value = run(state, run_id)
    if value.status != "held":
        raise RoutineStateError("run-not-held")
    current = routine(state, value.routine_id)
    executed = current.revision if revision is None else revision
    if type(executed) is not int or executed < 1:
        raise RoutineStateError("incident-invalid")
    state, value = _run_notice(state, value, "held", now, _step_detail(step))
    incident = Incident(
        run_id,
        value.routine_id,
        value.generation,
        now,
        executed,
        notice_version=value.notice_version,
        quote=current.quote,
        assistant_id=step[0],
        action=step[1],
    )
    kept = list(state.incidents)
    while len(kept) >= MAX_INCIDENTS:
        released = next((item for item in kept if item.status == "released"), None)
        if released is None:
            raise RoutineStateError("incident-limit")
        kept.remove(released)
    return _without_run(dataclasses.replace(state, incidents=(*kept, incident)), run_id)


def incident(state: TeamRoutines, incident_id: str) -> Incident:
    for item in state.incidents:
        if item.incident_id == incident_id:
            return item
    raise RoutineStateError("incident-not-found")


def reopen_incident(state: TeamRoutines, incident_id: str, now: int, generation: str) -> tuple[TeamRoutines, str]:
    """Resume a held run after Team-admitted evidence, as a continuation in its own ``generation`` (ADR-0092).

    Only the revision the run executed, of a Routine still listed, not paused, and with no other run, may continue.
    The incident gives way in the same write, and its archived generation is queued for removal; the continuation runs
    under a fresh internal lease that no machine assertion knows, and goes on with the run's notice.
    """
    value = incident(state, incident_id)
    if value.status != "unresolved":
        raise RoutineStateError("incident-not-unresolved")
    current = routine(state, value.routine_id)
    if current.deleting or current.paused or current.revision != value.revision:
        raise RoutineStateError("routine-not-resumable")
    if any(item.routine_id == value.routine_id for item in state.runs) or network_of(generation, incident_id) is None:
        raise RoutineStateError("routine-busy")
    token = secrets.token_urlsafe(32)
    resumed = Run(
        incident_id,
        value.routine_id,
        "leased",
        value.created_at,
        lease_sha256=lease_sha256(token),
        lease_key=HUMAN_LEASE,
        lease_expires_at=now + LEASE_SECONDS,
        generation=generation,
        notice_version=value.notice_version,
    )
    return (
        dataclasses.replace(
            state,
            incidents=tuple(item for item in state.incidents if item.incident_id != incident_id),
            runs=(*state.runs, resumed),
            # The hold queued its generation already unless that removal has run since.
            discards=tuple(dict.fromkeys((*state.discards, (incident_id, value.generation)))),
        ),
        token,
    )


def _step_detail(step: tuple[str, str]) -> dict[str, object]:
    assistant_id, action = step
    return {"assistant_id": assistant_id or None, "action": action or None}


def _incident_notice(
    state: TeamRoutines, value: Incident, outcome: str, now: int, detail: dict[str, object]
) -> tuple[TeamRoutines, Incident]:
    """Publish the next version of the held run's one notice, which outlives a deleted Routine with the incident."""
    version = value.notice_version + 1
    notice = Notice(value.incident_id, value.routine_id, value.incident_id, outcome, now, detail, version, value.quote)
    return _notice(state, notice), dataclasses.replace(value, notice_version=version)


def _replace_incident(state: TeamRoutines, updated: Incident) -> TeamRoutines:
    return dataclasses.replace(
        state, incidents=tuple(updated if item.incident_id == updated.incident_id else item for item in state.incidents)
    )


def skip_incident(state: TeamRoutines, incident_id: str, now: int) -> TeamRoutines:
    """Pular: abandon the rest of the held run and permit future cycles; its possible effects stay unresolved.

    Its notice says the person skipped it, which is distinct from the Routine's own missed-schedule skip.
    """
    value = incident(state, incident_id)
    if value.status != "unresolved":
        raise RoutineStateError("incident-not-unresolved")
    step = _step_detail((value.assistant_id, value.action))
    state, value = _incident_notice(state, value, "user-skipped", now, step)
    return _replace_incident(state, dataclasses.replace(value, status="skipped"))


def pause_incident(state: TeamRoutines, incident_id: str, now: int, reason: str) -> TeamRoutines:
    """Recovery or a person paused the Routine an unresolved incident holds, and the run's notice says why."""
    value = incident(state, incident_id)
    if value.status != "unresolved" or reason not in PAUSE_REASONS:
        raise RoutineStateError("incident-not-unresolved")
    state = set_paused(state, value.routine_id, True)
    detail = {**_step_detail((value.assistant_id, value.action)), "reason": reason}
    state, value = _incident_notice(state, value, "paused", now, detail)
    return _replace_incident(state, value)


def release_incident(state: TeamRoutines, incident_id: str) -> TeamRoutines:
    """A skipped incident's archive marker, cursor, and evidence are gone: only now may its record give way."""
    value = incident(state, incident_id)
    if value.status == "released":
        return state
    if value.status != "skipped":
        raise RoutineStateError("incident-not-skipped")
    released = dataclasses.replace(value, status="released")
    return dataclasses.replace(
        state, incidents=tuple(released if item.incident_id == incident_id else item for item in state.incidents)
    )


def set_paused(state: TeamRoutines, routine_id: str, paused: bool) -> TeamRoutines:
    """Pausar, or resume: resuming never bypasses an unresolved incident, which still holds the Routine.

    A resume starts a fresh failure streak, so the person's decision is not undone by the failures before it.
    """
    value = routine(state, routine_id)
    if value.deleting:
        raise RoutineStateError("routine-not-found")
    return _replace_routine(state, dataclasses.replace(value, paused=paused, failures=value.failures if paused else 0))


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

    A held run settles into its incident, which outlives the Routine (ADR-0092).
    """
    value = routine(state, routine_id)
    runs = tuple(item for item in state.runs if item.routine_id == routine_id)
    return _replace_routine(state, dataclasses.replace(value, deleting=True)), runs


def complete_delete(state: TeamRoutines, routine_id: str) -> TeamRoutines:
    """Remove a deleting Routine once none of its runs remains; its undelivered notices stay."""
    value = routine(state, routine_id)
    if not value.deleting or any(item.routine_id == routine_id for item in state.runs):
        raise RoutineStateError("routine-busy")
    return dataclasses.replace(state, routines=tuple(item for item in state.routines if item.routine_id != routine_id))


def mark_scope_changed(state: TeamRoutines, routine_id: str, now: int, assistants: list[str]) -> TeamRoutines:
    """The Routine's Assistants no longer match its pins: no claim until an authenticated update."""
    value = routine(state, routine_id)
    state = _notice(state, Notice(new_id(), routine_id, "", "scope-changed", now, {"assistants": assistants}))
    return _replace_routine(state, dataclasses.replace(value, needs_reconfirm=True))
