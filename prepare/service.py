"""Prepare one message's selected files into the private Brain ``attachments`` field (ADR-0093).

Text, code, CSV, JSON, and Markdown are validated and read in the controller with the standard library only. Images
and PDFs go to the networkless helper, whose answers are untrusted and re-validated here. A file beyond a per-file
ceiling is opaque with a closed reason; a message beyond a per-message ceiling is refused before dispatch.
"""

import base64
import binascii
import hashlib
import json
import threading
import time
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


class AdmissionBusyError(RuntimeError):
    """The preparation slot is not free in time: too many requests already wait for it, or the wait ran out."""


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
class Rendition:
    """One stored image's sanitized rendition: the bounded JPEG or PNG derivative a turn sends, never the original."""

    media_type: str
    width: int
    height: int
    data: bytes


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


class BoundedAdmission:
    """Admission into a controller's preparation slot for a request that must answer promptly.

    At most ``queue`` requests wait at once, each at most ``deadline`` seconds; a request beyond either bound is busy
    instead of waiting indefinitely, and ``interrupt`` leaves the wait as soon as it raises.
    """

    def __init__(
        self,
        slots: threading.Lock | threading.Semaphore,
        *,
        queue: int,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._slots = slots
        self._queue = queue
        self._deadline = deadline
        self._clock = clock
        self._guard = threading.Lock()
        self._waiting = 0

    @contextmanager
    def admit(self, interrupt: Callable[[], None]) -> Iterator[None]:
        """Hold the slot, or raise AdmissionBusyError when it is not free within the bounds."""
        with self._guard:
            if self._waiting >= self._queue:
                raise AdmissionBusyError("the preparation queue is full")
            self._waiting += 1
        try:
            ends = self._clock() + self._deadline
            while not self._slots.acquire(timeout=ADMISSION_POLL_SECONDS):
                interrupt()
                if self._clock() >= ends:
                    raise AdmissionBusyError("the preparation slot stayed busy")
        finally:
            with self._guard:
                self._waiting -= 1
        try:
            yield
        finally:
            self._slots.release()


def prepare_rendition(
    item: StoredFile,
    helper: Callable[[], AbstractContextManager[PreparationHelper]],
    admission: AbstractContextManager[object],
    interrupt: Callable[[], None] = lambda: None,
) -> Rendition | None:
    """One stored file's sanitized rendition through the same boundary as a turn's images, or None when it has none.

    Only a JPEG, PNG, or static WebP that the networkless helper reads within the image limits has one, and its kind
    comes from the bytes, never the uploaded type. An original beyond the image source ceiling is never read, the slot
    is held from before the original is read until the helper is gone, and the helper's answer is validated here as
    a turn's is. ``interrupt`` raises once the request is ended and runs before the read, the helper, and the answer.
    """
    if item.size > limits.MAX_IMAGE_BYTES:
        return None
    with ExitStack() as stack:
        stack.enter_context(admission)
        interrupt()
        data = item.read()
        found = detect.detect(item.name, data)
        if found.kind != detect.IMAGE or len(data) > limits.MAX_IMAGE_BYTES:
            return None
        interrupt()
        answer = stack.enter_context(helper()).prepare(found.kind, data)
        del data
    interrupt()
    content = _image_answer(answer)
    if content["type"] != "image":
        return None
    return Rendition(
        str(content["media_type"]),
        int(content["width"]),
        int(content["height"]),
        base64.b64decode(str(content["base64"]), validate=True),
    )


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
        or encoded_size(media_type, encoded) != (width, height)
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


# A JPEG frame header (SOF0 to SOF15, except the DHT, JPG, and DAC markers) carries the image's encoded size.
_JPEG_FRAMES = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
# Markers that stand alone, without a length: TEM and RST0 to RST7.
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})


def encoded_size(media_type: str, data: bytes) -> tuple[int, int] | None:
    """The width and height a JPEG or PNG declares in its own header, read without decoding it, or None.

    The helper's answer is untrusted, so its reported size must be the size its bytes make a decoder allocate.
    """
    if media_type == "image/png":
        if len(data) < 24 or data[8:16] != b"\x00\x00\x00\x0dIHDR":
            return None
        return int.from_bytes(data[16:20]), int.from_bytes(data[20:24])
    offset = 2
    while offset + 4 <= len(data) and data[offset] == 0xFF:
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        if marker in _JPEG_STANDALONE:
            offset += 2
            continue
        if marker == 0xDA:
            return None
        if marker in _JPEG_FRAMES:
            if offset + 9 > len(data):
                return None
            return int.from_bytes(data[offset + 7 : offset + 9]), int.from_bytes(data[offset + 5 : offset + 7])
        offset += 2 + int.from_bytes(data[offset + 2 : offset + 4])
    return None


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
