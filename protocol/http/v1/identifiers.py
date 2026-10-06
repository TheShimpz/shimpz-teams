"""Closed Team, Assistant, Action, and Assistant-declared identifiers of the Team HTTP protocol.

Team ids and Action ids are Team's own. An Assistant id and the identifier of an Assistant's Integration, provider, or
Stored Input follow the Developers published-Assistant protocol (`assistantIdentifier` and `identifier`): one grammar
with two bounds.
"""

from __future__ import annotations

import re

TEAM_ID_PATTERN = r"^[a-z0-9_]{1,40}$"
IDENTIFIER_PATTERN = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
ASSISTANT_ID_PATTERN = IDENTIFIER_PATTERN
ACTION_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
MAX_ASSISTANT_ID_CHARS = 40
MAX_IDENTIFIER_CHARS = 64
MAX_ACTION_ID_CHARS = 128

TEAM_ID_RE = re.compile(TEAM_ID_PATTERN)
IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)
ASSISTANT_ID_RE = IDENTIFIER_RE
ACTION_ID_RE = re.compile(ACTION_ID_PATTERN)


def _bounded(value: object, pattern: re.Pattern[str], maximum: int) -> str | None:
    if not isinstance(value, str) or len(value) > maximum or pattern.fullmatch(value) is None:
        return None
    return value


def canonical_team_id(value: object) -> str | None:
    return value if isinstance(value, str) and TEAM_ID_RE.fullmatch(value) is not None else None


def canonical_assistant_id(value: object) -> str | None:
    return _bounded(value, ASSISTANT_ID_RE, MAX_ASSISTANT_ID_CHARS)


def canonical_identifier(value: object) -> str | None:
    """Return one Integration, provider, or Stored Input identifier, or None."""
    return _bounded(value, IDENTIFIER_RE, MAX_IDENTIFIER_CHARS)


def canonical_action_id(value: object) -> str | None:
    return _bounded(value, ACTION_ID_RE, MAX_ACTION_ID_CHARS)
