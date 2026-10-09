"""Canonical unpadded base64url: the segment encoding of Team bearer assertions and OAuth PKCE challenges."""

import base64


class NonCanonicalError(ValueError):
    """The text decodes, but it is not the one canonical encoding of its bytes."""


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode(encoded: str) -> bytes:
    """Decode canonical unpadded base64url, raising ValueError when malformed and NonCanonicalError otherwise."""
    if not encoded.isascii() or "=" in encoded:
        raise ValueError("base64url text is malformed")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except ValueError as exc:
        raise ValueError("base64url text is malformed") from exc
    if encode(raw) != encoded:
        raise NonCanonicalError("base64url text is not canonical")
    return raw
