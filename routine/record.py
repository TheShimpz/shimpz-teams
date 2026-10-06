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

from action import journal as action_journal
from protocol.http.v1 import identifiers as http_identifiers
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine as http_routine
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import schedule
from routine import starts as routine_starts

MAX_ROUTINES = http_routine.MAX_ROUTINES
MAX_FROZEN_RUNS = 8
MAX_UNDELIVERED_NOTICES = 32
# A claimed run's lease until its segment starts; the segment then extends it over the run's active time.
LEASE_SECONDS = routine_plan.START_LEASE_SECONDS
# The most active time any run may have left; each revision's own is ``routine_plan.active_seconds`` of its steps.
ACTIVE_SECONDS = routine_plan.MAX_ACTIVE_SECONDS
MAX_GRACE_SECONDS = 12 * 3600
# Bounds the work of counting firings missed during a long outage; the count is reported, never replayed.
MAX_COUNTED_MISSES = 24 * 400
HUMAN_LEASE = "human"
_PERIOD_SECONDS = {"daily": 86_400, "weekly": 7 * 86_400, "monthly": 28 * 86_400}
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
# Undelivered Routine outcomes beside every run's: a created or changed notice and, once, a deleted one per Routine.
MAX_ROUTINE_NOTICES = 2 * MAX_ROUTINES
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
    schedule: dict[str, object]
    timezone: str
    # (assistant_id, scope pin) of each Assistant the plan uses, pinned at creation, sorted by assistant id.
    assistants: tuple[tuple[str, str], ...]
    # The canonical recorded plan document: its ordered steps, each Action's complete pin, and every input source.
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
    # Owed a rehearsal before it may run by schedule (ADR-0101 section 8); a pause never clears it.
    rehearsal: bool = False
    # The person's authenticated confirmation of the card that defined this revision (ADR-0101 section 5.3):
    # {proposal_id, proposal_digest, principal, incarnation, confirmed_at}.
    confirmation: dict[str, object] | None = None
    # Every Action the Routine may call at its pin: {assistant, action, pin, read_only, stored_inputs}, sorted.
    permitted: tuple[dict[str, object], ...] = ()
    # Grows with every permission a person adds while a run waits (ADR-0101 section 6.7).
    permissions_revision: int = 0
    # A decision's sealed base prompt by digest, its frozen model {provider, model, effort}, and its allowance.
    prompt: str | None = None
    model: dict[str, str] | None = None
    allowance: int = 0
    # The sealed input of the last decided run, {id, digest}, which a ``changes`` decision compares with.
    baseline: dict[str, str] | None = None
    # The run that rehearsed the current revision and permissions, {run_id, revision, permissions_revision}.
    rehearsed: dict[str, object] | None = None
    # Consecutive runs that failed with no effect; a success resets it, and three pause the Routine (ADR-0092).
    failures: int = 0
    # A continuous Routine's healthy runs rolled up into the notice of the minute starting at ``rollup_minute``.
    rollup_minute: int = 0
    rollup_runs: int = 0
    # The summed usage of that minute's runs, which the rollup notice carries (ADR-0101 section 10).
    rollup_usage: dict[str, object] | None = None
    # A person's Rodar: when they asked for one fresh run of a fixed schedule, kept until a claim starts it, so neither
    # a cap nor a late scheduler turns it into a missed firing; 0 when none is pending. It never moves the cadence.
    run_requested: int = 0
    # The keyed digest of the result the last queued notice showed, which a ``changes`` Routine compares each completed
    # run's result with; empty until one is shown, and again after every change of the Routine.
    output_digest: str = ""


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
    active_seconds_left: int = routine_plan.SHORT_ACTIVE_SECONDS
    request_kind: str = ""
    assistant_id: str = ""
    action: str = ""
    # The run's own Action journal generation, bound at its first segment, or its continuation's after a hold.
    generation: str = ""
    # A run has one notice, keyed by its id; each freeze and its end update it, so Admin replaces one transcript row.
    notice_version: int = 0
    # The human requests the logical run has answered, through every freeze, hold, and continuation; never reset.
    requests_used: int = 0
    # A frozen run's call by position ({"phase", "step"|"call"}) and its plan's step count; None and 0 otherwise.
    position: dict[str, object] | None = None
    steps: int = 0
    # The run's active time and model usage so far, through every freeze, hold, and continuation (ADR-0101 §10).
    usage: dict[str, object] = dataclasses.field(default_factory=lambda: {"duration_ms": 0, "models": []})
    # A rehearsal of a Routine that may change anything (ADR-0101 section 8).
    rehearsal: bool = False
    # The run lost the protection of its secret values, so nothing it produced since may be shown (section 6.2).
    protection_lost: bool = False


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
    # The Routine's name when this version was written, so the transcript names the work after a rename or deletion.
    name: str = ""
    # The run's usage, or a rollup's summed usage; None for other Routine outcomes.
    usage: dict[str, object] | None = None
    protection_lost: bool = False


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
    # The Routine's name when the run was held, so the incident's notices still name the work after it is deleted.
    name: str = ""
    # The call the run was held at, as its sealed cursor names it; both empty when no snapshot was sealed.
    assistant_id: str = ""
    action: str = ""
    # The run's active time left when it was held, which its continuation goes on from; a hold never refills it.
    active_seconds_left: int = routine_plan.SHORT_ACTIVE_SECONDS
    # The held call's position and its plan's step count; None and 0 when none was sealed.
    position: dict[str, object] | None = None
    steps: int = 0
    # The held run's answered human requests, usage, rehearsal, and protection, which its continuation goes on from.
    requests_used: int = 0
    usage: dict[str, object] = dataclasses.field(default_factory=lambda: {"duration_ms": 0, "models": []})
    rehearsal: bool = False
    protection_lost: bool = False


