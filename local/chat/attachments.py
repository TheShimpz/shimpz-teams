"""Local custody of a turn's selected files: Brain hydration and the references a turn holds (ADR-0093)."""

from collections.abc import Mapping, Sequence
from http import HTTPStatus

from chat import attachments as chat_attachments
from chat import orchestrator as chat_orchestrator
from local import prepare as local_prepare
from local.errors import ApiProblemError as ApiProblem
from local.errors import selected_file_not_found, team_context_changed
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

    def interrupt() -> None:
        if self._chat_cancelled(token):
            raise chat_orchestrator.ChatStoppedError("chat turn stopped")

    lifecycle = self.assistant_lifecycle
    try:
        prepared = local_prepare.prepare_attachments(
            lifecycle.client,
            chat_attachments.stored_files(files, lambda file_id: self.storage.get(team_id, file_id)),
            space_id=self.space_id,
            team_id=team_id,
            cpuset_cpus=getattr(lifecycle, "cpuset_cpus", None),
            stop=local_prepare.TurnStop(started=started, stopped=stopped, interrupt=interrupt),
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
        raise team_context_changed() from exc
    except team_storage.StorageNotFoundError as exc:
        raise selected_file_not_found() from exc
    except team_storage.StorageError as exc:
        self._raise_storage_problem(exc)
    interrupt()
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
        raise selected_file_not_found() from exc
    except team_storage.StorageError as exc:
        self._raise_storage_problem(exc)


def turn_failed(self, team_id: str, added: Sequence[str]) -> None:
    chat_attachments.release_failed_turn(self.storage, team_id, added)


def turn_completed(self, team_id: str, file_ids: Sequence[str]) -> None:
    chat_attachments.settle_completed_turn(self.storage, team_id, file_ids)
