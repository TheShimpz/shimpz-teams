"""Chat attachment rules both profiles share (ADR-0093).

Team alone reads the selected files, prepares them for the message, and rehydrates them for every segment of the
logical turn; Brain receives them only as request-local content. While any attachment's text or image is in the turn,
only Actions that declare an authorization capability are admitted, so file content can leave only through an Action a
person authorizes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence

from assistant.spec import ActionSpec
from inference import client as brain_runtime_client
from prepare import service as preparation
from protocol.assistant.v1 import input_file_validator

READABLE = frozenset({"text", "image"})


class AttachmentIntegrityError(RuntimeError):
    """A selected file no longer has the bytes its turn bound."""


def runtime_action(action_id: str, action: ActionSpec) -> brain_runtime_client.RuntimeAction:
    """One Action exactly as a Brain turn sees it, with its authorization and file-input declarations."""
    return brain_runtime_client.RuntimeAction(
        id=action_id,
        summary=action.summary,
        input_schema=action.input_schema,
        authorization=any(kind in input_file_validator.AUTHORIZATION_REQUESTS for kind in action.human_requests),
        input_files=tuple(action.input_files),
    )


def stored_files(
    files: Sequence[Mapping[str, object]],
    read: Callable[[str], tuple[Mapping[str, object], bytes]],
) -> list[preparation.StoredFile]:
    """Selected files that read their verified bytes lazily, refusing any whose digest no longer matches the turn."""

    def reader(metadata: Mapping[str, object]) -> Callable[[], bytes]:
        def load() -> bytes:
            stored, data = read(str(metadata["id"]))
            if stored.get("sha256") != metadata["sha256"] or stored.get("size") != metadata["size"]:
                raise AttachmentIntegrityError("a selected file changed during its turn")
            return data

        return load

    return [
        preparation.StoredFile(str(item["id"]), str(item["name"]), int(item["size"]), str(item["sha256"]), reader(item))
        for item in files
    ]


def wire(attachments: Iterable[preparation.Attachment]) -> tuple[dict[str, object], ...]:
    """The exact private Brain representation of prepared attachments."""
    return tuple(item.wire() for item in attachments)


def reads_content(attachments: Iterable[Mapping[str, object]]) -> bool:
    """Whether any attachment's text or image content is in the turn's provider projection."""
    return any(
        isinstance(item.get("content"), Mapping) and item["content"].get("type") in READABLE for item in attachments
    )


def admitted_actions(
    context: brain_runtime_client.RuntimeContext,
) -> dict[tuple[str, str], brain_runtime_client.RuntimeAction]:
    """The Actions a turn may run: every declared Action, or only authorizing ones while attachment content is in it."""
    restricted = reads_content(context.attachments)
    return {
        (assistant.id, action.id): action
        for assistant in context.assistants
        for action in assistant.actions
        if not restricted or action.authorization
    }