@dataclass(frozen=True, slots=True)
class TeamRoutines:
    routines: tuple[Routine, ...] = ()
    runs: tuple[Run, ...] = ()
    notices: tuple[Notice, ...] = ()
    served_at: int = 0
    # (routine_id, instant) of every start in the last rolling 24 hours (ADR-0092 section 9).
    starts: routine_starts.Starts = ()
    # (run_id, generation) of ended runs whose journal generation, continuation, and cursor are still held.
    discards: tuple[tuple[str, str], ...] = ()
    incidents: tuple[Incident, ...] = ()


@dataclass(frozen=True, slots=True)
class Claim:
    run: Run
    lease_token: str
    # The Routine revision and plan digest the run was claimed at; its segment request must name exactly these.
    revision: int = 1
    plan_digest: str = ""
    mode: str = "scheduled"
    # The run's active time, from its revision's units; Admin bounds its worker's wait by it.
    active_seconds: int = routine_plan.SHORT_ACTIVE_SECONDS


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


def continuous(routine_value: Routine) -> bool:
    return routine_value.schedule["kind"] == "continuous"


def grace_seconds(routine_value: Routine) -> int:
    """How late a run may still start: one period, and at most 12 hours."""
    kind = routine_value.schedule["kind"]
    period = routine_value.schedule["every"] * 3600 if kind == "hourly" else _PERIOD_SECONDS[kind]
    return min(period, MAX_GRACE_SECONDS)


_HEX32_RE = re.compile(r"[0-9a-f]{32}\Z")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _confirmed(value: object) -> bool:
    """The authenticated confirmation of the card that defined a revision, with the Team incarnation it was in."""
    return (
        isinstance(value, dict)
        and set(value) == {"proposal_id", "proposal_digest", "principal", "incarnation", "confirmed_at"}
        and _matches(value["proposal_id"], _HEX32_RE)
        and _matches(value["proposal_digest"], _DIGEST_RE)
        and _matches(value["principal"], _HEX32_RE)
        and _matches(value["incarnation"], _HEX64_RE)
        and type(value["confirmed_at"]) is int
        and value["confirmed_at"] > 0
    )


