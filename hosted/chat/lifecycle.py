"""Hosted chat cleanup when an authorized Team lifecycle changes or one of its files is deleted."""

from http import HTTPStatus

from action import journal as action_journal
from hosted import state as runtime_state
from hosted.team import resources as hosted_resources
from inference import client as brain_runtime_client


def cancel_replayable_human(team_id: str, generation: str) -> bool:
    """Cancel a pending human gate and end only settled Action state; uncertain work stays."""
    if not runtime_state._human_challenges.cancel_team(team_id):
        return False
    try:
        runtime_state._action_execution_journal().end_settled(generation)
    except action_journal.ActionJournalError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team Action execution state is unavailable",
        ) from exc
    return True


def turn_started(team_id: str, file_ids: object) -> None:
    """Record that a new turn may reference these files, before its Brain start can (ADR-0093)."""
    with runtime_state._active_chat_guard:
        known = runtime_state._brain_files.get(team_id)
        if known is not None:
            runtime_state._brain_files[team_id] = known | frozenset(file_ids)


def turn_completed(team_id: str, file_ids: object) -> None:
    """A completed turn leaves its Brain thread referencing only this turn's files."""
    with runtime_state._active_chat_guard:
        runtime_state._brain_files[team_id] = frozenset(file_ids)


def _pending_files(team_id: str) -> tuple[str, ...] | None:
    for store in (runtime_state._human_challenges, runtime_state._integration_challenges):
        current = store.current(team_id)
        if current is not None:
            # A paused turn whose state is unreadable is treated as referencing every file.
            return tuple(getattr(current.payload, "file_ids", ("*",)))
    return None


def forget_file(team_id: str, file_id: str, container_id: str) -> None:
    """Invalidate everything that could still deliver or show a file about to be deleted (ADR-0093).

    Held under the Team's execution slot: a paused turn that selected the file, or any paused turn when the Brain
    thread must be purged, is cancelled with its challenges, keeping uncertain Action evidence; the Brain thread is
    deleted when it may reference the file or nothing is known about it.
    """
    pending = _pending_files(team_id)
    with runtime_state._active_chat_guard:
        known = runtime_state._brain_files.get(team_id)
    purge = known is None or file_id in known
    referenced = pending is not None and ("*" in pending or file_id in pending)
    if not purge and not referenced:
        return
    if pending is not None:
        runtime_state._integration_challenges.cancel_team(team_id)
        runtime_state._integration_pkce.cancel_team(team_id)
        runtime_state._human_challenges.cancel_team(team_id)
        try:
            runtime_state._action_execution_journal().end_settled(container_id)
        except action_journal.ActionJournalError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Team Action execution state is unavailable",
            ) from exc
    try:
        runtime_state._brain_runtime.delete_thread(hosted_resources._brain_thread_id(team_id, container_id))
    except brain_runtime_client.BrainRuntimeError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE, "Team conversation state could not be deleted"
        ) from exc
    with runtime_state._active_chat_guard:
        runtime_state._brain_files[team_id] = frozenset()
