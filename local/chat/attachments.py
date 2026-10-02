"""Local hydration of one chat segment's selected files into request-local Brain content (ADR-0093)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from http import HTTPStatus

from chat import attachments as chat_attachments
from chat import orchestrator as chat_orchestrator
from local import prepare as local_prepare
from local.errors import ApiProblemError as ApiProblem
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
