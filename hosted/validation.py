"""Allowlist validation for team — runs BEFORE any Docker or postgresql-service call.

Nothing here touches Docker; it only decides yes/no and returns a validated team id the caller
(app.py) turns into container/network/volume/DB names. Same shape as the other runtime validators
modules — the actual security boundary, not the client that acts on its output.
"""

from __future__ import annotations

import re

from protocol.http.v1 import payload as http_payload


class ValidationError(Exception):
    """A team request failed the allowlist — nothing was touched."""


def sanitize(name: str) -> str:
    lowered = re.sub(r"[^a-z0-9_]+", "_", str(name).lower())
    return lowered.strip("_")


# The id becomes the DB project "team_<id>"; Postgres identifiers are 63 bytes and dbname/role are
# "proj_team_" + this, so the Team HTTP protocol caps it well under the limit. It also names the
# container/network/volumes, so it stays in the Docker-safe [a-z0-9_] set.
def validate_team_id(name: object) -> str:
    if not isinstance(name, str) or not name:
        raise ValidationError(f"team id must be a non-empty string: {name!r}")
    sanitized = sanitize(name)
    if not sanitized or not http_payload.TEAM_ID_RE.match(sanitized):
        raise ValidationError(f"team id sanitizes to empty or invalid: {name!r} -> {sanitized!r}")
    return sanitized


MAX_CHAT_MESSAGE = 16000


def validate_hosted_conversation(conversation: object) -> None:
    """Require an empty chat conversation window on Hosted.

    Hosted has no server-derived committed presentation history and Store relays browser frames, so a
    browser-supplied history never reaches the Brain.
    """
    if conversation != []:
        raise ValidationError("Hosted Team chat requires an empty conversation window")


def validate_chat_message(message: object) -> str:
    """A user-to-Assistant chat message: non-empty text, size-bounded."""
    if not isinstance(message, str):
        raise ValidationError("message must be a string")
    text = message.strip()
    if not text:
        raise ValidationError("message must be non-empty")
    if len(text) > MAX_CHAT_MESSAGE:
        raise ValidationError(f"message too long (> {MAX_CHAT_MESSAGE} chars)")
    return text
