"""A Team's Routines, runs, and notices, and the transitions of Routines and notices, without I/O (ADR-0086).

Claiming due runs lives in ``routine.claim`` and a claimed run's transitions in ``routine.runs``. All instants are
whole UTC epoch seconds. A transition validates the current state itself and returns a new ``TeamRoutines``; the caller
persists it atomically before acting on it, so a crash leaves either the old or the new state, never a mix.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import hashlib
import re
import secrets
from dataclasses import dataclass

from protocol.http.v1 import identifiers as http_identifiers
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import routine_notice as http_routine_notice
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
MAX_UNRESOLVED_INCIDENTS = http_routine_notice.MAX_UNRESOLVED_INCIDENTS
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
    # Where its timezone came from: the person's browser, a zone the person wrote, or none, when the Routine needs no
    # zone and ``timezone`` is "UTC" only by convention (ADR-0101).
    timezone_source: str = "browser"
    # Pausar: no dispatch until resumed; an unresolved incident still holds the Routine after that.
    paused: bool = False
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
    # The held run's answered human requests, usage, and protection, which its continuation goes on from.
    requests_used: int = 0
    usage: dict[str, object] = dataclasses.field(default_factory=lambda: {"duration_ms": 0, "models": []})
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
        and (1 <= value.allowance <= http_routine_notice.MAX_ALLOWANCE if decide else value.allowance == 0)
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
        and http_routine.zoned(value.timezone, value.timezone_source)
        and canonical is not None
        and _permitted(value)
        and _assistants(value)
        and _confirmed(value.confirmation)
        and _decision_scope(value)
        and type(value.paused) is bool
        and type(value.permissions_revision) is int
        and 0 <= value.permissions_revision < 2**31
        and type(value.revision) is int
        and 1 <= value.revision < 2**31
        and type(value.anchor) is int
    )


def _admitted(value: Routine, revision: int = 1) -> Routine:
    """A copy of a new Routine revision in the closed contract; anything else is refused before it can be persisted.

    A new revision is scheduled from its anchor; later claims and sweeps move its next firing on.
    """
    canonical = http_routine.canonical_schedule(value.schedule)
    if not definition_valid(dataclasses.replace(value, revision=revision)) or (
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
    detail = http_routine_notice.canonical_notice_detail(notice.outcome, notice.detail)
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
