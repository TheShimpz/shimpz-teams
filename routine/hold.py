"""Holding a Routine run and settling its incident, without I/O (ADR-0092 sections 5 and 7, ADR-0101).

A held run's incident is indexed as the run ends, keeps the run's notice, name, held call, remaining active time, usage,
rehearsal, and lost protection, and outlives a deleted Routine's record only until it is released. It is resumed as a
continuation under a fresh internal lease after Team-admitted evidence, or set aside by a person: Rodar, which requests
one fresh run, or the Routine's deletion. A person's card checks the exact state it was opened on in the same write.
"""

from __future__ import annotations

import dataclasses
import secrets
from dataclasses import dataclass

from protocol.http.v1 import routine as http_routine
from routine import definition as routine_definition
from routine import plan as routine_plan
from routine import record

# The call a held run stopped at: its Assistant, Action, position, and its plan's step count; all empty when the run
# sealed no cursor (ADR-0092 amendment, 2026-10-05, scale; ADR-0101 positions).
HeldStep = tuple[str, str, dict[str, object] | None, int]
UNKNOWN_STEP: HeldStep = ("", "", None, 0)


def settle_hold(
    state: record.TeamRoutines, run_id: str, now: int, revision: int | None = None, step: HeldStep = UNKNOWN_STEP
) -> record.TeamRoutines:
    """A held run's incident is durable and its batch archived: index the incident and end the run in one write.

    The run's live state is queued for removal like any ended run's; its archived journal marker stays with the
    incident. A claim reserved this incident's room, so it never displaces an unresolved one. ``revision`` is the one
    the run's recovery snapshot binds; without one, the run executed the Routine's current revision. ``step`` is the
    Assistant Action its sealed cursor stopped at, with its position, which the run's held notice names.
    """
    value = record.run(state, run_id)
    if value.status != "held":
        raise record.RoutineStateError("run-not-held")
    current = record.routine(state, value.routine_id)
    executed = current.revision if revision is None else revision
    if type(executed) is not int or executed < 1:
        raise record.RoutineStateError("incident-invalid")
    state, value = record._run_notice(state, value, "held", now, step_detail(step))
    incident = record.Incident(
        run_id,
        value.routine_id,
        value.generation,
        now,
        executed,
        notice_version=value.notice_version,
        name=current.name,
        assistant_id=step[0],
        action=step[1],
        active_seconds_left=value.active_seconds_left,
        position=step[2],
        steps=step[3],
        requests_used=value.requests_used,
        usage=value.usage,
        rehearsal=value.rehearsal,
        protection_lost=value.protection_lost,
    )
    kept = list(state.incidents)
    while len(kept) >= record.MAX_INCIDENTS:
        released = next((item for item in kept if item.status == "released"), None)
        if released is None:
            raise record.RoutineStateError("incident-limit")
        kept.remove(released)
    state = record._without_run(dataclasses.replace(state, incidents=(*kept, incident)), run_id, now)
    # A Routine being deleted keeps no incident for a person to settle: its run is set aside as it is indexed.
    return skip_incident(state, run_id, now, choice="delete") if current.deleting else state


def incident(state: record.TeamRoutines, incident_id: str) -> record.Incident:
    for item in state.incidents:
        if item.incident_id == incident_id:
            return item
    raise record.RoutineStateError("incident-not-found")


