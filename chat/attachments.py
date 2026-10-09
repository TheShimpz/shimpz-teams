"""Chat attachment rules both profiles share (ADR-0093).

Team alone reads the selected files, prepares them for the message, and rehydrates them for every segment of the
logical turn; Brain receives them only as request-local content. While any attachment's text or image is in the turn,
only Actions that declare an authorization capability are admitted, so file content can leave only through an Action a
person authorizes.
"""

import contextlib
from collections.abc import Callable, Iterable, Mapping, Sequence

from assistant.spec import ActionSpec
from inference import client as brain_runtime_client
from prepare import service as preparation
from protocol.assistant.v1.validators import input_file as input_file_validator
from protocol.http.v1 import payload as http_payload
from storage import files as team_storage

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
        output_schema=action.output_schema,
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


def restricted_actions(context: brain_runtime_client.RuntimeContext) -> dict[str, object] | None:
    """The selected Actions this turn withheld because readable attachment content was in it, or None (ADR-0093).

    Derived from the pinned contracts the turn offered, so a resumed turn reports what its final segment withheld; a
    turn whose attachments were all opaque withheld nothing.
    """
    if not reads_content(context.attachments):
        return None
    withheld = sorted(
        {
            (assistant.id, action.id)
            for assistant in context.assistants
            for action in assistant.actions
            if not action.authorization
        }
    )
    if not withheld:
        return None
    total = min(len(withheld), http_payload.MAX_RESTRICTED_ACTION_TOTAL)
    listed = withheld[: http_payload.MAX_RESTRICTED_ACTIONS]
    while True:
        # The first withheld Actions that fit the byte bound are named; the total still counts every one.
        restricted = http_payload.canonical_restricted_actions(
            {"actions": [{"assistant": assistant, "action": action} for assistant, action in listed], "total": total}
        )
        if restricted is not None or len(listed) == 1:
            return restricted
        listed = listed[:-1]


# A selected file's retention follows the turns that reference it (ADR-0093); each profile keeps its own failures,
# challenge cancellation, Action evidence, and Brain thread identity around these rules.
def release_failed_turn(storage: Callable[[], team_storage.TeamStorage], team_id: str, added: Sequence[str]) -> None:
    """A turn that ended without an outcome leaves no continuation to read its files: release what it added.

    The files earlier turns referenced stay referenced. A release that fails, including an unavailable storage that
    ``storage`` opens, keeps them referenced, which never collects one early; the next completed turn releases them.
    """
    if added:
        with contextlib.suppress(team_storage.StorageError):
            storage().release(team_id, added)


def settle_completed_turn(
    storage: Callable[[], team_storage.TeamStorage], team_id: str, file_ids: Sequence[str]
) -> None:
    """A completed turn leaves its Brain thread referencing only this turn's files; the others start their grace.

    The reply is already committed, so a release that fails, including an unavailable storage that ``storage`` opens,
    keeps every file referenced, which never collects one early, and the next completed turn releases them.
    """
    with contextlib.suppress(team_storage.StorageError):
        storage().settle(team_id, file_ids)


def paused_files(team_id: str, challenge_stores: Iterable[object]) -> tuple[str, ...] | None:
    """The files the Team's paused turn selected, from the first store holding one, or None when none is paused."""
    for store in challenge_stores:
        current = store.current(team_id)
        if current is not None:
            # A paused turn whose state is unreadable is treated as referencing every file.
            return tuple(getattr(current.payload, "file_ids", ("*",)))
    return None


def forget_required(
    storage: team_storage.TeamStorage, team_id: str, file_id: str, pending: tuple[str, ...] | None
) -> bool:
    """Whether deleting a file must first purge the Brain thread or a paused turn that may still deliver it."""
    referenced = file_id in storage.referenced(team_id)
    return referenced or (pending is not None and ("*" in pending or file_id in pending))