def _matches(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _permitted(value: Routine) -> bool:
    """Every permitted Action once, sorted, at a complete pin, covering every step's Action at the step's own pin."""
    entries = value.permitted
    if not isinstance(entries, tuple) or len(entries) > http_routine.MAX_PERMITTED:
        return False
    fields = {"assistant", "action", "pin", "read_only", "stored_inputs"}
    if not all(
        isinstance(item, dict)
        and set(item) == fields
        and http_identifiers.canonical_assistant_id(item["assistant"]) is not None
        and http_identifiers.canonical_action_id(item["action"]) is not None
        and _matches(item["pin"], routine_plan.PIN_RE)
        and type(item["read_only"]) is bool
        and isinstance(item["stored_inputs"], list)
        and len(item["stored_inputs"]) <= http_routine.MAX_STEP_STORED_INPUTS
        and all(http_identifiers.canonical_identifier(name) is not None for name in item["stored_inputs"])
        and item["stored_inputs"] == sorted(set(item["stored_inputs"]))
        for item in entries
    ):
        return False
    pins = {(item["assistant"], item["action"]): item["pin"] for item in entries}
    return (
        list(pins) == sorted(pins)
        and len(pins) == len(entries)
        and all(pins.get((step["assistant"], step["action"])) == step["pin"] for step in value.plan["steps"])
    )


def _decision_scope(value: Routine) -> bool:
    """Only a decision has a base prompt, a model, and an allowance, which its steps leave room for."""
    decide = value.plan["output"]["mode"] == "decide"
    return (
        (_matches(value.prompt, _DIGEST_RE) if decide else value.prompt is None)
        and (
            value.model is not None and http_routine.canonical_model(value.model) == value.model
            if decide
            else value.model is None
        )
        and type(value.allowance) is int
        and (1 <= value.allowance <= http_routine.MAX_ALLOWANCE if decide else value.allowance == 0)
        and routine_definition.run_units(value) <= routine_plan.MAX_STEPS
        and (value.baseline is None or (decide and _baseline(value.baseline)))
    )


def _baseline(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "digest"}
        and _matches(value["id"], _HEX32_RE)
        and _matches(value["digest"], _HEX64_RE)
    )


def _rehearsed(value: Routine) -> bool:
    """None, or the run that rehearsed exactly this revision and these permissions."""
    rehearsed = value.rehearsed
    return rehearsed is None or (
        isinstance(rehearsed, dict)
        and set(rehearsed) == {"run_id", "revision", "permissions_revision"}
        and _matches(rehearsed["run_id"], _HEX32_RE)
        and rehearsed["revision"] == value.revision
        and rehearsed["permissions_revision"] == value.permissions_revision
    )


def _assistants(value: Routine) -> bool:
    """Exactly the Assistants of the plan's steps and permitted Actions, each with its scope pin, sorted."""
    assistants = tuple(value.assistants)
    used = sorted({item["assistant"] for item in value.permitted})
    return (
        [item for item, _pin in assistants] == used
        and len(assistants) <= http_routine.MAX_NOTICE_ASSISTANTS
        and list(assistants) == sorted(set(assistants))
        and all(
            http_identifiers.canonical_assistant_id(assistant) is not None
            and http_payload.SOURCE_DIGEST_RE.fullmatch(digest)
            for assistant, digest in assistants
        )
    )


def definition_valid(value: Routine) -> bool:
    """Whether a Routine's definition is in its closed contract: identity, plan, scope, confirmation, and schedule."""
    canonical = http_routine.canonical_schedule(value.schedule)
    try:
        schedule.zone(value.timezone)
    except schedule.ScheduleError:
        return False
    return (
        http_routine.ROUTINE_ID_RE.fullmatch(value.routine_id) is not None
        and http_routine.canonical_name(value.name) is not None
        and routine_plan.well_formed(value.plan)
        and value.plan["timezone"] == value.timezone
        and canonical is not None
        and _permitted(value)
        and _assistants(value)
        and _confirmed(value.confirmation)
        and _decision_scope(value)
        and type(value.rehearsal) is bool
        and type(value.paused) is bool
        and type(value.permissions_revision) is int
        and value.permissions_revision >= 0
        and type(value.revision) is int
        and 1 <= value.revision < 2**31
        and _rehearsed(value)
        and type(value.anchor) is int
    )


def _admitted(value: Routine, revision: int = 1) -> Routine:
    """A copy of a new Routine revision in the closed contract; anything else is refused before it can be persisted.

    A new revision is scheduled from its anchor; later claims and sweeps move its next firing on.
    """
    canonical = http_routine.canonical_schedule(value.schedule)
    if not definition_valid(dataclasses.replace(value, revision=revision, rehearsed=None)) or (
        value.next_run_at != next_after(dataclasses.replace(value, schedule=canonical), value.anchor)
    ):
        raise RoutineStateError("routine-invalid")
    # Every page of what a Supervisor inspects is deliverable, and the definition fits its own budget (scale).
    if not routine_definition.fits(value):
        raise RoutineStateError("routine-too-large")
    return dataclasses.replace(
        value,
        schedule=http_routine.canonical_schedule(value.schedule),
        plan=copy.deepcopy(value.plan),
        permitted=tuple(copy.deepcopy(item) for item in value.permitted),
        revision=revision,
        rehearsed=None,
        needs_reconfirm=False,
        deleting=False,
        gap_started_at=0,
        missed=0,
        reported_missed=0,
        failures=0,
        rollup_minute=0,
        rollup_runs=0,
        rollup_usage=None,
        run_requested=0,
        output_digest="",
    )


