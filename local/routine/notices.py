"""Routine run outcomes for delivery, and the human decisions that end or release a run (ADR-0086).

Admin's automatic delivery only reads notices and acknowledges exact versions; it never changes a run. Releasing an
uncertain run is a separate, informed Supervisor resolution of that run's exact batch.
"""

from __future__ import annotations

import datetime
import json
from http import HTTPStatus

from local import audit as local_audit
from local.errors import ApiProblemError as ApiProblem
from local.routine import human as routine_human
from local.routine import manage as routine_manage
from local.routine import run as routine_run
from local.routine import state as routine_state
from local.validation import validate_team_id
from routine import record

MAX_DELIVERIES = 256
# Under the Local API's 128 KiB response cap with room for its envelope. A notice at its bound, a 16,000-character reply
# whose every character JSON-escapes to six bytes, is about 96.5 KB, so one always fits.
MAX_BATCH_BYTES = 112 * 1024


def _problem(status: HTTPStatus, message: str, code: str) -> ApiProblem:
    return ApiProblem(status, message, code=code)


def _notice(team_id: str, notice: record.Notice) -> dict[str, object]:
    return {
        "team_id": team_id,
        "notice_id": notice.notice_id,
        "version": notice.version,
        "routine_id": notice.routine_id,
        "run_id": notice.run_id or None,
        "outcome": notice.outcome,
        "created_at": datetime.datetime.fromtimestamp(notice.created_at, datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "detail": notice.detail,
    }


def _size(item: dict[str, object]) -> int:
    return len(json.dumps(item, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")) + 1


def routine_notices(self) -> dict[str, object]:
    """A bounded batch of every Team's undelivered notices, for Admin to write to each Team's transcript.

    A batch stays under MAX_BATCH_BYTES of encoded JSON and always holds at least one notice, which fits even at its
    bound, so Admin drains any backlog by acknowledging each batch and asking again while ``more`` is true.
    """
    notices: list[dict[str, object]] = []
    size = 0
    for team_id in routine_state.call(self.routine_store.teams):
        for notice in routine_state.load(self, team_id).notices:
            item = _notice(team_id, notice)
            if notices and size + _size(item) > MAX_BATCH_BYTES:
                return {"notices": notices, "more": True}
            notices.append(item)
            size += _size(item)
    return {"notices": notices, "more": False}


def _deliveries(body: object) -> dict[str, frozenset[tuple[str, int]]]:
    deliveries = body.get("deliveries") if isinstance(body, dict) and set(body) == {"deliveries"} else None
    if not isinstance(deliveries, list) or not 0 < len(deliveries) <= MAX_DELIVERIES:
        raise _problem(HTTPStatus.UNPROCESSABLE_ENTITY, "Routine deliveries are invalid", "invalid-body")
    by_team: dict[str, set[tuple[str, int]]] = {}
    for item in deliveries:
        if (
            not isinstance(item, dict)
            or set(item) != {"team_id", "notice_id", "version"}
            or not isinstance(item["notice_id"], str)
            or type(item["version"]) is not int
        ):
            raise _problem(HTTPStatus.UNPROCESSABLE_ENTITY, "Routine deliveries are invalid", "invalid-body")
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
        raise _problem(HTTPStatus.NOT_FOUND, "Routine run is unavailable", "routine-run-not-found") from exc


def resolve_routine_run(self, team_id: str, run_id: str, body: object) -> dict[str, object]:
    """A Supervisor's informed resolution of an uncertain run's exact batch; only this releases its Routine."""
    team_id = validate_team_id(team_id)
    fingerprint = (
        body.get("batch_fingerprint") if isinstance(body, dict) and set(body) == {"batch_fingerprint"} else None
    )
    value = _run(self, team_id, run_id)

    def resolve(state: record.TeamRoutines) -> tuple[record.TeamRoutines, bool]:
        try:
            return record.resolve_uncertain(
                state, value.run_id, fingerprint if isinstance(fingerprint, str) else ""
            ), True
        except record.RoutineStateError:
            return state, False

    if not routine_state.update(self, team_id, resolve):
        raise _problem(HTTPStatus.CONFLICT, "Routine run has no such uncertain batch", "routine-run-not-uncertain")
    routine_manage.settle(self, team_id, value.routine_id)
    local_audit.record_request("routine-resolve", result="ok", team_id=team_id, detail=value.run_id)
    return {"team_id": team_id, "run_id": value.run_id, "resolved": True}


def stop_routine(self, team_id: str, run_id: str) -> dict[str, object]:
    """Stop exactly one run: a running one ends itself once stopped; a frozen one ends now."""
    team_id = validate_team_id(team_id)
    value = _run(self, team_id, run_id)
    if value.status == "uncertain":
        raise _problem(HTTPStatus.CONFLICT, "An uncertain run needs a resolution, not Stop", "routine-run-uncertain")
    if value.status == "leased":
        return {
            "team_id": team_id,
            "run_id": value.run_id,
            "stopped": routine_run.halt_routine_run(self, team_id, value.run_id),
        }
    routine_human.cancel_routine_challenge(self, team_id, value.run_id)
    # A replay may have resumed the frozen run since it was read: then it is running, and Stop reaches its segment.
    stopped = routine_manage.end_frozen(
        self, team_id, value.run_id, "stopped", {"actions": []}
    ) or routine_run.halt_routine_run(self, team_id, value.run_id)
    routine_manage.settle(self, team_id, value.routine_id)
    return {"team_id": team_id, "run_id": value.run_id, "stopped": stopped}
