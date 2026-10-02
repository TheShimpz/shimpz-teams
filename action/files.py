"""Team-brokered delivery of one selected file to an Action that declares a file input (ADR-0093).

The model names a file only by the opaque id of a file the current logical turn selected. The first invocation carries
the file's metadata with its content withheld; only the replay whose transcript holds the Action's own declared
authorization response carries the original bytes, read from Team storage and checked against the turn's digest.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from action import human as action_human
from protocol.assistant.v1.validators import input_file as input_file_validator

# Two distinct file-taking logical Actions per turn bound unique granted bytes to 16 MiB; the turn's 16 human requests
# already bound byte deliveries to 16 and 128 MiB cumulatively.
MAX_FILE_ACTIONS_PER_TURN = 2
FILE_RPC_TIMEOUT_SECONDS = 60
_FILE_RPC_SLOT = threading.BoundedSemaphore(1)


class FileDeliveryError(RuntimeError):
    """An Action named a file the turn did not select, or the selected file no longer has its turn's bytes."""


@dataclass(frozen=True, slots=True)
class ActionFile:
    """The selected file one invocation of a file-taking Action carries, exactly as the turn bound it."""

    id: str
    name: str
    media_type: str
    size: int
    sha256: str

    def metadata(self) -> dict[str, object]:
        """The closed metadata both the invocation and the authorization disclosure carry."""
        return {
            "id": self.id,
            "name": self.name,
            "media_type": self.media_type,
            "size": self.size,
            "sha256": self.sha256,
        }


def selected(attachments: Iterable[Mapping[str, object]]) -> dict[str, ActionFile]:
    """The files a turn selected, by id, with the media type Team determined while preparing them."""
    return {
        str(item["id"]): ActionFile(
            str(item["id"]),
            str(item["name"]),
            str(item["media_type"]),
            int(item["size"]),
            str(item["sha256"]),
        )
        for item in attachments
    }


def action_file(
    input_files: Iterable[str],
    action_input: Mapping[str, object],
    files: Mapping[str, ActionFile],
) -> ActionFile | None:
    """The one selected file a file-taking Action's declared input names, or None for an ordinary Action."""
    names = tuple(input_files)
    if not names:
        return None
    file_id = action_input.get(names[0])
    file = files.get(file_id) if isinstance(file_id, str) else None
    if file is None:
        raise FileDeliveryError("the Action names a file this turn did not select")
    if (
        not 1 <= file.size <= input_file_validator.MAX_FILE_BYTES
        or not input_file_validator.valid_name(file.name)
        or len(file.media_type) > input_file_validator.MAX_MEDIA_TYPE
        or input_file_validator.MEDIA_TYPE.fullmatch(file.media_type) is None
    ):
        raise FileDeliveryError("the selected file cannot be delivered to an Action")
    return file


def disclosure(
    input_files: Iterable[str],
    action_input: Mapping[str, object],
    files: Mapping[str, ActionFile],
    kind: str,
) -> dict[str, object] | None:
    """The file an authorization request of a file-taking Action discloses; no other request discloses one."""
    if kind not in action_human.AUTHORIZATION_KINDS:
        return None
    file = action_file(input_files, action_input, files)
    return None if file is None else file.metadata()


def commitments(file: ActionFile | None) -> list[dict[str, object]]:
    """The file commitments of an operation fingerprint: always present, ``[]`` for an ordinary Action."""
    return [] if file is None else [file.metadata()]


def authorized(human_requests: Iterable[str], transcript: action_human.ActionTranscript) -> bool:
    """Whether the transcript holds the response to the one authorization the Action declares."""
    declared = [kind for kind in human_requests if kind in action_human.AUTHORIZATION_KINDS]
    return len(declared) == 1 and any(response.kind == declared[0] for response in transcript.responses)