def scheduled(value: Routine, now: int) -> Routine:
    """A defined Routine scheduled from ``now``: its first firing comes no sooner than 30 s after it is durable."""
    anchored = dataclasses.replace(value, anchor=now + INITIAL_DELAY_SECONDS)
    try:
        return dataclasses.replace(anchored, next_run_at=next_after(anchored, anchored.anchor))
    except (KeyError, TypeError, schedule.ScheduleError) as exc:
        raise RoutineStateError("routine-invalid") from exc


def _budgets(others: tuple[Routine, ...], admitted: Routine) -> None:
    refused = routine_definition.over_budget(others, admitted)
    if refused is not None:
        raise RoutineStateError(refused)


def change_room(state: TeamRoutines, notices: int) -> str | None:
    """Why one more Routine change adding ``notices`` new notices cannot be admitted now; None when it fits.

    It is checked before a card is shown; the confirmation's own write checks again with what only it can tell.
    """
    if len(state.notices) + notices > MAX_UNDELIVERED_NOTICES + MAX_ROUTINE_NOTICES:
        return "notices-full"
    return None


def create(state: TeamRoutines, value: Routine, now: int) -> TeamRoutines:
    """Add a confirmed Routine with its created notice in one transition; its minted id never exists twice."""
    state = add_routine(state, value)
    admitted = routine(state, value.routine_id)
    detail = routine_definition.detail(admitted)
    return _notice(state, Notice(new_id(), admitted.routine_id, "", "created", now, detail))


def update(state: TeamRoutines, value: Routine, expected_revision: int, now: int) -> TeamRoutines:
    """Replace a Routine's definition as its next revision, with its changed notice.

    Only the revision the card saw changes, never while a run is live or it is being deleted; it clears a scope hold,
    keeps a pause and the minute rollup, and restarts its schedule.
    """
    current = routine(state, value.routine_id)
    if current.deleting:
        raise RoutineStateError("routine-not-found")
    if current.revision != expected_revision:
        raise RoutineStateError("routine-revision-changed")
    if any(item.routine_id == current.routine_id for item in state.runs):
        raise RoutineStateError("routine-busy")
    admitted = _admitted(value, current.revision + 1)
    _budgets(tuple(item for item in state.routines if item.routine_id != current.routine_id), admitted)
    # The minute rollup outlives a change: its notice id is the Routine's and the minute's, so a count restarted at 1
    # would reuse a delivered version of the same notice.
    changed = dataclasses.replace(
        admitted,
        paused=current.paused,
        rollup_minute=current.rollup_minute,
        rollup_runs=current.rollup_runs,
        rollup_usage=current.rollup_usage,
    )
    state = _replace_routine(state, changed)
    return _notice(state, Notice(new_id(), changed.routine_id, "", "changed", now, routine_definition.detail(changed)))


def add_routine(state: TeamRoutines, value: Routine) -> TeamRoutines:
    admitted = _admitted(value)
    if any(item.routine_id == admitted.routine_id for item in state.routines):
        raise RoutineStateError("routine-exists")
    if len(state.routines) >= MAX_ROUTINES:
        raise RoutineStateError("routine-limit")
    _budgets(state.routines, admitted)
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


def _without_run(state: TeamRoutines, run_id: str, now: int) -> TeamRoutines:
    """Remove an ended run and, in the same write, queue the removal of everything it held.

    A continuous Routine's next run becomes due its gap after this one ended, so its runs never overlap.
    """
    value = run(state, run_id)
    state = dataclasses.replace(
        state,
        runs=tuple(item for item in state.runs if item.run_id != run_id),
        discards=(*state.discards, (run_id, value.generation)),
    )
    return rebase_continuous(state, value.routine_id, now)


def rebase_continuous(state: TeamRoutines, routine_id: str, now: int) -> TeamRoutines:
    """A continuous Routine's run ended at ``now``, so its next one is due its gap later; any other is unchanged."""
    current = next((item for item in state.routines if item.routine_id == routine_id), None)
    if current is None or not continuous(current):
        return state
    return _replace_routine(state, dataclasses.replace(current, next_run_at=now + current.schedule["gap"]))


