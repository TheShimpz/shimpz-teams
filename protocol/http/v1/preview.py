"""A Team file's preview: the framing of the sanitized rendition Team sends a Supervisor's browser (ADR-0093).

The rendition is the bounded image derivative a chat turn sends the model, never the original: a JPEG or PNG of at most
the derivative's bytes, of an image or of one page of a PDF. Team and Admin each admit its exact media type and length,
and its bytes start with that media type's signature; a page's rendition also names its document's page count.
"""

from __future__ import annotations

import re

if __package__:
    from . import turn
else:  # The protocol verifier runs every module of this directory flat.
    import turn

MAX_PREVIEW_BYTES = turn.MAX_ATTACHMENT_IMAGE_BYTES
SIGNATURES = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}
# A PDF has a page preview only within its page ceiling (Team prepare/limits.py); pages count from 1.
MAX_PDF_PAGES = 50
PAGE_RE = re.compile(r"[1-9][0-9]?")
PAGE_COUNT_HEADER = "X-Page-Count"


def canonical_framing(value: object) -> dict[str, object] | None:
    """Return exactly ``{"media_type", "length"}`` of an admissible preview response, or None."""
    if not isinstance(value, dict) or set(value) != {"media_type", "length"}:
        return None
    media_type, length = value["media_type"], value["length"]
    if not isinstance(media_type, str) or media_type not in SIGNATURES:
        return None
    if type(length) is not int or not 1 <= length <= MAX_PREVIEW_BYTES:
        return None
    return value


def canonical_page(value: object) -> int | None:
    """Return the page number a page preview path names, from 1 to MAX_PDF_PAGES, or None."""
    if not isinstance(value, str) or PAGE_RE.fullmatch(value) is None or int(value) > MAX_PDF_PAGES:
        return None
    return int(value)


def canonical_page_framing(value: object) -> dict[str, object] | None:
    """Return exactly ``{"media_type", "length", "page_count"}`` of an admissible page preview response, or None."""
    if not isinstance(value, dict) or set(value) != {"media_type", "length", "page_count"}:
        return None
    count = value["page_count"]
    if type(count) is not int or not 1 <= count <= MAX_PDF_PAGES:
        return None
    if canonical_framing({"media_type": value["media_type"], "length": value["length"]}) is None:
        return None
    return value
