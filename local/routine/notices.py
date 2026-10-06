"""Routine run outcomes for delivery, and a person's Stop of a run (ADR-0086).

Admin's automatic delivery only reads notices and acknowledges exact versions; it never changes a run. A held run is
settled through its recovery card instead (ADR-0092).
"""

from __future__ import annotations

from local import audit as local_audit
from local import errors as local_errors
from local.errors import ApiProblemError as ApiProblem
from local.routine import human as routine_human
from local.routine import manage as routine_manage
from local.routine import run as routine_run
from local.routine import state as routine_state
from local.validation import validate_team_id
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import routine_notice as http_routine_notice
from routine import record

MAX_DELIVERIES = 256


def _notice(team_id: str, notice: record.Notice) -> dict[str, object]:
    return {
        "team_id": team_id,
        "notice_id": notice.notice_id,
        "version": notice.version,
        "routine_id": notice.routine_id,
        "name": notice.name,
        "run_id": notice.run_id or None,
        "outcome": notice.outcome,
        "created_at": record.instant_text(notice.created_at),
        "detail": notice.detail,
        "usage": notice.usage,
        "protection_lost": notice.protection_lost,
    }


def _team_notices(self, team_id: str) -> tuple[record.Notice, ...]:
    """One identified Team's notices; a Team whose state cannot be read is audited and left out, never blocking others.

    Its notices stay undelivered and unacknowledged in its state until it reads again.
    """
    try:
        return routine_state.load(self, team_id).notices
    except ApiProblem:
        local_audit.record_request(
            "routine-notices", result="error", team_id=team_id, detail="routine-state-unavailable"
        )
        return ()


def routine_notices(self) -> dict[str, object]:
    """A bounded batch of every readable Team's undelivered notices, for Admin to write to each Team's transcript.

    A batch's encoded notice list stays within the protocol's byte bound and always holds at least one notice, which
    fits even at its bound, so Admin drains any backlog by acknowledging each batch and asking again while ``more``.
    Teams that cannot be enumerated fail the whole batch; one identified Team's damaged state is only skipped.
    """
    notices: list[dict[str, object]] = []
    size = 2  # the list's brackets
    for team_id in routine_state.call(self.routine_store.teams):
        for notice in _team_notices(self, team_id):
            item = _notice(team_id, notice)
            cost = http_routine.encoded_bytes(item) + (1 if notices else 0)
            if notices and size + cost > http_routine_notice.MAX_NOTICE_BATCH_BYTES:
                return {"notices": notices, "more": True}
            notices.append(item)
            size += cost
    return {"notices": notices, "more": False}


def _deliveries(body: object) -> dict[str, frozenset[tuple[str, int]]]:
    deliveries = body.get("deliveries") if isinstance(body, dict) and set(body) == {"deliveries"} else None
    if not isinstance(deliveries, list) or not 0 < len(deliveries) <= MAX_DELIVERIES:
        raise local_errors.routine_deliveries_invalid()
    by_team: dict[str, set[tuple[str, int]]] = {}
    for item in deliveries:
        if (
            not isinstance(item, dict)
            or set(item) != {"team_id", "notice_id", "version"}
            or not isinstance(item["notice_id"], str)
            or type(item["version"]) is not int
        ):
            raise local_errors.routine_deliveries_invalid()
        by_team.setdefault(validate_team_id(item["team_id"]), set()).add((item["notice_id"], item["version"]))
    return {team_id: frozenset(pairs) for team_id, pairs in by_team.items()}


def acknowledge_notices(self, body: object) -> dict[str, object]:
    """Admin wrote these exact notice versions to the transcripts; a notice updated since stays undelivered."""
    for team_id, delivered in _deliveries(body).items():
        routine_state.update(self, team_id, lambda state, pairs=delivered: (record.acknowledge(state, pairs), None))
    return {"acknowledged": True}


def _run(self, team_id: str, run_id: object) -> record.Run:
    state = routine_state.load(self, team_id)
    try:
        return record.run(state, run_id if isinstance(run_id, str) else "")
    except record.RoutineStateError as exc:
        raise local_errors.routine_run_not_found() from exc


def _stop_recovery(self, team_id: str, run_id: object) -> dict[str, object] | None:
    """Stop the verification or automatic episode of a held run's unresolved incident, keeping its evidence.

    Its registration is cancelled, so the verifier stops and no continuation of the run may start; the incident stays
    for the person to settle.
    """
    state = routine_state.load(self, team_id)
    found = any(item.incident_id == run_id and item.status == "unresolved" for item in state.incidents)
    if not found:
        return None
    return {"team_id": team_id, "run_id": run_id, "stopped": routine_run.stop_routine_run(self, team_id, run_id)}


def stop_routine(self, team_id: str, run_id: str) -> dict[str, object]:
    """Stop exactly one run: a running one ends itself once stopped; a frozen one ends now.

    A held run's recovery in progress stops, while its incident stays.
    """
    team_id = validate_team_id(team_id)
    recovering = _stop_recovery(self, team_id, run_id)
    if recovering is not None:
        return recovering
    value = _run(self, team_id, run_id)
    if value.status == "leased":
        halted = routine_run.halt_routine_run(self, team_id, value.run_id)
        # A segment that froze before this Stop cancelled it is frozen now: Stop ends it as any frozen run.
        runs = routine_state.load(self, team_id).runs
        if not any(item.run_id == value.run_id and item.status == "frozen" for item in runs):
            return {"team_id": team_id, "run_id": value.run_id, "stopped": halted}
    # Under the Team lifecycle lock an opening either published its challenge first, and it is withdrawn here, or
    # finds the run ended. A replay may have resumed the frozen run since it was read: Stop then reaches its segment.
    with self._lock(team_id):
        routine_human.cancel_routine_challenge(self, team_id, value.run_id)
        ended = routine_manage.end_frozen(self, team_id, value.run_id, "stopped", {"actions": []})
    stopped = ended or routine_run.halt_routine_run(self, team_id, value.run_id)
    routine_manage.settle(self, team_id, value.routine_id)
    return {"team_id": team_id, "run_id": value.run_id, "stopped": stopped}