def discarded(state: TeamRoutines, run_id: str, generation: str) -> TeamRoutines:
    """Team removed what one ended generation of a run held; a resumed run may queue more than one."""
    return dataclasses.replace(state, discards=tuple(item for item in state.discards if item != (run_id, generation)))


def _run_notice(state: TeamRoutines, value: Run, outcome: str, now: int, detail: dict[str, object]):
    """Publish the next version of the run's one notice; returns the state and the run carrying that version.

    A completed or recovered run resets its Routine's failure streak; a failed one extends it, the third pausing it.
    """
    version = value.notice_version + 1
    notice = Notice(value.run_id, value.routine_id, value.run_id, outcome, now, detail, version)
    usage = copy.deepcopy(value.usage)
    state = _notice(state, dataclasses.replace(notice, usage=usage, protection_lost=value.protection_lost))
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

    Claims and new skip reports stop at MAX_UNDELIVERED_NOTICES, so every run in flight's outcome always fits above.
    """
    detail = http_routine.canonical_notice_detail(notice.outcome, notice.detail)
    if detail is None:
        raise RoutineStateError("notice-invalid")
    # Each version names the Routine as it is now, so a later rename never retitles an earlier version.
    notice = dataclasses.replace(notice, detail=detail, name=notice.name or routine(state, notice.routine_id).name)
    if type(notice.version) is not int or notice.version < 1:
        raise RoutineStateError("notice-invalid")
    kept = tuple(item for item in state.notices if item.notice_id != notice.notice_id)
    if len(kept) >= MAX_UNDELIVERED_NOTICES + MAX_ROUTINE_NOTICES:
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
        if continuous(item):
            # A continuous Routine has no backlog to skip: it starts again once it may.
            continue
        cutoff = now - grace_seconds(item)
        if item.next_run_at < cutoff:
            count, next_run_at = _count_before(item, item.next_run_at, cutoff)
            item = _miss(item, item.next_run_at, count, next_run_at)
            state = _replace_routine(state, item)
        state = _report_gap(state, item, now)
    return state


def free_at(state: TeamRoutines, routine_value: Routine, now: int) -> int:
    """The earliest instant a due Routine may start under the Team ceiling and, for a continuous one, its own cap.

    A scheduled Routine's own firings already bound its starts, and a late start never delays the next firing.
    """
    cap = http_routine.daily_cap(routine_value.schedule) if continuous(routine_value) else None
    units = routine_definition.run_units(routine_value)
    return routine_starts.free_at(state.starts, routine_value.routine_id, cap, now, units)


def _due_at(routine_value: Routine) -> int:
    """When the Routine is next due: its next firing, or sooner a person's pending Rodar."""
    if routine_value.run_requested:
        return min(routine_value.next_run_at, routine_value.run_requested)
    return routine_value.next_run_at


def _ready(state: TeamRoutines, busy: set[str], long: bool = True) -> list[Routine]:
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


def _segment_leased(state: TeamRoutines) -> bool:
    """Whether one of the Team's runs is leased to drive a segment; frozen and held runs hold no slot."""
    return any(item.status == "leased" for item in state.runs)


def _backpressured(state: TeamRoutines) -> bool:
    """Whether the Team must catch up before any run starts: undelivered notices, cleanup, or incident room."""
    return (
        undelivered(state) >= MAX_UNDELIVERED_NOTICES
        or len(state.discards) >= MAX_ROUTINES
        or not incident_capacity(state)
    )


def claimable(state: TeamRoutines, now: int, long: bool = True) -> Routine | None:
    """The Team's oldest due Routine that may start now, or None; the caller has already swept.

    A Team leases one run at a time (ADR-0092 section 9); without ``long``, only a short Routine may start.
    """
    if _backpressured(state) or _segment_leased(state):
        return None
    busy = {item.routine_id for item in state.runs} | held_routines(state)
    due = [item for item in _ready(state, busy, long) if _due_at(item) <= now and free_at(state, item, now) <= now]
    return min(due, key=lambda item: (_due_at(item), item.routine_id)) if due else None


def next_due(state: TeamRoutines, now: int) -> int | None:
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


def held_routines(state: TeamRoutines) -> set[str]:
    """Routines an unresolved incident holds: no cycle of theirs starts, whatever else resumes them."""
    return {item.routine_id for item in state.incidents if item.status == "unresolved"}


