"""Holding a Routine run and settling its incident, without I/O (ADR-0092 sections 5 and 7).

A held run's incident is indexed as the run ends, keeps the run's notice, quote, step, and remaining active time, and
outlives a deleted Routine. It is resumed as a continuation under a fresh internal lease, skipped by the person (Pular),
or paused (Pausar); a person's card checks the exact state it was opened on in the same write.
"""

from __future__ import annotations

import dataclasses
import secrets
from dataclasses import dataclass

from routine import record


def settle_hold(
    state: record.TeamRoutines, run_id: str, now: int, revision: int | None = None, step: tuple[str, str] = ("", "")
) -> record.TeamRoutines:
    """A held run's incident is durable and its batch archived: index the incident and end the run in one write.

    The run's live state is queued for removal like any ended run's; its archived journal marker stays with the
    incident. A claim reserved this incident's room, so it never displaces an unresolved one. ``revision`` is the one
    the run's recovery snapshot binds; without one, the run executed the Routine's current revision. ``step`` is the
    Assistant Action its sealed cursor stopped at, which the run's held notice names.
    """
    value = record.run(state, run_id)
    if value.status != "held":
        raise record.RoutineStateError("run-not-held")
    current = record.routine(state, value.routine_id)
    executed = current.revision if revision is None else revision
    if type(executed) is not int or executed < 1:
        raise record.RoutineStateError("incident-invalid")
    state, value = record._run_notice(state, value, "held", now, _step_detail(step))
    incident = record.Incident(
        run_id,
        value.routine_id,
        value.generation,
        now,
        executed,
        notice_version=value.notice_version,
        quote=current.quote,
        assistant_id=step[0],
        action=step[1],
        active_seconds_left=value.active_seconds_left,
    )
    kept = list(state.incidents)
    while len(kept) >= record.MAX_INCIDENTS:
        released = next((item for item in kept if item.status == "released"), None)
        if released is None:
            raise record.RoutineStateError("incident-limit")
        kept.remove(released)
    return record._without_run(dataclasses.replace(state, incidents=(*kept, incident)), run_id, now)


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
        lease_expires_at=now + record.LEASE_SECONDS,
        active_seconds_left=value.active_seconds_left,
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
    state: record.TeamRoutines, value: record.Incident, outcome: str, now: int, detail: dict[str, object]
) -> tuple[record.TeamRoutines, record.Incident]:
    """Publish the next version of the held run's one notice, which outlives a deleted Routine with the incident."""
    version = value.notice_version + 1
    notice = record.Notice(
        value.incident_id, value.routine_id, value.incident_id, outcome, now, detail, version, value.quote
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
    state: record.TeamRoutines, incident_id: str, now: int, expected: Expected | None = None
) -> record.TeamRoutines:
    """Pular: abandon the rest of the held run and permit future cycles; its possible effects stay unresolved.

    Its notice says the person skipped it, which is distinct from the Routine's own missed-schedule skip. A card's
    ``expected`` state is checked in the same write. The skip is the held run's end: a continuous Routine's next run is
    due its gap after it, however long the run was held.
    """
    value = incident(state, incident_id)
    if value.status != "unresolved":
        raise record.RoutineStateError("incident-not-unresolved")
    _expect(state, value, expected)
    step = _step_detail((value.assistant_id, value.action))
    state, value = _incident_notice(state, value, "user-skipped", now, step)
    state = record.rebase_continuous(state, value.routine_id, now)
    return _replace_incident(state, dataclasses.replace(value, status="skipped"))


def pause_incident(
    state: record.TeamRoutines, incident_id: str, now: int, reason: str, expected: Expected | None = None
) -> record.TeamRoutines:
    """Recovery or a person paused the Routine an unresolved incident holds, and the run's notice says why.

    A card's ``expected`` state is checked in the same write.
    """
    value = incident(state, incident_id)
    if value.status != "unresolved" or reason not in record.PAUSE_REASONS:
        raise record.RoutineStateError("incident-not-unresolved")
    _expect(state, value, expected)
    state = record.set_paused(state, value.routine_id, True)
    detail = {**_step_detail((value.assistant_id, value.action)), "reason": reason}
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
    refunded = min(record.ACTIVE_SECONDS, value.active_seconds_left + seconds)
    return _replace_incident(state, dataclasses.replace(value, active_seconds_left=refunded))
