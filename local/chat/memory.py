"""A committed chat turn's memory changes, recorded as activity before they are saved (ADR-0084, ADR-0121).

Team commits each change as the machine actor and names the turn's author as its initiator; an event carries the
memory's opaque id, never its topic, preference, or the turn's request nonce.
"""

import logging
import secrets
from collections.abc import Sequence
from http import HTTPStatus

from inference import config as inference_config
from inference import memory as inference_memory
from local import audit as local_audit
from local.errors import ApiProblemError

STORE_FAILED = "memory-store-failed"
log = logging.getLogger("shimpz.team.local.chat.memory")


def save(store: object, team_id: str, changes: Sequence[dict[str, str]], source: inference_memory.Source) -> None:
    """Record one `requested` event per change, durably, then save; settle each as `completed` or `failed`.

    A journal that cannot record the request fails the turn before memory is touched. A failed save fails the turn.
    A completion the journal cannot record after the save never raises: the change stays applied and one
    metadata-only line goes to the process log.
    """
    try:
        plan = store.plan_knowledge(team_id, list(changes), source)
    except inference_config.InferenceConfigError as exc:
        raise _store_failed() from exc
    if not plan.changes:
        return
    events = tuple((change, secrets.token_hex(16)) for change in plan.changes)
    for change, event_id in events:
        _record(team_id, source, change, event_id, "requested")
    local_audit.flush()
    try:
        store.save_knowledge(team_id, plan)
    except inference_config.InferenceConfigError as exc:
        _settle(team_id, source, events, "failed")
        raise _store_failed() from exc
    _settle(team_id, source, events, "completed")


def _store_failed() -> ApiProblemError:
    return ApiProblemError(HTTPStatus.SERVICE_UNAVAILABLE, "Team memory could not be saved", code=STORE_FAILED)


def _record(
    team_id: str, source: inference_memory.Source, change: inference_memory.Change, event_id: str, phase: str
) -> None:
    activity = {
        "event_id": event_id,
        "phase": phase,
        "subject": change.memory_id,
        "incarnation": source.incarnation,
        "initiator_id": source.actor_id,
    }
    if phase == "failed":
        activity["outcome"] = STORE_FAILED
    local_audit.record_activity(
        change.operation,
        result="error" if phase == "failed" else "ok",
        principal=local_audit.TEAM,
        team_id=team_id,
        activity=activity,
    )


def _settle(
    team_id: str,
    source: inference_memory.Source,
    events: tuple[tuple[inference_memory.Change, str], ...],
    phase: str,
) -> None:
    """Record each change's outcome; a journal that cannot leaves one metadata-only process-log line instead."""
    try:
        for change, event_id in events:
            _record(team_id, source, change, event_id, phase)
    except RuntimeError as exc:
        failure = type(exc).__name__
    else:
        return
    identifiers = ",".join(event_id for _change, event_id in events)
    log.error("Team could not record memory %s for Team %s: events %s (%s)", phase, team_id, identifiers, failure)
