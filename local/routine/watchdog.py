"""Team enforces every Routine lease and deadline itself, even if Admin's worker stalls or Team restarts (ADR-0086).

A leased run whose lease or active time ran out, or whose routine key is no longer current, is stopped when its
segment is running; otherwise nothing can be running it, so it is recovered: a batch that may have acted holds the
run uncertain, and anything else ends it interrupted. Nothing is replayed automatically.
"""

from __future__ import annotations

import logging
import threading
import time

from action import journal as action_journal
from local import audit as local_audit
from local import authority as local_authority
from local.errors import ApiProblemError
from local.routine import manage as routine_manage
from local.routine import run as routine_run
from local.routine import store as routine_store
from routine import record

INTERVAL_SECONDS = 30
_PRINCIPAL = local_audit.AuditPrincipal("team-local", "machine")
# What one failed pass, or one Team's failed check within a periodic pass, can raise; the next pass retries it.
_PASS_ERRORS = (
    routine_store.RoutineStoreError,
    record.RoutineStateError,
    action_journal.ActionJournalError,
    ApiProblemError,
)
log = logging.getLogger("shimpz.team.local.routine.watchdog")


def _audit(operation: str, detail: str, team_id: str | None = None) -> None:
    """Audit one watchdog event without letting a failed journal stop recovery.

    The fallback log names only the event and the failure's type, never a message that could carry a secret.
    """
    try:
        local_audit.record(operation, result="error", principal=_PRINCIPAL, team_id=team_id, detail=detail)
    except RuntimeError as exc:
        failure = type(exc).__name__
    else:
        return
    log.error("Routine watchdog could not audit %s/%s (%s)", operation, detail, failure)


def _recover(service, team_id: str, value: record.Run) -> str | None:
    """End a leased run that nothing is running; its journal decides whether its effects are uncertain.

    The run is re-read in the same write: one that ended or changed lease since the pass read it is left alone.
    """
    now = int(time.time())
    fingerprint = service.action_state.uncertain_fingerprint(value.generation) if value.generation else None

    def recover(state: record.TeamRoutines) -> tuple[record.TeamRoutines, str | None]:
        current = next((item for item in state.runs if item.run_id == value.run_id), None)
        if current is None or current.status != "leased" or current.lease_sha256 != value.lease_sha256:
            return state, None
        if fingerprint is not None:
            return record.end(state, value.run_id, now, "uncertain", {"actions": []}, fingerprint), "uncertain"
        return record.end(state, value.run_id, now, "failed", {"code": "interrupted", "actions": []}), "failed"

    return service.routine_store.update(team_id, recover)


def _check_team(service, team_id: str, now: int, key: str | None, *, startup: bool) -> None:
    state = service.routine_store.load(team_id)
    stale = {item.run_id: item for item in record.expired(state, now)}
    if key is not None:
        stale.update({item.run_id: item for item in record.rekeyed(state, key)})
    if startup:
        # After a restart nothing runs any segment, so every leased run is recovered at once.
        stale.update({item.run_id: item for item in state.runs if item.status == "leased"})
    for value in stale.values():
        if routine_run.stop_routine_run(service, team_id, value.run_id):
            continue
        outcome = _recover(service, team_id, value)
        if outcome is not None:
            _audit("routine-recover", outcome, team_id)
    # A continuation whose run no longer exists was left by a crash; an ended run's own is queued for removal anyway.
    runs = {item.run_id for item in service.routine_store.load(team_id).runs}
    for run_id in service.routine_store.continuations(team_id):
        if run_id not in runs:
            service.routine_store.delete_continuation(team_id, run_id)
    routine_manage.settle_team(service, team_id)


def check(service, *, startup: bool = False) -> None:
    """One pass: stop segments out of active time, then recover and clean up every Team's Routine runs.

    A periodic pass audits one Team's failure and goes on, so a Team whose state cannot be read never holds back the
    others. At startup every failure stays fatal, as does a Routine directory whose Team cannot be identified.
    """
    for team_id, run_id in routine_run.stop_overdue(service):
        _audit("routine-stop", run_id, team_id)
    try:
        key = local_authority.routine_key_fingerprint()
    except local_authority.SupervisorUnavailableError:
        key = None
    now = int(time.time())
    for team_id in service.routine_store.teams():
        try:
            _check_team(service, team_id, now, key, startup=startup)
        except _PASS_ERRORS:
            if startup:
                raise
            _audit("routine-watchdog", "team-check-failed", team_id)


class RoutineWatchdog:
    """A daemon that checks Routine leases every 30 seconds until the controller stops."""

    def __init__(self, service, *, interval: float = INTERVAL_SECONDS) -> None:
        self._service = service
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="routine-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(30)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                check(self._service)
            except _PASS_ERRORS:
                # A failed pass leaves every run as it was; the next pass retries, and the failure is audited.
                _audit("routine-watchdog", "check-failed")