def incident_capacity(state: TeamRoutines) -> bool:
    """Whether one more run may start, reserving the room its incident would need.

    Each run that could be held reserves an unresolved incident's and a record's room, so a hold never displaces one.
    """
    unresolved = sum(item.status == "unresolved" for item in state.incidents)
    retained = sum(item.status != "released" for item in state.incidents)
    runs = len(state.runs) + 1
    return unresolved + runs <= MAX_UNRESOLVED_INCIDENTS and retained + runs <= MAX_INCIDENTS


def claim(state: TeamRoutines, now: int, key_fingerprint: str, long: bool = True) -> tuple[TeamRoutines, Claim | None]:
    """Lease one run of the Team's oldest claimable Routine, rechecked on this exact state.

    Its next firing moves past ``now`` so it can never be claimed twice; only this late firing is made up, and any
    others since it join the Routine's gap, which ends here.
    """
    if http_payload.SHA256_RE.fullmatch(key_fingerprint) is None:
        raise RoutineStateError("routine-key-invalid")
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
    if continuous(due):
        # Provisional: the run's end sets the next one its gap after it, and nothing starts while it runs.
        due = dataclasses.replace(due, next_run_at=next_after(due, now))
    else:
        following = next_after(due, scheduled_at)
        extra, next_run_at = _count_before(due, following, now + 1)
        due = _miss(due, following, extra, next_run_at) if extra else dataclasses.replace(due, next_run_at=next_run_at)
    state = _report_gap(_replace_routine(state, due), due, now, in_flight=True)
    ended = dataclasses.replace(routine(state, due.routine_id), gap_started_at=0, missed=0, reported_missed=0)
    return _lease(state, ended, scheduled_at, now, key_fingerprint)


def _lease(
    state: TeamRoutines, due: Routine, scheduled_at: int, now: int, key_fingerprint: str
) -> tuple[TeamRoutines, Claim]:
    """Start one run of the claimed Routine under every start cap, its lease covering only its segment's start."""
    token = secrets.token_urlsafe(32)
    units = routine_definition.run_units(due)
    leased = Run(
        run_id=new_id(),
        routine_id=due.routine_id,
        status="leased",
        scheduled_at=scheduled_at,
        lease_sha256=lease_sha256(token),
        lease_key=key_fingerprint,
        lease_expires_at=now + LEASE_SECONDS,
        active_seconds_left=routine_plan.active_seconds(units),
    )
    state = dataclasses.replace(
        _replace_routine(state, due),
        runs=(*state.runs, leased),
        served_at=now,
        starts=routine_starts.started(state.starts, due.routine_id, now, units),
    )
    mode = http_routine.run_mode(due.schedule)
    digest = routine_definition.plan_digest(due.plan)
    return state, Claim(leased, token, due.revision, digest, mode, leased.active_seconds_left)


def lease_until(value: Run, now: int) -> int:
    """A running segment's lease: the run's active time left, and a margin, so a deadline cuts it before the lease."""
    return now + max(value.active_seconds_left, 0) + routine_plan.LEASE_MARGIN_SECONDS


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

    A continuation (``s<n>``) or verification (``v<n>``) after a hold gets its own: the held one is archived.
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
    if action_journal.SAFE_ID_RE.fullmatch(generation) is None or network_of(generation, run_id) != network_id:
        raise RoutineStateError("generation-invalid")
    # The claimed run's segment has started: its lease now covers the run's active time left, once; a later bind of the
    # same run (a retried request) never renews it.
    extended = value.lease_expires_at if value.generation else max(value.lease_expires_at, lease_until(value, now))
    return _replace_run(state, dataclasses.replace(value, generation=generation, lease_expires_at=extended))


