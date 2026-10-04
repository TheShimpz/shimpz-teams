"""Prepare one message's selected files into the private Brain ``attachments`` field (ADR-0093).

Text, code, CSV, JSON, and Markdown are validated and read in the controller with the standard library only. Images
and PDFs go to the networkless helper, whose answers are untrusted and re-validated here. A file beyond a per-file
ceiling is opaque with a closed reason; a message beyond a per-message ceiling is refused before dispatch.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass

from prepare import detect, limits
from prepare.helper import PreparationHelper

_IMAGE_MAGIC = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}
_SOURCE_LIMITS = {
    detect.TEXT: limits.MAX_TEXT_SOURCE_BYTES,
    detect.PDF: limits.MAX_PDF_BYTES,
    detect.IMAGE: limits.MAX_IMAGE_BYTES,
}
_MAX_SOURCE_BYTES = max(_SOURCE_LIMITS.values())
# How often a turn waiting for its controller's preparation admission notices that it was stopped.
ADMISSION_POLL_SECONDS = 0.1


class AttachmentLimitError(ValueError):
    """The message's attachments exceed a per-message ceiling; the message is refused before dispatch."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredFile:
    """One selected Team file; ``read`` returns its bytes after the storage reader verifies length and digest.

    Bytes are read one file at a time and dropped after preparation, so at most one original is held in memory.
    """

    id: str
    name: str
    size: int
    sha256: str
    read: Callable[[], bytes]


@dataclass(frozen=True, slots=True)
class Attachment:
    """One prepared attachment: literal metadata plus one closed text, image, or opaque content branch."""

    id: str
    name: str
    media_type: str
    size: int
    sha256: str
    content: dict[str, object]

    def wire(self) -> dict[str, object]:
        """The exact private Brain representation of this attachment."""
        return {
            "id": self.id,
            "name": self.name,
            "media_type": self.media_type,
            "size": self.size,
            "sha256": self.sha256,
            "content": dict(self.content),
        }


@contextmanager
def admission(slots: threading.Lock | threading.Semaphore, interrupt: Callable[[], None]) -> Iterator[None]:
    """Hold one of a controller's preparation slots, leaving the wait as soon as ``interrupt`` raises."""
    while not slots.acquire(timeout=ADMISSION_POLL_SECONDS):
        interrupt()
    try:
        yield
    finally:
        slots.release()


def prepare_attachments(
    files: Sequence[StoredFile],
    helper: Callable[[], AbstractContextManager[PreparationHelper]],
    admission: AbstractContextManager[object] | None = None,
    interrupt: Callable[[], None] = lambda: None,
) -> tuple[Attachment, ...]:
    """Prepare every selected file, starting the helper only when an image or PDF needs it.

    ``admission`` is held before the first original is read and until the helper is gone, so a profile can bound how
    many originals and derivatives its controller holds at once. ``interrupt`` raises once the turn is stopped and runs
    before every file, so a stopped turn reads nothing further. An original larger than every readable type's source
    ceiling is never read: it is opaque without allocating its bytes.
    """
    if len(files) > limits.MAX_SELECTED_FILES:
        raise AttachmentLimitError("attachments-too-many")
    if sum(item.size for item in files) > limits.MAX_SELECTED_ORIGINAL_BYTES:
        raise AttachmentLimitError("attachments-too-large")
    prepared: list[Attachment] = []
    with ExitStack() as stack:
        if admission is not None:
            stack.enter_context(admission)
        session: list[PreparationHelper] = []

        def running() -> PreparationHelper:
            if not session:
                session.append(stack.enter_context(helper()))
            return session[0]

        for item in files:
            interrupt()
            if item.size > _MAX_SOURCE_BYTES:
                prepared.append(
                    Attachment(item.id, item.name, detect.OCTET_STREAM, item.size, item.sha256, _opaque("too_large"))
                )
                continue
            data = item.read()
            found = detect.detect(item.name, data)
            content = _content(data, found, running)
            prepared.append(Attachment(item.id, item.name, found.media_type, item.size, item.sha256, content))
            del data
    admit_message(prepared)
    return tuple(prepared)


