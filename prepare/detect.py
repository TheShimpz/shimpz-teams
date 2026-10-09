"""Determine an attachment's kind and media type from its bytes; names and uploaded types grant nothing."""

import codecs
import json
from dataclasses import dataclass
from pathlib import PurePosixPath

from prepare import limits

TEXT = "text"
PDF = "pdf"
IMAGE = "image"
OPAQUE = "opaque"
OCTET_STREAM = "application/octet-stream"

_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)
# A Markdown or CSV extension only names a text subtype; the bytes must still be valid text.
_TEXT_EXTENSIONS = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}
_UTF8_BOM = b"\xef\xbb\xbf"
# Tab, line feed, form feed, and carriage return are the only control characters admitted in text.
_TEXT_CONTROLS = frozenset({"\t", "\n", "\f", "\r"})


@dataclass(frozen=True, slots=True)
class Detected:
    """A file's kind and the media type Team determined from its bytes."""

    kind: str
    media_type: str


def detect(name: str, data: bytes) -> Detected:
    """Classify bytes as an image, a PDF, UTF-8 text, or opaque."""
    for signature, media_type in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return Detected(IMAGE, media_type)
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return Detected(IMAGE, "image/webp")
    if data.startswith(b"%PDF-"):
        return Detected(PDF, "application/pdf")
    if _text_prefix(data):
        return Detected(TEXT, _text_media_type(name, data))
    return Detected(OPAQUE, OCTET_STREAM)


def _text_prefix(data: bytes) -> bool:
    """Whether the bytes read as text, deciding from at most the text source ceiling without decoding more.

    A longer file classifies as text from its prefix and is then refused for size, so no oversized text is decoded.
    """
    prefix = data[: limits.MAX_TEXT_SOURCE_BYTES]
    final = len(data) <= limits.MAX_TEXT_SOURCE_BYTES
    body = prefix.removeprefix(_UTF8_BOM)
    if not body:
        return False
    try:
        text = codecs.getincrementaldecoder("utf-8")().decode(body, final=final)
    except UnicodeDecodeError:
        return False
    return _printable(text)


def decode_text(data: bytes) -> str | None:
    """Return the text of strict UTF-8 bytes, with an optional BOM, or None when the bytes are not text."""
    body = data.removeprefix(_UTF8_BOM)
    if not body:
        return None
    if len(data) > limits.MAX_TEXT_SOURCE_BYTES:
        return None
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if _printable(text) else None


def _printable(text: str) -> bool:
    return not any(ord(character) < 32 and character not in _TEXT_CONTROLS for character in text) and "\x7f" not in text


def _text_media_type(name: str, data: bytes) -> str:
    media_type = _TEXT_EXTENSIONS.get(PurePosixPath(name).suffix.lower(), "text/plain")
    if media_type == "application/json" and not _valid_json(data):
        return "text/plain"
    return media_type


def _valid_json(data: bytes) -> bool:
    if len(data) > limits.MAX_TEXT_SOURCE_BYTES:
        return False
    try:
        json.loads(data.removeprefix(_UTF8_BOM))
    except UnicodeDecodeError, ValueError, RecursionError:
        return False
    return True
