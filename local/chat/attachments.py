"""Local custody of a turn's selected files: Brain hydration and deletion linearized with their use (ADR-0093)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from http import HTTPStatus

from action import journal as action_journal
from chat import attachments as chat_attachments
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from local import prepare as local_prepare
from local.errors import ApiProblemError as ApiProblem
from local.validation import brain_thread_id as _brain_thread_id
from prepare import helper as preparation_helper
from prepare import service as preparation
from storage import files as team_storage


def turn_attachments(
    self,
    team_id: str,
    token: str,
    files: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Prepare the selected files again for this segment; the helper is gone before the Brain is asked."""
    if not files:
        return ()

    def started(container: object) -> None:
        # Stop reaches a helper exactly as it reaches an Action workload: the chat's one active container slot.
        with self._active_chat_guard:
            if (
                self._active_chat_tokens.get(team_id) != token
                or token in self._cancelled_chat_tokens
                or team_id in self._active_action_containers
            ):
                raise chat_orchestrator.ChatStoppedError("chat turn stopped")
            self._active_action_containers[team_id] = (token, container)

    def stopped(container: object) -> None:
        with self._active_chat_guard:
            active = self._active_action_containers.get(team_id)
            if active is not None and active[1] is container:
                self._active_action_containers.pop(team_id, None)

    lifecycle = self.assistant_lifecycle
    try:
        prepared = local_prepare.prepare_attachments(
            lifecycle.client,
            chat_attachments.stored_files(files, lambda file_id: self.storage.get(team_id, file_id)),
            space_id=self.space_id,
            team_id=team_id,
            cpuset_cpus=getattr(lifecycle, "cpuset_cpus", None),
            started=started,
            stopped=stopped,
        )
    except preparation.AttachmentLimitError as exc:
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY, "the selected files exceed this message's limits", code=exc.code
        ) from exc
    except preparation_helper.HelperUnavailableError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE, "attached files could not be prepared", code="attachments-unavailable"
        ) from exc
    except chat_attachments.AttachmentIntegrityError as exc:
        raise ApiProblem(HTTPStatus.CONFLICT, "Team capabilities changed; retry", code="team-context-changed") from exc
    except team_storage.StorageNotFoundError as exc:
        raise ApiProblem(HTTPStatus.NOT_FOUND, "selected file not found", code="file-not-found") from exc
    except team_storage.StorageError as exc:
        self._raise_storage_problem(exc)
    if self._chat_cancelled(token):
        raise chat_orchestrator.ChatStoppedError("chat turn stopped")
    return chat_attachments.wire(prepared)


def turn_started(self, team_id: str, file_ids: Sequence[str]) -> tuple[str, ...]:
    """Record that a new turn references these files, before its Brain start can (ADR-0093).

    Until that turn completes, the Brain thread may still hold the previous attached exchange too, so the files add
    to those already referenced; a referenced file is never collected. Returns the files this turn newly referenced.
    """
    if not file_ids:
        return ()
    try:
        return self.storage.reference(team_id, file_ids)
    except team_storage.StorageNotFoundError as exc:
        raise ApiProblem(HTTPStatus.NOT_FOUND, "selected file not found", code="file-not-found") from exc
    except team_storage.StorageError as exc:
        self._raise_storage_problem(exc)


def turn_failed(self, team_id: str, added: Sequence[str]) -> None:
    """A turn that ended without an outcome leaves no continuation to read its files: release what it added.

    The files earlier turns referenced stay referenced. A release that fails keeps them referenced, which never
    collects one early; the next completed turn releases them.
    """
    if added:
        with contextlib.suppress(team_storage.StorageError):
            self.storage.release(team_id, added)


def turn_completed(self, team_id: str, file_ids: Sequence[str]) -> None:
    """A completed turn leaves its Brain thread referencing only this turn's files; the others start their grace.

    The reply is already committed, so a release that fails keeps every file referenced, which never collects one
    early, and the next completed turn releases them.
    """
    with contextlib.suppress(team_storage.StorageError):
        self.storage.settle(team_id, file_ids)


@contextmanager
def deletion_slot(self, team_id: str) -> Iterator[None]:
    """Hold the Team's one execution slot, so no turn delivers or reads a file while one is deleted."""
    lock = self._chat_lock(team_id)
    if not lock.acquire(blocking=False):
        with self._active_chat_guard:
            routine = team_id in self._routine_holders
        if routine:
            raise ApiProblem(HTTPStatus.CONFLICT, "Team is running a Routine", code="routine-active")
        raise ApiProblem(HTTPStatus.CONFLICT, "Team already has an active chat turn", code="chat-active")
    try:
        yield
    finally:
        lock.release()


def _pending_files(self, team_id: str) -> tuple[str, ...] | None:
    """The files a paused turn of the Team selected, or None when no turn is paused."""
    for store in (self.human_challenges, self.integration_challenges):
        current = store.current(team_id)
        if current is not None:
            # A paused turn whose state is unreadable is treated as referencing every file.
            return tuple(getattr(current.payload, "file_ids", ("*",)))
    return ("*",) if self.chat_continuations.current(team_id) is not None else None


def forget_file(self, team_id: str, file_id: str, network: object) -> None:
    """Invalidate everything that could still deliver or show a file about to be deleted (ADR-0093).

    Held under the Team's execution slot: a paused turn that selected the file, or any paused turn when the Brain
    thread must be purged, is cancelled with its challenges and continuation, keeping uncertain Action evidence; the
    Brain thread is deleted when it may reference the file, which releases every file it referenced.
    """
    pending = _pending_files(self, team_id)
    purge = file_id in self.storage.referenced(team_id)
    referenced = pending is not None and ("*" in pending or file_id in pending)
    if not purge and not referenced:
        return
    if pending is not None:
        self.integration_challenges.cancel_team(team_id)
        self.human_challenges.cancel_team(team_id)
        self.oauth_pkce.cancel_team(team_id)
        self._delete_chat_continuation(team_id)
        try:
            self.action_state.end_settled(network.id)
        except action_journal.ActionJournalError as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Team Action execution state is unavailable",
                code="action-state-unavailable",
            ) from exc
    try:
        self.brain_runtime.delete_thread(_brain_thread_id(self.space_id, team_id, network.id))
    except brain_runtime_client.BrainRuntimeError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team conversation state could not be deleted",
            code="brain-runtime-failed",
        ) from exc
    self.storage.settle(team_id, ())