def admit_message(attachments: Sequence[Attachment]) -> None:
    """Refuse a message whose readable attachments together exceed a per-message ceiling."""
    images = [item for item in attachments if item.content["type"] == "image"]
    texts = [str(item.content["text"]) for item in attachments if item.content["type"] == "text"]
    if len(images) > limits.MAX_MESSAGE_IMAGES:
        raise AttachmentLimitError("attachments-too-many-images")
    if (
        sum(len(text) for text in texts) > limits.MAX_MESSAGE_TEXT_CHARACTERS
        or sum(len(text.encode("utf-8")) for text in texts) > limits.MAX_MESSAGE_TEXT_BYTES
    ):
        raise AttachmentLimitError("attachments-too-much-text")
    encoded = json.dumps([item.wire() for item in attachments], ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > limits.MAX_ATTACHMENTS_FIELD_BYTES:
        raise AttachmentLimitError("attachments-too-large")


def _content(
    data: bytes,
    found: detect.Detected,
    running: Callable[[], PreparationHelper],
) -> dict[str, object]:
    if found.kind == detect.OPAQUE:
        return _opaque("unsupported")
    if len(data) > _SOURCE_LIMITS[found.kind]:
        return _opaque("too_large")
    if found.kind == detect.TEXT:
        return _text(detect.decode_text(data), pdf=False)
    answer = running().prepare(found.kind, data)
    if found.kind == detect.PDF:
        return _pdf_answer(answer)
    return _image_answer(answer)


def _text(text: str | None, *, pdf: bool) -> dict[str, object]:
    if not isinstance(text, str) or not text.strip():
        return _opaque("no_text" if pdf else "unreadable")
    if len(text) > limits.MAX_TEXT_CHARACTERS or len(text.encode("utf-8")) > limits.MAX_TEXT_BYTES:
        return _opaque("too_large")
    return {"type": "text", "text": text, "pdf": pdf}


def _pdf_answer(answer: dict[str, object]) -> dict[str, object]:
    if set(answer) == {"type", "text"} and answer["type"] == "text":
        return _text(answer["text"] if isinstance(answer["text"], str) else None, pdf=True)
    return _opaque_answer(answer)


def _image_answer(answer: dict[str, object]) -> dict[str, object]:
    if set(answer) != {"type", "media_type", "width", "height", "base64"} or answer["type"] != "image":
        return _opaque_answer(answer)
    media_type, width, height = answer["media_type"], answer["width"], answer["height"]
    encoded = _decoded(answer["base64"])
    if (
        media_type not in _IMAGE_MAGIC
        or type(width) is not int
        or type(height) is not int
        or not 1 <= width <= limits.DERIVATIVE_LONG_EDGE
        or not 1 <= height <= limits.DERIVATIVE_LONG_EDGE
        or width * height > limits.DERIVATIVE_PIXELS
        or encoded is None
        or not encoded.startswith(_IMAGE_MAGIC[media_type])
    ):
        return _opaque("unreadable")
    return {
        "type": "image",
        "media_type": media_type,
        "width": width,
        "height": height,
        "base64": answer["base64"],
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _decoded(value: object) -> bytes | None:
    if not isinstance(value, str) or len(value) > 4 * -(-limits.MAX_DERIVATIVE_BYTES // 3):
        return None
    try:
        data = base64.b64decode(value, validate=True)
    except binascii.Error, ValueError:
        return None
    if len(data) > limits.MAX_DERIVATIVE_BYTES or base64.b64encode(data).decode("ascii") != value:
        return None
    return data


def _opaque_answer(answer: dict[str, object]) -> dict[str, object]:
    reason = answer.get("reason")
    if set(answer) == {"type", "reason"} and answer["type"] == "opaque" and reason in limits.OPAQUE_REASONS:
        return _opaque(str(reason))
    return _opaque("unreadable")


def _opaque(reason: str) -> dict[str, object]:
    return {"type": "opaque", "reason": reason}