def spend(state: TeamRoutines, run_id: str, lease: Lease, now: int, elapsed: tuple[int, int]) -> TeamRoutines:
    """Charge one segment's active time to the run: whole seconds against its budget, milliseconds to its usage."""
    seconds, milliseconds = elapsed
    if type(seconds) is not int or seconds < 0 or type(milliseconds) is not int or milliseconds < 0:
        raise RoutineStateError("invalid-duration")
    value = _live(state, run_id, lease, now)
    usage = {**value.usage, "duration_ms": min(value.usage["duration_ms"] + milliseconds, MAX_USAGE_MS)}
    return _replace_run(
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


def used(state: TeamRoutines, run_id: str, models: list[dict[str, object]]) -> TeamRoutines:
    """Add the tokens a run's model calls reported, its recovery's included, to its usage (ADR-0082, ADR-0101)."""
    value = run(state, run_id)
    usage = joined_usage(value.usage, {"duration_ms": 0, "models": models})
    if http_routine.canonical_run_usage(usage) != usage:
        raise RoutineStateError("usage-invalid")
    return _replace_run(state, dataclasses.replace(value, usage=usage))


def lose_protection(state: TeamRoutines, run_id: str) -> TeamRoutines:
    """The run lost the protection of its secret values; every later version of its notice says so, for good."""
    value = run(state, run_id)
    return _replace_run(state, dataclasses.replace(value, protection_lost=True))


def freeze(
    state: TeamRoutines, run_id: str, lease: Lease, now: int, request: tuple[str, str, str, dict[str, object]]
) -> TeamRoutines:
    """Park a run for a person; it keeps no lease, and the same Routine never fires while it is frozen.

    ``request`` is its kind and the Assistant Action that asked at its call's position: a replay step, which must be
    that step of the plan, or a decision call. A Routine being deleted never freezes a run: its deletion ends only the
    frozen runs it saw.
    """
    request_kind, assistant_id, action, position = request
    value = _live(state, run_id, lease, now)
    current = routine(state, value.routine_id)
    if current.deleting:
        raise RoutineStateError("routine-deleting")
    steps = current.plan["steps"]
    placed = http_routine.canonical_position(position, len(steps))
    if (
        request_kind not in http_routine.REQUEST_KINDS
        or http_identifiers.canonical_assistant_id(assistant_id) is None
        or http_identifiers.canonical_action_id(action) is None
        or placed is None
        or (
            placed["phase"] == "replay"
            and (steps[placed["step"] - 1]["assistant"], steps[placed["step"] - 1]["action"]) != (assistant_id, action)
        )
    ):
        raise RoutineStateError("freeze-invalid")
    if sum(item.status == "frozen" for item in state.runs) >= MAX_FROZEN_RUNS:
        raise RoutineStateError("frozen-limit")
    fields = {"request_kind": request_kind, "assistant_id": assistant_id, "action": action, "position": placed}
    state, value = _run_notice(state, value, "frozen", now, {**fields, "steps": len(steps)})
    unleased = {"lease_sha256": "", "lease_key": "", "lease_expires_at": 0}
    return _replace_run(state, dataclasses.replace(value, status="frozen", **unleased, **fields, steps=len(steps)))


def thaw(state: TeamRoutines, run_id: str, now: int, requests_used: int) -> tuple[TeamRoutines, str]:
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
        # The person's answer runs its segment at once, over the run's active time left.
        lease_expires_at=lease_until(value, now),
        request_kind="",
        assistant_id="",
        action="",
        position=None,
        steps=0,
        requests_used=max(value.requests_used, requests_used),
    )
    return _replace_run(state, resumed), token


def finish(
    state: TeamRoutines,
    run_id: str,
    lease: Lease,
    now: int,
    outcome: str,
    detail: dict[str, object],
    shown: dict[str, object] | None = None,
) -> TeamRoutines:
    """A worker ends its leased run with a durable notice; only its live lease may.

    A completed run ends by its Routine's output disposition with ``shown``; any other outcome publishes ``detail``.
    """
    value = _live(state, run_id, lease, now)
    if outcome not in _RUN_OUTCOMES:
        raise RoutineStateError("invalid-outcome")
    if outcome in {"done", "recovered"}:
        state = _completion(state, value, outcome, now, shown)
    else:
        state = _run_notice(state, value, outcome, now, detail)[0]
    return _without_run(state, run_id, now)


def _completion(state: TeamRoutines, value: Run, outcome: str, now: int, shown: dict | None) -> TeamRoutines:
    """How a completed run tells the person, by its Routine's output disposition (ADR-0092 amendment, ADR-0101).

    ``show`` publishes the result every run; ``changes`` only when its keyed digest differs from the last shown one,
    recorded in the same write; ``none`` publishes nothing, a continuous Routine's healthy run rolling up into its
    minute's notice. A result not kept is unavailable, and so is every result after the run lost its protection, which
    never moves the ``changes`` baseline; a run with a notice of its own always gets its terminal version.
    """
    current = routine(state, value.routine_id)
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
                state = _replace_routine(state, dataclasses.replace(current, output_digest=digest or ""))
        return _run_notice(state, value, outcome, now, {**detail, "output": output})[0]
    if not value.notice_version and not value.protection_lost:
        rolled = _healthy(state, value, now) if outcome == "done" else None
        return _quiet(state, current) if rolled is None else rolled
    return _run_notice(state, value, outcome, now, detail)[0]


