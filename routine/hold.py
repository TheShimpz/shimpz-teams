"""Holding a Routine run and settling its incident, without I/O (ADR-0092 sections 5 and 7, ADR-0101).

A held run becomes its own incident in the write that indexes it: the same record, ``unresolved``, keeping the run's
notice, held call, remaining active time, usage, and lost protection, while its generation's removal is queued like an
ended run's. It is resumed in place as the run's continuation, under a fresh internal lease, after Team-admitted
evidence, or set aside (``skipped``) by a person: Rodar, which requests one fresh run, or the Routine's deletion. A
set-aside record goes once what it kept is released, and a Routine's deletion completes only after that, so no run
record outlives its Routine. A person's card checks the exact state it was opened on in the same write.
"""

import dataclasses
from dataclasses import dataclass

from routine import claim as routine_claim
from routine import record

# The call a held run stopped at: its Assistant, Action, position, and its plan's step count; all empty when the run
# sealed no cursor, or when it is a run of a Routine being deleted whose cursor, evidence, or recovery snapshot could
# not be read (ADR-0092 amendment, 2026-10-05, scale; ADR-0101 positions).
HeldStep = tuple[str, str, int | None, int]
UNKNOWN_STEP: HeldStep = ("", "", None, 0)


def settle_hold(
    state: record.TeamRoutines, run_id: str, now: int, step: HeldStep = UNKNOWN_STEP
) -> record.TeamRoutines:
    """A held run's incident is durable and its batch archived: index the run as its incident in one write.

    The run's live state is queued for removal like an ended run's; its archived journal marker stays with the
    incident. ``step`` is the Assistant Action its sealed cursor stopped at, with its position, which its incident
    names.
    """
    value = record.run(state, run_id)
    if value.status != "held":
        raise record.RoutineStateError("run-not-held")
    deleting = record.routine(state, value.routine_id).deleting
    state, value = record.run_notice(state, value, "held", now, {})
    assistant_id, action, position, steps = step
    indexed = dataclasses.replace(
        value,
        status="unresolved",
        assistant_id=assistant_id,
        action=action,
        position=position,
        steps=steps,
    )
    state = record.ended(record.replace_run(state, indexed), indexed, now)
    # A Routine being deleted keeps no incident for a person to settle: its run is set aside as it is indexed.
    return skip_incident(state, run_id, now) if deleting else state


def incident(state: record.TeamRoutines, incident_id: str) -> record.Run:
    """The held run's incident, unresolved or set aside."""
    for item in record.incidents(state):
        if item.run_id == incident_id:
            return item
    raise record.RoutineStateError("incident-not-found")


def spend_incident(state: record.TeamRoutines, incident_id: str, seconds: int) -> record.TeamRoutines:
    """Charge time a held run's recovery spent outside any segment, such as verification, to its active time left."""
    value = incident(state, incident_id)
    return record.replace_run(
        state, dataclasses.replace(value, active_seconds_left=max(0, value.active_seconds_left - seconds))
    )


def reopen_incident(
    state: record.TeamRoutines, incident_id: str, now: int, generation: str
) -> tuple[record.TeamRoutines, str]:
    """Resume a held run in place after Team-admitted evidence, as its continuation in ``generation`` (ADR-0092).

    Only the revision the run executed, of a Routine still listed, not paused, and with no live run, may continue. Its
    archived generation is queued for removal in the same write; the continuation runs under a fresh internal lease
    that no machine assertion knows, and goes on with the run's notice.
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
        any(item.routine_id == value.routine_id for item in record.live_runs(state))
        or routine_claim.network_of(generation, incident_id) is None
    ):
        raise record.RoutineStateError("routine-busy")
    # The continuation runs at once under its own generation.
    resumed, token = routine_claim.human_lease(value, now, generation=generation)
    # The hold queued its generation already unless that removal has run since.
    discards = tuple(dict.fromkeys((*state.discards, (incident_id, value.generation))))
    return dataclasses.replace(record.replace_run(state, resumed), discards=discards), token


def step_detail(step: HeldStep) -> dict[str, object]:
    assistant_id, action, position, steps = step
    if not assistant_id:
        return {"assistant_id": None, "action": None, "position": None, "steps": None}
    return {"assistant_id": assistant_id, "action": action, "position": position, "steps": steps}


def held_step(value: record.Run) -> HeldStep:
    return (value.assistant_id, value.action, value.position, value.steps)


@dataclass(frozen=True, slots=True)
class Expected:
    """What a person's recovery card was opened on: the run's revision and generation, and the Routine's revision.

    ``current`` is 0 when the Routine is deleted or being deleted.
    """

    revision: int
    generation: str
    current: int


def current_revision(state: record.TeamRoutines, routine_id: str) -> int:
    """The Routine's current revision, 0 once it is deleted or being deleted."""
    return next((item.revision for item in state.routines if item.routine_id == routine_id and not item.deleting), 0)