def invocation_files(
    file: ActionFile | None,
    granted: bool,
    read: Callable[[str], tuple[Mapping[str, object], bytes]],
) -> dict[str, object]:
    """The invocation's ``files``: metadata with withheld content, or the verified original bytes once granted."""
    if file is None:
        return {}
    content: dict[str, object] = dict(input_file_validator.WITHHELD)
    if granted:
        stored, data = read(file.id)
        if (
            stored.get("sha256") != file.sha256
            or stored.get("size") != file.size
            or len(data) != file.size
            or hashlib.sha256(data).hexdigest() != file.sha256
        ):
            raise FileDeliveryError("the selected file no longer has its turn's bytes")
        content = {"type": input_file_validator.DELIVERED, "base64": base64.b64encode(data).decode("ascii")}
    record = file.metadata()
    record.pop("id")
    return {file.id: {**record, "content": content}}


def deliver(
    action_spec: object,
    file: ActionFile | None,
    transcript: action_human.ActionTranscript,
    action_input: Mapping[str, object],
    read: Callable[[str], tuple[Mapping[str, object], bytes]],
) -> dict[str, object]:
    """The ``files`` of one invocation, refused unless the reference validator admits it for this exact Action.

    A file-taking Action outside a turn that selected its file therefore never runs, and bytes travel only with the
    Action's own admitted authorization response.
    """
    if file is None and not action_spec.input_files:
        return {}
    files = invocation_files(file, authorized(action_spec.human_requests, transcript), read)
    declaration = {
        "input_files": list(action_spec.input_files),
        "input_schema": action_spec.input_schema,
        "human_requests": list(action_spec.human_requests),
    }
    invocation = {
        "input": dict(action_input),
        "files": files,
        "responses": [dict(response) for response in transcript.payloads()],
    }
    error = input_file_validator.invocation_files_error(declaration, invocation)
    if error is not None:
        raise FileDeliveryError(f"the Action file invocation is refused: {error}")
    return files


def delivered(files: Mapping[str, object]) -> ActionFile | None:
    """The one file whose original bytes an invocation's ``files`` deliver, for audit by id and size only."""
    for file_id, record in files.items():
        if record["content"]["type"] == input_file_validator.DELIVERED:
            return ActionFile(file_id, record["name"], record["media_type"], record["size"], record["sha256"])
    return None


class FileRpcBusyError(RuntimeError):
    """Another delivery held the one file-RPC slot past this delivery's whole deadline; nothing was journaled."""


class FileRpcCancelledError(RuntimeError):
    """The turn was stopped while its delivery waited for the file-RPC slot; nothing was journaled."""


_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("action_file_deadline", default=None)
_SLOT_POLL_SECONDS = 0.25


@contextmanager
def admitted(
    file: ActionFile | None,
    human_requests: Iterable[str],
    transcript: action_human.ActionTranscript,
    cancelled: Callable[[], bool],
) -> Iterator[None]:
    """Hold the one file-RPC slot for an invocation that will deliver bytes, before its execution is journaled.

    The slot is admitted before any byte is read and before the journal begins the attempt, so a delivery refused
    here leaves the journal exactly as it was. The wait observes Stop, and the wait and the exchange share one
    deadline (ADR-0093). An invocation that delivers nothing holds nothing.
    """
    if file is None or not authorized(human_requests, transcript):
        yield
        return
    deadline = time.monotonic() + FILE_RPC_TIMEOUT_SECONDS
    while not _FILE_RPC_SLOT.acquire(timeout=max(0.0, min(_SLOT_POLL_SECONDS, deadline - time.monotonic()))):
        if cancelled():
            raise FileRpcCancelledError("the turn was stopped while its file delivery waited")
        if time.monotonic() >= deadline:
            raise FileRpcBusyError("another file-bearing Action is still running")
    if cancelled():
        # Stop may win while the wait succeeds; the slot is released before anything is journaled.
        _FILE_RPC_SLOT.release()
        raise FileRpcCancelledError("the turn was stopped while its file delivery waited")
    token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)
        _FILE_RPC_SLOT.release()


def rpc_timeout(files: Mapping[str, object], ordinary: float) -> float:
    """The exchange deadline of one invocation: the remainder of its admitted delivery, or the ordinary one.

    Delivered bytes outside an admitted slot are refused, so no path can dispatch them unbounded.
    """
    if not input_file_validator.delivers_content({"files": files}):
        return ordinary
    deadline = _DEADLINE.get()
    if deadline is None:
        raise FileDeliveryError("delivered file content was not admitted to the file-RPC slot")
    return max(0.0, deadline - time.monotonic())
