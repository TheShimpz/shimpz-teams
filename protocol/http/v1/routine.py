"""Canonical Team Routine wire forms (ADR-0086): schedules, timezones, and their closed bounds."""

from __future__ import annotations

import copy
import re
from fractions import Fraction

MAX_ROUTINES = 8
MAX_ROUTINE_QUOTE_CHARS = 500
MAX_DAILY_RUNS = 24
MAX_NOTICE_REPLY_CHARS = 16_000
MAX_NOTICE_QUESTION_CHARS = 240
MAX_NOTICE_ACTIONS = 16
MAX_NOTICE_ASSISTANTS = 16
OUTCOMES = frozenset({"done", "failed", "denied", "uncertain", "stopped", "skipped", "scope-changed", "needs-input"})
# The same identifier grammar as payload.py; protocol modules stay independent, and a Team test pins the equality.
ASSISTANT_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
ACTION_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*\Z")
ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
SCHEDULE_KINDS = frozenset({"hourly", "daily", "weekly", "monthly"})
ROUTINE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
# An IANA zone name such as "UTC" or "America/Argentina/Buenos_Aires"; Team also requires that it loads.
TIMEZONE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,31}(?:/[A-Za-z0-9][A-Za-z0-9_+-]{0,31}){0,2}\Z")
_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_FIELDS = {
    "hourly": frozenset({"kind", "every"}),
    "daily": frozenset({"kind", "time"}),
    "weekly": frozenset({"kind", "weekday", "time"}),
    "monthly": frozenset({"kind", "day", "time"}),
}


def _whole(value: object, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _wall_clock(value: dict[str, object]) -> bool:
    kind = value["kind"]
    return (
        isinstance(value["time"], str)
        and _TIME_RE.fullmatch(value["time"]) is not None
        and (kind != "weekly" or _whole(value["weekday"], 0, 6))
        and (kind != "monthly" or _whole(value["day"], 1, 28))
    )


def canonical_schedule(value: object) -> dict[str, object] | None:
    """The exact schedule, or None: hourly every 1..24 hours, daily, weekly (0 = Monday), or monthly on day 1..28."""
    kind = value.get("kind") if isinstance(value, dict) else None
    if not isinstance(kind, str) or kind not in SCHEDULE_KINDS or set(value) != _FIELDS[kind]:
        return None
    valid = _whole(value["every"], 1, 24) if value["kind"] == "hourly" else _wall_clock(value)
    return dict(value) if valid else None


def canonical_timezone(value: object) -> str | None:
    return value if isinstance(value, str) and TIMEZONE_RE.fullmatch(value) is not None else None


def daily_rate(schedule: dict[str, object]) -> Fraction:
    """The average runs per day a canonical schedule fires; a Team's Routines may sum to at most MAX_DAILY_RUNS."""
    kind = schedule["kind"]
    if kind == "hourly":
        return Fraction(24, schedule["every"])
    return {"daily": Fraction(1), "weekly": Fraction(1, 7), "monthly": Fraction(1, 28)}[kind]


def _text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and value.strip() == value


def _actions(value: object) -> bool:
    """Bounded Assistant and Action identities only: a notice never carries an Action's input or result."""
    return (
        isinstance(value, list)
        and len(value) <= MAX_NOTICE_ACTIONS
        and all(
            isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(part, str) for part in pair)
            and ASSISTANT_ID_RE.fullmatch(pair[0]) is not None
            and ACTION_ID_RE.fullmatch(pair[1]) is not None
            for pair in value
        )
    )


def _detail_valid(outcome: str, detail: dict[str, object]) -> bool:
    fields = set(detail)
    if outcome == "done":
        return fields == {"reply"} and _text(detail["reply"], MAX_NOTICE_REPLY_CHARS)
    if outcome == "needs-input":
        return fields == {"question"} and _text(detail["question"], MAX_NOTICE_QUESTION_CHARS)
    if outcome == "skipped":
        return fields == {"missed"} and type(detail["missed"]) is int and detail["missed"] >= 1
    if outcome == "scope-changed":
        assistants = detail.get("assistants")
        return (
            fields == {"assistants"}
            and isinstance(assistants, list)
            and 0 < len(assistants) <= MAX_NOTICE_ASSISTANTS
            and all(isinstance(item, str) and ASSISTANT_ID_RE.fullmatch(item) is not None for item in assistants)
        )
    if outcome == "failed":
        code = detail.get("code")
        return (
            fields == {"code", "actions"}
            and isinstance(code, str)
            and ERROR_CODE_RE.fullmatch(code) is not None
            and _actions(detail["actions"])
        )
    # denied, stopped, and uncertain name the Actions that completed or whose effects are unknown.
    return fields == {"actions"} and _actions(detail["actions"]) and (outcome != "uncertain" or bool(detail["actions"]))


def canonical_notice_detail(outcome: object, detail: object) -> dict[str, object] | None:
    """The exact closed detail for a run outcome, or None; never an Action's raw input or result."""
    if not isinstance(outcome, str) or outcome not in OUTCOMES or not isinstance(detail, dict):
        return None
    return copy.deepcopy(detail) if _detail_valid(outcome, detail) else None