def _expect(state: record.TeamRoutines, value: record.Run, expected: Expected | None) -> None:
    """Refuse a card's transition when the incident or its Routine changed since the card opened."""
    if expected is None:
        return
    current = current_revision(state, value.routine_id)
    if (value.revision, value.generation, current) != (expected.revision, expected.generation, expected.current):
        raise record.RoutineStateError("incident-changed")


def skip_incident(
    state: record.TeamRoutines, incident_id: str, now: int, expected: Expected | None = None
) -> record.TeamRoutines:
    """A person sets the held run aside, never verified, replayed, or fabricated; its possible effects stay unresolved.

    Rodar or deleting the Routine does it; the run's ``user-skipped`` notice is distinct from the Routine's own
    missed-schedule skip. A card's ``expected`` state is checked in the same write.
    """
    value = incident(state, incident_id)
    if value.status != "unresolved":
        raise record.RoutineStateError("incident-not-unresolved")
    _expect(state, value, expected)
    state, value = record.run_notice(state, value, "user-skipped", now, {})
    return record.replace_run(state, dataclasses.replace(value, status="skipped"))


def run_incident(state: record.TeamRoutines, incident_id: str, now: int, expected: Expected) -> record.TeamRoutines:
    """Rodar: set the held run aside and request one fresh run of the Routine's current revision, in one write.

    The Routine leaves its pause with a fresh failure streak. Whatever its schedule, the request stands until a claim
    starts it now, under every start cap and without moving its cadence.
    """
    value = incident(state, incident_id)
    state = record.set_paused(skip_incident(state, incident_id, now, expected), value.routine_id, False)
    current = record.routine(state, value.routine_id)
    return record.replace_routine(state, dataclasses.replace(current, run_requested=now))


def pause_incident(state: record.TeamRoutines, incident_id: str, now: int, reason: str) -> record.TeamRoutines:
    """Recovery paused the Routine an unresolved incident holds, and the run's notice says why."""
    if reason not in record.PAUSE_REASONS:
        raise ValueError("unknown Routine pause reason")
    value = incident(state, incident_id)
    if value.status != "unresolved":
        raise record.RoutineStateError("incident-not-unresolved")
    state = record.set_paused(state, value.routine_id, True)
    state, value = record.run_notice(state, value, "paused", now, {"reason": reason})
    return record.replace_run(state, value)


def release_incident(state: record.TeamRoutines, incident_id: str) -> record.TeamRoutines:
    """A skipped incident's archive marker, cursor, and evidence are gone: its record goes; one gone is released."""
    value = next((item for item in state.runs if item.run_id == incident_id), None)
    if value is None:
        return state
    if value.status != "skipped":
        raise record.RoutineStateError("incident-not-skipped")
    return dataclasses.replace(state, runs=tuple(item for item in state.runs if item.run_id != incident_id))


def _held(state: record.TeamRoutines, value: record.Run) -> record.TeamRoutines:
    """Stop a run's lease so no worker may advance it; nothing ends it yet."""
    if not value.generation:
        raise record.RoutineStateError("generation-invalid")
    held = dataclasses.replace(value, status="held", lease_sha256="", lease_key="", lease_expires_at=0)
    return record.replace_run(state, held)


def fence(state: record.TeamRoutines, run_id: str, lease: record.Lease, now: int) -> record.TeamRoutines:
    """Stop the live lease of a run that must be held (ADR-0092)."""
    return _held(state, routine_claim.live_lease(state, run_id, lease, now))


def hold_recovered(state: record.TeamRoutines, run_id: str, lease_sha256: str) -> record.TeamRoutines:
    """Team's watchdog holds a leased run nothing runs any more whose durable state shows it may have acted."""
    return _held(state, routine_claim.read_lease(state, run_id, lease_sha256))


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
