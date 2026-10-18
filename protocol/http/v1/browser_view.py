"""The Browser screen view a Supervisor watches while a chat turn's Browser task runs (ADR-0117, M2 Watch).

Opening a view answers the running task's random `view_id`; a frame request names that id and a cursor, the number
of the last frame the viewer has, in its path. Team answers a frame only while the very task it opened the view for
still runs, so a viewer never receives another task's screen. A frame is a PNG Team encoded itself from the display's
raw pixels, never image bytes the Browser produced; its number travels in `FRAME_HEADER`. Only Team and Admin consume
this module.
"""

from __future__ import annotations

import re

VIEW_ID_RE = re.compile(r"[0-9a-f]{32}")
# A cursor is the decimal number of a frame, 0 before the first; frames count from 1.
CURSOR_RE = re.compile(r"0|[1-9][0-9]{0,9}")
MAX_FRAME_NUMBER = 2**31 - 1
# A 1280x800 RGB frame stored uncompressed still fits, so no frame Team encodes is ever refused for its size.
MAX_FRAME_BYTES = 4 * 1024 * 1024
FRAME_HEADER = "X-Shimpz-Browser-Frame"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# The closed reasons a view or a frame is refused.
NOT_RUNNING = "browser-not-running"
BUSY = "browser-view-busy"
UNAVAILABLE = "browser-view-unavailable"


def canonical_view(value: object) -> dict[str, object] | None:
    """Return exactly ``{"view_id"}`` of an opened view, or None."""
    if not isinstance(value, dict) or set(value) != {"view_id"}:
        return None
    view_id = value["view_id"]
    if not isinstance(view_id, str) or VIEW_ID_RE.fullmatch(view_id) is None:
        return None
    return value


def canonical_cursor(value: object) -> int | None:
    """Return the frame number a frame path names as its cursor, or None."""
    if not isinstance(value, str) or CURSOR_RE.fullmatch(value) is None or int(value) > MAX_FRAME_NUMBER:
        return None
    return int(value)


def canonical_frame(value: object) -> dict[str, object] | None:
    """Return exactly ``{"frame", "length"}`` of an admissible frame response, or None."""
    if not isinstance(value, dict) or set(value) != {"frame", "length"}:
        return None
    frame, length = value["frame"], value["length"]
    if type(frame) is not int or not 1 <= frame <= MAX_FRAME_NUMBER:
        return None
    if type(length) is not int or not len(PNG_SIGNATURE) <= length <= MAX_FRAME_BYTES:
        return None
    return value
