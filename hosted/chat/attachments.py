"""Hosted hydration of one chat segment's selected files into request-local Brain content (ADR-0093)."""

from collections.abc import Mapping, Sequence
from http import HTTPStatus

from chat import attachments as chat_attachments
from hosted import prepare as hosted_prepare
from hosted import state as runtime_state
from hosted.assistant import runtime as hosted_assistants
from prepare import helper as preparation_helper
from prepare import service as preparation
from storage import files as team_storage


def turn_attachments(
    team_id: str,
    token: str,
    owner: str,
    files: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Prepare the selected files again for this segment; the helper is gone before the Brain is asked."""
    if not files:
        return ()
    storage = runtime_state._storage()

    def interrupt() -> None:
        if runtime_state._token_cancelled(token):
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "brain turn stopped")

    try:
        prepared = hosted_prepare.prepare_attachments(
            chat_attachments.stored_files(files, lambda file_id: storage.get(team_id, file_id)),
            team_id=team_id,
            owner=owner,
            # Stop reaches a helper exactly as it reaches an Action workload: the chat's one active container slot.
            started=lambda container: hosted_assistants._register_active_action(team_id, token, container),
            stopped=lambda container: hosted_assistants._release_active_action(team_id, token, container.id),
            interrupt=interrupt,
        )
    except preparation.AttachmentLimitError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.UNPROCESSABLE_ENTITY, f"the selected files exceed this message's limits ({exc.code})"
        ) from exc
    except preparation_helper.HelperUnavailableError as exc:
        raise runtime_state.ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "attached files could not be prepared") from exc
    except chat_attachments.AttachmentIntegrityError as exc:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Team capabilities changed; retry") from exc
    except team_storage.StorageNotFoundError as exc:
        raise runtime_state.ApiError(HTTPStatus.NOT_FOUND, "selected file not found") from exc
    except team_storage.StorageError as exc:
        raise runtime_state.ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Team storage failed its safety checks") from exc
    interrupt()
    return chat_attachments.wire(prepared)