def reopen_incident(
    state: record.TeamRoutines, incident_id: str, now: int, generation: str
) -> tuple[record.TeamRoutines, str]:
    """Resume a held run after Team-admitted evidence, as a continuation in its own ``generation`` (ADR-0092).

    Only the revision the run executed, of a Routine still listed, not paused, and with no other run, may continue.
    The incident gives way in the same write, and its archived generation is queued for removal; the continuation runs
    under a fresh internal lease that no machine assertion knows, and goes on with the run's notice.
    """
    value = incident(state, incident_id)
    if value.status != "unresolved":
        raise record.RoutineStateError("incident-not-unresolved")
    current = record.routine(state, value.routine_id)
    if current.deleting or current.paused or current.revision != value.revision:
        raise record.RoutineStateError("routine-not-resumable")
    if value.active_seconds_left <= 0:
        raise record.RoutineStateError("run-time-exhausted")
    if (
        any(item.routine_id == value.routine_id for item in state.runs)
        or record.network_of(generation, incident_id) is None
    ):
        raise record.RoutineStateError("routine-busy")
    token = secrets.token_urlsafe(32)
    resumed = record.Run(
        incident_id,
        value.routine_id,
        "leased",
        value.created_at,
        lease_sha256=record.lease_sha256(token),
        lease_key=record.HUMAN_LEASE,
        # The continuation runs at once, over the run's active time left.
        lease_expires_at=now + value.active_seconds_left + routine_plan.LEASE_MARGIN_SECONDS,
        active_seconds_left=value.active_seconds_left,
        generation=generation,
        notice_version=value.notice_version,
        requests_used=value.requests_used,
        usage=value.usage,
        rehearsal=value.rehearsal,
        protection_lost=value.protection_lost,
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


def step_detail(step: HeldStep) -> dict[str, object]:
    assistant_id, action, position, steps = step
    if not assistant_id:
        return {"assistant_id": None, "action": None, "position": None, "steps": None}
    return {"assistant_id": assistant_id, "action": action, "position": position, "steps": steps}


def held_step(value: record.Incident) -> HeldStep:
    return (value.assistant_id, value.action, value.position, value.steps)


def _incident_notice(
    state: record.TeamRoutines, value: record.Incident, outcome: str, now: int, detail: dict[str, object]
) -> tuple[record.TeamRoutines, record.Incident]:
    """Publish the next version of the held run's one notice, which outlives a deleted Routine with the incident."""
    version = value.notice_version + 1
    notice = record.Notice(
        value.incident_id,
        value.routine_id,
        value.incident_id,
        outcome,
        now,
        detail,
        version,
        value.name,
        value.usage,
        value.protection_lost,
    )
    return record._notice(state, notice), dataclasses.replace(value, notice_version=version)


def _replace_incident(state: record.TeamRoutines, updated: record.Incident) -> record.TeamRoutines:
    return dataclasses.replace(
        state, incidents=tuple(updated if item.incident_id == updated.incident_id else item for item in state.incidents)
    )


@dataclass(frozen=True, slots=True)
class Expected:
    """What a person's recovery card was opened on: the run's revision and generation, and the Routine's revision.

    ``current`` is 0 when the Routine is deleted or being deleted.
    """

    revision: int
    generation: str
    current: int


def _expect(state: record.TeamRoutines, value: record.Incident, expected: Expected | None) -> None:
    """Refuse a card's transition when the incident or its Routine changed since the card opened."""
    if expected is None:
        return
    current = next(
        (item.revision for item in state.routines if item.routine_id == value.routine_id and not item.deleting), 0
    )
    if (value.revision, value.generation, current) != (expected.revision, expected.generation, expected.current):
        raise record.RoutineStateError("incident-changed")


def skip_incident(
    state: record.TeamRoutines, incident_id: str, now: int, expected: Expected | None = None, *, choice: str
) -> record.TeamRoutines:
    """A person sets the held run aside, never verified, replayed, or fabricated; its possible effects stay unresolved.

    ``choice`` is how: Rodar (``run``) or deleting the Routine (``delete``), which the run's
    ``user-skipped`` notice names; it is distinct from the Routine's own missed-schedule skip. A card's ``expected``
    state is checked in the same write. Setting it aside is the held run's end: a continuous Routine's next run is due
    its gap after it, however long the run was held.
    """
    value = incident(state, incident_id)
    if value.status != "unresolved":
        raise record.RoutineStateError("incident-not-unresolved")
    _expect(state, value, expected)
    detail = {**step_detail(held_step(value)), "choice": choice}
    state, value = _incident_notice(state, value, "user-skipped", now, detail)
    state = record.rebase_continuous(state, value.routine_id, now)
    return _replace_incident(state, dataclasses.replace(value, status="skipped"))


def run_incident(state: record.TeamRoutines, incident_id: str, now: int, expected: Expected) -> record.TeamRoutines:
    """Rodar: set the held run aside and request one fresh run of the Routine's current revision, in one write.

    The Routine leaves its pause with a fresh failure streak. A fixed schedule keeps the request until a claim starts
    it, under every start cap and without moving its cadence; a continuous one is simply due its gap after this.
    """
    value = incident(state, incident_id)
    state = record.set_paused(skip_incident(state, incident_id, now, expected, choice="run"), value.routine_id, False)
    current = record.routine(state, value.routine_id)
    if record.continuous(current):
        return state
    return record._replace_routine(state, dataclasses.replace(current, run_requested=now))


def pause_incident(state: record.TeamRoutines, incident_id: str, now: int, reason: str) -> record.TeamRoutines:
    """Recovery paused the Routine an unresolved incident holds, and the run's notice says why."""
    value = incident(state, incident_id)
    if value.status != "unresolved" or reason not in record.PAUSE_REASONS:
        raise record.RoutineStateError("incident-not-unresolved")
    state = record.set_paused(state, value.routine_id, True)
    detail = {**step_detail(held_step(value)), "reason": reason}
    state, value = _incident_notice(state, value, "paused", now, detail)
    return _replace_incident(state, value)


def release_incident(state: record.TeamRoutines, incident_id: str) -> record.TeamRoutines:
    """A skipped incident's archive marker, cursor, and evidence are gone: only now may its record give way."""
    value = incident(state, incident_id)
    if value.status == "released":
        return state
    if value.status != "skipped":
        raise record.RoutineStateError("incident-not-skipped")
    released = dataclasses.replace(value, status="released")
    return dataclasses.replace(
        state, incidents=tuple(released if item.incident_id == incident_id else item for item in state.incidents)
    )


def used(state: record.TeamRoutines, incident_id: str, models: list[dict[str, object]]) -> record.TeamRoutines:
    """Add the tokens a held run's recovery reported to the usage its notices and continuation carry (ADR-0101)."""
    value = incident(state, incident_id)
    usage = record.joined_usage(value.usage, {"duration_ms": 0, "models": models})
    if http_routine.canonical_run_usage(usage) != usage:
        raise record.RoutineStateError("usage-invalid")
    return _replace_incident(state, dataclasses.replace(value, usage=usage))


def charge_incident(state: record.TeamRoutines, incident_id: str, seconds: int) -> record.TeamRoutines:
    """Reserve part of an unresolved incident's remaining active time for its recovery; never more than it has."""
    value = incident(state, incident_id)
    if value.status != "unresolved" or type(seconds) is not int or not 0 < seconds <= value.active_seconds_left:
        raise record.RoutineStateError("incident-time-invalid")
    return _replace_incident(state, dataclasses.replace(value, active_seconds_left=value.active_seconds_left - seconds))


def refund_incident(state: record.TeamRoutines, incident_id: str, generation: str, seconds: int) -> record.TeamRoutines:
    """Return unused reserved time to the same held run it was reserved from; anything else stays as it is."""
    value = next((item for item in state.incidents if item.incident_id == incident_id), None)
    if value is None or value.status != "unresolved" or value.generation != generation or seconds <= 0:
        return state
    # Never beyond the active time its revision gave the run: a hold never refills it. A run whose Routine changed or
    # went can never continue, so it gets nothing back.
    current = next((item for item in state.routines if item.routine_id == value.routine_id), None)
    if current is None or current.revision != value.revision:
        return state
    refunded = min(
        routine_plan.active_seconds(routine_definition.run_units(current)), value.active_seconds_left + seconds
    )
    return _replace_incident(state, dataclasses.replace(value, active_seconds_left=refunded))


def fence(state: record.TeamRoutines, run_id: str, lease: record.Lease, now: int) -> record.TeamRoutines:
    """Stop the live lease of a run that must be held (ADR-0092): no worker may advance it, nothing ends it yet."""
    value = record._live(state, run_id, lease, now)
    if not value.generation:
        raise record.RoutineStateError("generation-invalid")
    held = dataclasses.replace(value, status="held", lease_sha256="", lease_key="", lease_expires_at=0)
    return record._replace_run(state, held)


def hold_recovered(state: record.TeamRoutines, run_id: str, lease_sha256: str) -> record.TeamRoutines:
    """Team's watchdog holds a leased run nothing runs any more whose durable state shows it may have acted.

    Only the exact lease the watchdog read is fenced, so a run that ended or was claimed again meanwhile is untouched.
    """
    value = record._leased(state, run_id)
    if not secrets.compare_digest(value.lease_sha256, lease_sha256):
        raise record.RoutineStateError("run-changed")
    if not value.generation:
        raise record.RoutineStateError("generation-invalid")
    return record._replace_run(
        state, dataclasses.replace(value, status="held", lease_sha256="", lease_key="", lease_expires_at=0)
    )


# What Team's watchdog fences or ends: runs nothing may drive any more.


def expired(state: record.TeamRoutines, now: int) -> tuple[record.Run, ...]:
    """Leased runs whose lease or active time ran out; the caller stops each and decides its outcome."""
    return tuple(
        item
        for item in state.runs
        if item.status == "leased" and (item.lease_expires_at <= now or item.active_seconds_left <= 0)
    )


def rekeyed(state: record.TeamRoutines, key_fingerprint: str) -> tuple[record.Run, ...]:
    """Machine-leased runs claimed under a routine key that is no longer current."""
    return tuple(
        item
        for item in state.runs
        if item.status == "leased" and item.lease_key not in {key_fingerprint, record.HUMAN_LEASE}
    )
