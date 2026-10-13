"""A Team file's preview: the framing of the one sanitized rendition Team sends a Supervisor's browser (ADR-0093).

The rendition is the bounded image derivative a chat turn sends the model, never the original: a JPEG or PNG of at most
the derivative's bytes. Team and Admin each admit its exact media type and length, and its bytes start with that media
type's signature.
"""

from __future__ import annotations

if __package__:
    from . import turn
else:  # The protocol verifier runs every module of this directory flat.
    import turn

MAX_PREVIEW_BYTES = turn.MAX_ATTACHMENT_IMAGE_BYTES
SIGNATURES = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}


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
