"""The Browser screen a Supervisor watches while a chat turn's Browser task runs (ADR-0117, watching over KasmVNC).

Opening a view answers the running task's random `view_id`. A stream request names that id in its path and upgrades to
one WebSocket that Team carries to the Browser's display, only while the very task it opened the view for still runs,
so a viewer never receives another task's screen. The upgrade has one exact form, the same toward Team and toward the
display: WebSocket version 13, a 16-byte key, the `binary` subprotocol, and no extension. Only Team and Admin consume
this module.
"""

from __future__ import annotations

import base64
import hashlib
import re

VIEW_ID_RE = re.compile(r"[0-9a-f]{32}")
WEBSOCKET_VERSION = "13"
SUBPROTOCOL = "binary"
KEY_RE = re.compile(r"[A-Za-z0-9+/]{22}==")
# RFC 6455's fixed GUID, which the accept value hashes with the key.
ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# The closed reasons a view or a stream is refused.
NOT_RUNNING = "browser-not-running"
BUSY = "browser-view-busy"
UNAVAILABLE = "browser-view-unavailable"
INVALID_UPGRADE = "browser-view-upgrade-invalid"


def canonical_view(value: object) -> dict[str, object] | None:
    """Return exactly ``{"view_id"}`` of an opened view, or None."""
    if not isinstance(value, dict) or set(value) != {"view_id"}:
        return None
    view_id = value["view_id"]
    if not isinstance(view_id, str) or VIEW_ID_RE.fullmatch(view_id) is None:
        return None
    return value


def canonical_key(value: object) -> str | None:
    """Return a WebSocket key that encodes exactly 16 bytes, or None."""
    if not isinstance(value, str) or KEY_RE.fullmatch(value) is None:
        return None
    # The grammar admits only base64 that decodes to 16 bytes; only its canonical encoding passes, with no stray low
    # bits in the last character.
    decoded = base64.b64decode(value, validate=True)
    return value if base64.b64encode(decoded).decode("ascii") == value else None


def accept(key: str) -> str:
    """The `Sec-WebSocket-Accept` value that answers `key` (RFC 6455 §4.2.2); SHA-1 there protects nothing."""
    digest = hashlib.sha1((key + ACCEPT_GUID).encode("ascii"), usedforsecurity=False).digest()
    return base64.b64encode(digest).decode("ascii")
