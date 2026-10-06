"""Closed Team, Assistant, and Action identifiers of the Team HTTP protocol."""

from __future__ import annotations

import re

TEAM_ID_PATTERN = r"^[a-z0-9_]{1,40}$"
ASSISTANT_ID_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
ACTION_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"

TEAM_ID_RE = re.compile(TEAM_ID_PATTERN)
ASSISTANT_ID_RE = re.compile(ASSISTANT_ID_PATTERN)
ACTION_ID_RE = re.compile(ACTION_ID_PATTERN)


def canonical_team_id(value: object) -> str | None:
    return value if isinstance(value, str) and TEAM_ID_RE.fullmatch(value) is not None else None


def canonical_assistant_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 80 or ASSISTANT_ID_RE.fullmatch(value) is None:
        return None
    return value


def canonical_action_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 128 or ACTION_ID_RE.fullmatch(value) is None:
        return None
    return value
