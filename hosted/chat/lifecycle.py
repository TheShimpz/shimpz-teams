"""Hosted chat cleanup when an authorized Team lifecycle changes or one of its files is deleted."""

from http import HTTPStatus

from action import journal as action_journal
from chat import attachments as chat_attachments
from hosted import state as runtime_state
from hosted.assistant import runtime as hosted_assistants
from hosted.team import resources as hosted_resources
from inference import client as brain_runtime_client
from storage import files as team_storage


def cancel_replayable_human(team_id: str, generation: str) -> bool:
    """Cancel a pending human gate and end only its own paused batch when settled; uncertain work stays.

    Ending is bound to the paused batch's fingerprint, so a batch that another turn prepared is never ended.
    """
    challenge = runtime_state._human_challenges.withdraw_team(team_id)
    if challenge is None:
        return False
    pending = challenge.payload
    if not isinstance(pending, hosted_assistants._PendingHostedChat) or not isinstance(pending.paused_batch, str):
        raise AssertionError("invalid hosted human continuation")
    try:
        runtime_state._action_execution_journal().end_settled_batch(generation, pending.paused_batch)
    except action_journal.ActionJournalError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team Action execution state is unavailable",
        ) from exc
    return True


def cancel_paused_integration(team_id: str) -> bool:
    """Withdraw a pending Integration gate with the Team's OAuth state, which only a live gate can start.

    An Integration pause comes before its Action batch is prepared, so its turn holds no batch to end.
    """
    challenge = runtime_state._integration_challenges.withdraw_team(team_id)
    if challenge is not None and (
        not isinstance(challenge.payload, hosted_assistants._PendingHostedChat)
        or challenge.payload.paused_batch is not None
    ):
        raise AssertionError("invalid hosted integration continuation")
    runtime_state._integration_pkce.cancel_team(team_id)
    return challenge is not None


def turn_started(team_id: str, file_ids: object) -> tuple[str, ...]:
    """Record that a new turn references these files, before its Brain start can (ADR-0093); return those it added."""
    if not file_ids:
        return ()
    try:
        return runtime_state._storage().reference(team_id, file_ids)
    except team_storage.StorageNotFoundError as exc:
        raise runtime_state.ApiError(HTTPStatus.NOT_FOUND, "selected file not found") from exc
    except team_storage.StorageError as exc:
        raise runtime_state.ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Team storage failed its safety checks") from exc


def turn_failed(team_id: str, added: tuple[str, ...]) -> None:
    chat_attachments.release_failed_turn(runtime_state._storage, team_id, added)


def turn_completed(team_id: str, file_ids: object) -> None:
    chat_attachments.settle_completed_turn(runtime_state._storage, team_id, file_ids)


def forget_file(team_id: str, file_id: str, container_id: str) -> None:
    """Invalidate everything that could still deliver or show a file about to be deleted (ADR-0093).

    Held under the Team's execution slot: a paused turn that selected the file, or any paused turn when the Brain
    thread must be purged, is cancelled with its challenges, keeping uncertain Action evidence; the Brain thread is
    deleted when it may reference the file or nothing is known about it.
    """
    pending = chat_attachments.paused_files(
        team_id, (runtime_state._human_challenges, runtime_state._integration_challenges)
    )
    storage = runtime_state._storage()
    if not chat_attachments.forget_required(storage, team_id, file_id, pending):
        return
    if pending is not None:
        runtime_state._integration_challenges.cancel_team(team_id)
        runtime_state._integration_pkce.cancel_team(team_id)
        runtime_state._human_challenges.cancel_team(team_id)
        try:
            # The caller holds the Team's one execution slot throughout, so no turn can own a batch of this generation
            # but the paused one (or settled residue a restart left); ending the generation's settled state is exact.
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
    storage.settle(team_id, ())
