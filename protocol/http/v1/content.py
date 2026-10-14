"""A Team file's original: the framing of the bytes Team sends a Supervisor's browser to download (ADR-0093).

Team sends a listed file's stored bytes only after they matched their stored digest, as an octet stream of exactly
``length`` bytes whose SHA-256 is ``sha256``; Admin verifies both before it sends a byte on.
"""

from __future__ import annotations

import re

if __package__:
    from . import payload
else:  # The protocol verifier runs every module of this directory flat.
    import payload

MAX_CONTENT_BYTES = payload.MAX_FILE_UPLOAD_BYTES
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def canonical_framing(value: object) -> dict[str, object] | None:
    """Return exactly ``{"length", "sha256"}`` of an admissible file content response, or None."""
    if not isinstance(value, dict) or set(value) != {"length", "sha256"}:
        return None
    length, digest = value["length"], value["sha256"]
    if type(length) is not int or not 1 <= length <= MAX_CONTENT_BYTES:
        return None
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        return None
    return value