def _quiet(state: TeamRoutines, current: Routine) -> TeamRoutines:
    """A completed run that tells the person nothing still resets its Routine's failure streak."""
    return _replace_routine(state, dataclasses.replace(current, failures=0))


def _healthy(state: TeamRoutines, value: Run, now: int) -> TeamRoutines | None:
    """Roll a continuous Routine's healthy run into the one versioned notice of the minute it ended in, or None.

    Its count doubles as the notice version Admin acknowledges, so a notice acknowledged mid-minute is replaced by the
    next version, which carries the minute's summed usage (ADR-0092 section 9, ADR-0101).
    """
    current = routine(state, value.routine_id)
    if not continuous(current):
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
    state = _replace_routine(state, rolled)
    notice_id = hashlib.sha256(f"healthy:{current.routine_id}:{minute}".encode()).hexdigest()[:32]
    notice = Notice(notice_id, current.routine_id, "", "healthy", minute, {"runs": runs}, runs)
    return _notice(state, dataclasses.replace(notice, usage=copy.deepcopy(usage)))


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

    A leased run ends stopped or failed; a frozen run denied, stopped, or failed; one that may have acted is held
    instead (``fence``). With ``status``, the run must still be in it, so a stale decision never lands on a changed run.
    """
    value = run(state, run_id)
    if status and value.status != status:
        raise RoutineStateError("run-changed")
    allowed = {"leased": frozenset({"stopped", "failed"}), "frozen": _FROZEN_OUTCOMES}.get(value.status, frozenset())
    if outcome not in allowed:
        raise RoutineStateError("invalid-outcome")
    state, _value = _run_notice(state, value, outcome, now, detail)
    return _without_run(state, run_id, now)


def complete_recovered(
    state: TeamRoutines, run_id: str, lease_sha256: str, now: int, shown: dict[str, object] | None = None
) -> TeamRoutines:
    """Team's watchdog ends a leased run whose sealed cursor completed every step, as the worker would have."""
    value = _leased(state, run_id)
    if not secrets.compare_digest(value.lease_sha256, lease_sha256):
        raise RoutineStateError("run-changed")
    state = _completion(state, value, completed(value), now, shown)
    return _without_run(state, run_id, now)


def completed(value: Run) -> str:
    """How a run that completed every step ends: recovered when a continuation after a hold completed it."""
    first = generation_for(network_of(value.generation, value.run_id), value.run_id)
    return "done" if value.generation == first else "recovered"


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


def begin_delete(state: TeamRoutines, routine_id: str) -> tuple[TeamRoutines, tuple[Run, ...]]:
    """Mark a Routine as deleting, so it is never claimed or resumed again; its runs are returned for the caller to end.

    A held run settles into its incident, which outlives the Routine (ADR-0092).
    """
    value = routine(state, routine_id)
    runs = tuple(item for item in state.runs if item.routine_id == routine_id)
    return _replace_routine(state, dataclasses.replace(value, deleting=True)), runs


def complete_delete(state: TeamRoutines, routine_id: str, now: int) -> TeamRoutines:
    """Remove a deleting Routine once none of its runs remains, publishing its ``deleted`` notice in the same write.

    The notice names the Routine as it was and closes its timeline; it and every undelivered notice outlive the record.
    """
    value = routine(state, routine_id)
    if not value.deleting or any(item.routine_id == routine_id for item in state.runs):
        raise RoutineStateError("routine-busy")
    state = _notice(state, Notice(new_id(), routine_id, "", "deleted", now, {}))
    return dataclasses.replace(state, routines=tuple(item for item in state.routines if item.routine_id != routine_id))


def mark_scope_changed(state: TeamRoutines, routine_id: str, now: int, assistants: list[str]) -> TeamRoutines:
    """The Routine's Assistants no longer match its pins: no claim until an authenticated update."""
    value = routine(state, routine_id)
    state = _notice(state, Notice(new_id(), routine_id, "", "scope-changed", now, {"assistants": assistants}))
    return _replace_routine(state, dataclasses.replace(value, needs_reconfirm=True))
