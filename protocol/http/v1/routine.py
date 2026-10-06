"""Canonical Team Routine wire forms (ADR-0086, ADR-0101): schedules, timezones, bounds, and the views Admin admits."""

from __future__ import annotations

import copy
import datetime
import json
import math
import re
import unicodedata
from collections.abc import Callable
from fractions import Fraction

if __package__:
    from . import identifiers, payload
else:  # The protocol verifier runs every module of this directory flat.
    import identifiers
    import payload

MAX_ROUTINES = 8
MAX_ROUTINE_NAME_CHARS = 80
# The ordered Actions of a recorded plan (ADR-0092 section 3, ADR-0101): one Action may repeat.
MAX_ROUTINE_STEPS = 256
# A Team's starts in any rolling 24 hours, and the bound on the sum of its Routines' caps (ADR-0092 section 9).
MAX_DAILY_RUNS = 1000
# A continuous Routine starts its next run this long, at least, after the previous one ended; at most a day.
MIN_CONTINUOUS_GAP_SECONDS = 5
MAX_CONTINUOUS_GAP_SECONDS = 86_400
# The healthy runs a continuous Routine can end in one minute bucket, since each next run starts its gap after.
MAX_ROLLUP_RUNS = 60 // MIN_CONTINUOUS_GAP_SECONDS
MAX_NOTICE_ACTIONS = 16
MAX_NOTICE_ASSISTANTS = 16
# The Actions one decision turn may call at most: its decision allowance, at most 64 (ADR-0101 section 6.4).
MAX_DECISION_CALLS = 64
# The extra Actions a recording may permit for a decision, and the lifetime bound of a Routine's permitted Actions:
# every recorded call and every extra one, which grants never exceed (ADR-0101 section 4.3).
MAX_DECIDE_ACTIONS = 32
MAX_PERMITTED = MAX_ROUTINE_STEPS + MAX_DECIDE_ACTIONS
OUTCOMES = frozenset(
    {
        "done",
        "healthy",
        "recovered",
        "rehearsed",
        "held",
        "paused",
        "user-skipped",
        "failed",
        "denied",
        "stopped",
        "skipped",
        "scope-changed",
        "frozen",
        "created",
        "changed",
        "deleted",
    }
)
# Outcomes of the Routine itself, never of a run: they carry no run id. ``skipped`` reports missed firings; a person
# setting a held run aside through its card's Rodar, or by deleting its Routine, is the run outcome ``user-skipped``,
# whose ``choice`` says which. ``healthy`` rolls up a continuous Routine's healthy runs that ended in one minute,
# starting at the notice's instant (ADR-0092 section 9). ``deleted`` closes the Routine's timeline (ADR-0101).
ROUTINE_OUTCOMES = ("skipped", "scope-changed", "created", "changed", "healthy", "deleted")
# Why a held run's Routine was paused (ADR-0092): the recovery decision, a decision that could not be made, the spent
# recovery budget, a Team-detected policy fault such as a secret echo or an invalid frame, or recovery evidence that
# could not be read.
PAUSE_REASONS = ("decided", "unavailable", "exhausted", "policy", "evidence")
ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
# The interface languages: the same closed set as payload.CHAT_LOCALES, pinned equal by a Team test.
LOCALES = frozenset({"ar", "de", "en", "es", "fr", "ja", "pt", "zh"})
SCHEDULE_KINDS = frozenset({"hourly", "daily", "weekly", "monthly", "continuous"})
ROUTINE_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
# An IANA zone name such as "UTC" or "America/Argentina/Buenos_Aires"; Team also requires that it loads.
TIMEZONE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]{0,31}(?:/[A-Za-z0-9][A-Za-z0-9_+-]{0,31}){0,2}\Z")
_TIME_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")
_FIELDS = {
    "hourly": frozenset({"kind", "every"}),
    "daily": frozenset({"kind", "time"}),
    "weekly": frozenset({"kind", "weekday", "time"}),
    "monthly": frozenset({"kind", "day", "time"}),
    "continuous": frozenset({"kind", "gap", "cap"}),
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
    """The exact schedule, or None.

    Hourly every 1..24 hours, daily, weekly (0 = Monday), monthly on day 1..28, or continuous: ``gap`` seconds (5 to
    86,400) after each run ends, and at most ``cap`` (1 to 1,000) starts in any rolling 24 hours.
    """
    kind = value.get("kind") if isinstance(value, dict) else None
    if not isinstance(kind, str) or kind not in SCHEDULE_KINDS or set(value) != _FIELDS[kind]:
        return None
    if kind == "continuous":
        valid = _whole(value["gap"], MIN_CONTINUOUS_GAP_SECONDS, MAX_CONTINUOUS_GAP_SECONDS) and _whole(
            value["cap"], 1, MAX_DAILY_RUNS
        )
    elif kind == "hourly":
        valid = _whole(value["every"], 1, 24)
    else:
        valid = _wall_clock(value)
    return dict(value) if valid else None


def canonical_name(value: object) -> str | None:
    """A Routine's short name: NFC, one line, no control characters, trimmed, 1 to 80 characters."""
    return _line(value, MAX_ROUTINE_NAME_CHARS)


def _line(value: object, maximum: int) -> str | None:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= maximum
        or unicodedata.normalize("NFC", value) != value
        or value.strip() != value
        or any(
            unicodedata.category(character)[0] == "C" or unicodedata.category(character) in {"Zl", "Zp"}
            for character in value
        )
    ):
        return None
    return value


def canonical_timezone(value: object) -> str | None:
    return value if isinstance(value, str) and TIMEZONE_RE.fullmatch(value) is not None else None


def daily_rate(schedule: dict[str, object]) -> Fraction:
    """The runs per day a canonical schedule allows, its cap for a continuous one.

    A Team's Routines may sum to at most MAX_DAILY_RUNS.
    """
    kind = schedule["kind"]
    if kind == "continuous":
        return Fraction(schedule["cap"])
    if kind == "hourly":
        return Fraction(24, schedule["every"])
    return {"daily": Fraction(1), "weekly": Fraction(1, 7), "monthly": Fraction(1, 28)}[kind]


def daily_cap(schedule: dict[str, object]) -> int:
    """The most runs a Routine may start in any rolling 24 hours: its cap, or its schedule's whole daily rate."""
    return math.ceil(daily_rate(schedule))


def _assistant(value: object) -> bool:
    return identifiers.canonical_assistant_id(value) is not None


def _action(value: object) -> bool:
    return identifiers.canonical_action_id(value) is not None


def _team(value: object) -> bool:
    return identifiers.canonical_team_id(value) is not None


def _actions(value: object) -> bool:
    """Bounded Assistant and Action identities only: a notice never carries an Action's input or result."""
    return (
        isinstance(value, list)
        and len(value) <= MAX_NOTICE_ACTIONS
        and all(
            isinstance(pair, list) and len(pair) == 2 and _assistant(pair[0]) and _action(pair[1]) for pair in value
        )
    )


def _identity(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _scope_changed(detail: dict[str, object]) -> bool:
    assistants = detail["assistants"]
    return (
        isinstance(assistants, list)
        and 0 < len(assistants) <= MAX_NOTICE_ASSISTANTS
        and all(_assistant(item) for item in assistants)
    )


# Where one Action call stands in a run (ADR-0101 section 10): a replay step by its 1-based position among its plan's
# steps, or a decision turn's call by its 1-based order. Notices, incidents, freezes, cards, and run records use it.
PHASES = ("replay", "decision")


def _position(value: object, maximum: int = MAX_ROUTINE_STEPS) -> bool:
    return type(value) is int and 1 <= value <= maximum


def canonical_position(value: object, steps: object) -> dict[str, object] | None:
    """A call's position: a replay step among ``steps``, or a decision call; or None."""
    if not isinstance(value, dict) or not _whole(steps, 0, MAX_ROUTINE_STEPS):
        return None
    phase = value.get("phase")
    if phase == "replay" and set(value) == {"phase", "step"} and _position(value["step"], steps):
        return {"phase": "replay", "step": value["step"]}
    if phase == "decision" and set(value) == {"phase", "call"} and _position(value["call"], MAX_DECISION_CALLS):
        return {"phase": "decision", "call": value["call"]}
    return None


# A Routine's plan as a Supervisor inspects it (ADR-0092): each step's Action, every input's source, and the Stored
# Inputs its Action uses by name only. A literal shows as a bounded preview of its JSON text; plan admission already
# refuses a literal where a secret belongs, and a Stored Input's value never enters a plan. On the wire a step is named
# by its 1-based position, never its internal id, a reference names its earlier step so, and a plan is read page by
# page, bound to its revision (ADR-0092 amendment, 2026-10-05, scale). A reference through an array item names the
# item's selecting member and its constant as JSON text, and the item's own pointer (ADR-0101).
MAX_PREVIEW_CHARS = 120
MAX_STEP_INPUTS = 64
MAX_STEP_STORED_INPUTS = 8
MAX_MEMBER_CHARS = 128
MAX_POINTER_CHARS = 256
# A selector's constant as JSON text: a string or an integer the request named, which the plan admits whole.
MAX_WHERE_CHARS = 4096
# One projected step's encoded size at most: plan admission refuses a step whose projection outgrows it, never cuts it.
MAX_STEP_VIEW_BYTES = 24 * 1024
# A page of projected steps: whole steps only, at most this many and this many encoded bytes, so one always fits.
MAX_PAGE_STEPS = 64
MAX_PAGE_BYTES = 96 * 1024
CLOCK_FORMATS = frozenset({"date"})
PLAN_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_POINTER_RE = re.compile(r"(?:/(?:[^/~]|~[01])*)*\Z")
# A lone surrogate is unsafe too: it has no UTF-8 encoding, so it is escaped in a preview and refused elsewhere.
_PLAN_UNSAFE_RE = re.compile(r"[\u0000-\u001f\u007f-\u009f​-‏ -‮⁠-⁯\ud800-\udfff﻿]")
_INPUT_FIELDS = {
    "literal": frozenset({"member", "source", "value"}),
    "run_clock": frozenset({"member", "source", "value"}),
    "step_output": frozenset({"member", "source", "step", "pointer", "where", "item"}),
}


def escaped(text: str) -> str:
    """Text with every control or invisible character, and a lone surrogate, written as its JSON unicode escape."""
    return _PLAN_UNSAFE_RE.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def literal_preview(value: object) -> str:
    """A literal's JSON text, with every control or invisible character escaped, cut to 120 characters."""
    text = escaped(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return text if len(text) <= MAX_PREVIEW_CHARS else text[: MAX_PREVIEW_CHARS - 1] + "…"


def _plain(value: object, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and _PLAN_UNSAFE_RE.search(value) is None


def _pointer(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= MAX_POINTER_CHARS
        and _POINTER_RE.fullmatch(value) is not None
        and _PLAN_UNSAFE_RE.search(value) is None
    )


def _where(value: object) -> bool:
    """A selector as shown: its member and its constant as JSON text, a string or an integer."""
    if not isinstance(value, dict) or set(value) != {"member", "value_json"}:
        return False
    text = value["value_json"]
    if not _plain(value["member"], MAX_MEMBER_CHARS) or not _plain(text, MAX_WHERE_CHARS):
        return False
    try:
        constant = json.loads(text)
    except ValueError:
        return False
    return (isinstance(constant, str) or type(constant) is int) and where_text(constant) == text


def where_text(constant: str | int) -> str:
    """A selector constant as shown: its JSON text with every control or invisible character escaped."""
    return escaped(json.dumps(constant, ensure_ascii=False))


def _reference(value: dict[str, object], position: int) -> bool:
    """A copied value: an earlier position and its pointer, and through an array item its selector and item pointer."""
    selected = value["where"] is not None
    return (
        _position(value["step"], position - 1)
        and _pointer(value["pointer"])
        and (_where(value["where"]) and _pointer(value["item"]) if selected else value["item"] is None)
    )


def _input(value: object, position: int) -> bool:
    source = value.get("source") if isinstance(value, dict) else None
    if not isinstance(source, str) or source not in _INPUT_FIELDS or set(value) != _INPUT_FIELDS[source]:
        return False
    if not _plain(value["member"], MAX_MEMBER_CHARS):
        return False
    if source == "literal":
        return _plain(value["value"], MAX_PREVIEW_CHARS)
    if source == "run_clock":
        return isinstance(value["value"], str) and value["value"] in CLOCK_FORMATS
    return _reference(value, position)


def _members(items: object, admit: Callable[[object], bool]) -> bool:
    """A step's inputs: at most MAX_STEP_INPUTS, each admitted, in member order with no member twice."""
    return (
        isinstance(items, list)
        and len(items) <= MAX_STEP_INPUTS
        and all(admit(item) for item in items)
        and [item["member"] for item in items] == sorted({item["member"] for item in items})
    )


def canonical_step(value: object, position: int) -> dict[str, object] | None:
    """One projected step at exactly ``position``, referring only to earlier positions, within its byte bound."""
    fields = {"position", "assistant", "action", "read_only", "inputs", "stored_inputs"}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    inputs, stored = value["inputs"], value["stored_inputs"]
    valid = (
        _position(position)
        and value["position"] == position
        and type(value["position"]) is int
        and _assistant(value["assistant"])
        and _action(value["action"])
        and type(value["read_only"]) is bool
        and _members(inputs, lambda item: _input(item, position))
        and isinstance(stored, list)
        and len(stored) <= MAX_STEP_STORED_INPUTS
        and all(identifiers.canonical_identifier(item) is not None for item in stored)
        and stored == sorted(set(stored))
        and encoded_bytes(value) <= MAX_STEP_VIEW_BYTES
    )
    return copy.deepcopy(value) if valid else None


def _paged(value: dict[str, object], step: Callable[[object, int], object]) -> bool:
    """Whole consecutive entries of ``total`` from ``offset``, the next offset or null, within the page's bounds.

    A total of zero has exactly one page: offset zero, no entries, and no next one.
    """
    total, offset, steps = value["total"], value["offset"], value["steps"]
    if total == 0 and type(total) is int:
        return offset == 0 and type(offset) is int and steps == [] and value["next"] is None
    return (
        _whole(total, 1, MAX_ROUTINE_STEPS + MAX_DECISION_CALLS)
        and type(offset) is int
        and 0 <= offset < total
        and isinstance(steps, list)
        and 0 < len(steps) <= min(MAX_PAGE_STEPS, total - offset)
        and all(step(item, offset + index + 1) is not None for index, item in enumerate(steps))
        and value["next"] == (None if offset + len(steps) == total else offset + len(steps))
        and encoded_bytes(steps) <= MAX_PAGE_BYTES
    )


def canonical_page(value: object) -> dict[str, object] | None:
    """One page of a revision's projected steps, naming the revision and digest so no reader mixes two revisions."""
    fields = {"routine_id", "revision", "plan_digest", "total", "offset", "steps", "next"}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    valid = (
        _identity(value["routine_id"], ROUTINE_ID_RE)
        and _revision(value["revision"])
        and _identity(value["plan_digest"], PLAN_DIGEST_RE)
        and _whole(value["total"], 0, MAX_ROUTINE_STEPS)
        and _paged(value, canonical_step)
    )
    return copy.deepcopy(value) if valid else None


# A revision's summary, which list views and notices carry instead of steps: its digest, step count, and Actions as at
# most 16 runs of consecutive equal Actions; ``more`` counts the steps after them. A decision plan may have none.
MAX_SUMMARY_RUNS = 16


def canonical_summary(value: object) -> dict[str, object] | None:
    """A plan summary: its runs cover the first steps in order, each differs from the one before, and none is empty."""
    if not isinstance(value, dict) or set(value) != {"revision", "plan_digest", "steps", "actions", "more"}:
        return None
    runs, more, total = value["actions"], value["more"], value["steps"]
    valid = (
        _revision(value["revision"])
        and _identity(value["plan_digest"], PLAN_DIGEST_RE)
        and _whole(total, 0, MAX_ROUTINE_STEPS)
        and isinstance(runs, list)
        and (0 < len(runs) <= MAX_SUMMARY_RUNS if total else runs == [])
        and all(
            isinstance(run, list) and len(run) == 3 and _assistant(run[0]) and _action(run[1]) and _position(run[2])
            for run in runs
        )
        and all(runs[index][:2] != runs[index - 1][:2] for index in range(1, len(runs)))
        and type(more) is int
        and more >= 0
        and (more == 0 or len(runs) == MAX_SUMMARY_RUNS)
        and sum(run[2] for run in runs) + more == total
    )
    return copy.deepcopy(value) if valid else None


# What a completed run does with its result (ADR-0092 amendment, 2026-10-05, output; ADR-0101): show one step's result
# after every run, only when it changed, show none of it, or hand every result to a decision turn, ``always`` or only
# when the results changed. show and changes name the shown step, on the wire by its position.
OUTPUT_MODES = ("show", "changes", "none", "decide")
SHOWN_MODES = frozenset({"show", "changes"})
DECISION_WHEN = ("always", "changes")


def canonical_disposition(value: object, total: object) -> dict[str, object] | None:
    """A plan's output disposition, whose shown step is the position of one of its ``total`` steps, or None.

    Only a decision has a condition, and only a decision may have no steps.
    """
    if not isinstance(value, dict) or set(value) != {"mode", "step", "when"} or value["mode"] not in OUTPUT_MODES:
        return None
    mode, shown, when = value["mode"], value["step"], value["when"]
    if not _whole(total, 0 if mode == "decide" else 1, MAX_ROUTINE_STEPS):
        return None
    valid = (_position(shown, total) if mode in SHOWN_MODES else shown is None) and (
        when in DECISION_WHEN if mode == "decide" else when is None
    )
    return {"mode": mode, "step": shown, "when": when} if valid else None


# A shown result (ADR-0092 amendment, 2026-10-05, output): Team's bounded, redacted, ordered projection of one step's
# result, which Admin renders as escaped plain text. Each node is one closed variant; containers nest at most
# MAX_OUTPUT_DEPTH levels, past which a container is elided.
OUTPUT_STATES = ("shown", "unchanged", "unavailable")
MAX_OUTPUT_DEPTH = 4
MAX_OUTPUT_ITEMS = 50
MAX_OUTPUT_FIELDS = 24
MAX_OUTPUT_TEXT_CHARS = 300
MAX_OUTPUT_KEY_CHARS = 64
# A number is its exact JSON text, so no consumer rounds it; a longer one is shown as text.
MAX_OUTPUT_NUMBER_CHARS = 64
_OUTPUT_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
MAX_OUTPUT_BYTES = 16 * 1024


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**31


def _scalar_node(value: dict[str, object]) -> bool:
    """One scalar node: null, redacted, elided, a boolean, a finite number, or escaped text of bounded length."""
    kind, item = value.get("kind"), value.get("value")
    checks = {
        "null": lambda: set(value) == {"kind"},
        "redacted": lambda: set(value) == {"kind"},
        "elided": lambda: set(value) == {"kind"},
        "bool": lambda: set(value) == {"kind", "value"} and type(item) is bool,
        "number": lambda: (
            set(value) == {"kind", "value"}
            and isinstance(item, str)
            and len(item) <= MAX_OUTPUT_NUMBER_CHARS
            and _OUTPUT_NUMBER_RE.fullmatch(item) is not None
        ),
        "text": lambda: (
            set(value) == {"kind", "value", "cut"}
            and type(value["cut"]) is bool
            and _plain_text(item, MAX_OUTPUT_TEXT_CHARS)
        ),
    }
    return isinstance(kind, str) and kind in checks and checks[kind]()


def _plain_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and len(value) <= maximum and _PLAN_UNSAFE_RE.search(value) is None


def _output_node(value: object, depth: int) -> bool:
    if not isinstance(value, dict):
        return False
    kind = value.get("kind")
    if kind not in ("list", "fields"):
        return _scalar_node(value)
    if depth >= MAX_OUTPUT_DEPTH or not _count(value.get("omitted")):
        return False
    if kind == "list":
        items = value.get("items")
        return (
            set(value) == {"kind", "items", "omitted"}
            and isinstance(items, list)
            and len(items) <= MAX_OUTPUT_ITEMS
            and all(_output_node(item, depth + 1) for item in items)
        )
    fields = value.get("fields")
    return (
        set(value) == {"kind", "fields", "omitted"}
        and isinstance(fields, list)
        and len(fields) <= MAX_OUTPUT_FIELDS
        and all(
            isinstance(field, list)
            and len(field) == 2
            and _plain(field[0], MAX_OUTPUT_KEY_CHARS)
            and _output_node(field[1], depth + 1)
            for field in fields
        )
        and len({field[0] for field in fields}) == len(fields)
    )


def canonical_output(value: object) -> dict[str, object] | None:
    """A completed run's result as shown: its step's position and state, and for a shown one its bounded projection."""
    if not isinstance(value, dict) or set(value) != {"step", "state", "value", "truncated"}:
        return None
    state, node = value["state"], value["value"]
    valid = (
        _position(value["step"])
        and state in OUTPUT_STATES
        and type(value["truncated"]) is bool
        and (_output_node(node, 0) if state == "shown" else node is None and value["truncated"] is False)
        and encoded_bytes(value) <= MAX_OUTPUT_BYTES
    )
    return copy.deepcopy(value) if valid else None


# A run's model usage, in the shape of a chat reply's (ADR-0082, ADR-0101): its active duration and, per provider and
# model, the tokens its decision and recovery calls reported; a replay-only run has no model.
canonical_run_usage = payload.canonical_run_usage


# The model a decision turn uses (ADR-0101 section 6.9), frozen at confirmation.
MODEL_PROVIDERS = ("anthropic", "openai")
MODEL_EFFORTS = ("low", "medium", "high")


def canonical_model(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != {"provider", "model", "effort"}:
        return None
    valid = (
        value["provider"] in MODEL_PROVIDERS
        and _identity(value["model"], payload.TURN_USAGE_ID_RE)
        and value["effort"] in MODEL_EFFORTS
    )
    return {key: value[key] for key in ("provider", "model", "effort")} if valid else None


def _model(value: object) -> bool:
    return value is None or canonical_model(value) is not None


def canonical_permitted(value: object) -> dict[str, int] | None:
    """A Routine's permitted Actions as views and notices carry them: how many, and how many change something.

    The card showed the complete set when the person confirmed it.
    """
    if not isinstance(value, dict) or set(value) != {"total", "changes"}:
        return None
    valid = _whole(value["total"], 0, MAX_PERMITTED) and _whole(value["changes"], 0, value["total"])
    return {"total": value["total"], "changes": value["changes"]} if valid else None


# How a decision turn ended (ADR-0101 section 6.8): it decided (with its escaped message when it notified, none when it
# only ends a run that already had a waiting notice), found the results unchanged, could not decide (with its code),
# or hit the Team's decision ceiling.
DECISION_STATES = ("decided", "unchanged", "unavailable", "ceiling")
MAX_DECISION_MESSAGE_CHARS = 4000
MAX_DECISION_RULES = 8
MAX_DECISION_RULE_CHARS = 200
MAX_DECISION_RATIONALE_CHARS = 500
MAX_ALLOWANCE = MAX_DECISION_CALLS
# The code a decision that could not run because the run lost its protection carries (ADR-0101 section 6.2).
PROTECTION_LOST = "routine-protection-lost"


def _decision(value: object) -> bool:
    """A notice's decision: its state, its code when it could not decide, and its message when it decided."""
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) != {"state", "code", "message"}:
        return False
    state, code, message = value["state"], value["code"], value["message"]
    if state == "decided":
        return code is None and (
            message is None or (_plain_text(message, MAX_DECISION_MESSAGE_CHARS) and bool(message))
        )
    if state == "unavailable":
        return _identity(code, ERROR_CODE_RE) and message is None
    return state in DECISION_STATES and code is None and message is None


def canonical_decision_record(value: object) -> dict[str, object] | None:
    """A run's one decision record (ADR-0101 section 7): how its decision turn ended, and what it said and used."""
    fields = {"state", "code", "model", "rules", "rationale", "notify", "usage"}
    if not isinstance(value, dict) or set(value) != fields or value["state"] not in DECISION_STATES:
        return None
    state, rules = value["state"], value["rules"]
    usage = value["usage"]
    quoted = (
        isinstance(rules, list)
        and len(rules) <= MAX_DECISION_RULES
        and all(_plain_text(rule, MAX_DECISION_RULE_CHARS) and rule for rule in rules)
    )
    if state == "decided":
        valid = (
            value["code"] is None
            and canonical_model(value["model"]) is not None
            and quoted
            and (value["rationale"] is None or _plain_text(value["rationale"], MAX_DECISION_RATIONALE_CHARS))
            and type(value["notify"]) is bool
            and canonical_run_usage(usage) is not None
        )
    elif state == "unavailable":
        valid = (
            _identity(value["code"], ERROR_CODE_RE)
            and _model(value["model"])
            and rules == []
            and value["rationale"] is None
            and value["notify"] is None
            and (usage is None or canonical_run_usage(usage) is not None)
        )
    else:
        valid = all(value[key] is None for key in fields - {"state", "rules"}) and rules == []
    return copy.deepcopy(value) if valid else None


def _disposed(value: object, steps: int) -> bool:
    """Exactly one admitted disposition of a plan of ``steps`` steps; null is none."""
    admitted = canonical_disposition(value, steps)
    return admitted is not None and admitted == value


def _scope(value: dict[str, object], steps: int) -> bool:
    """A Routine's standing scope: only a decision has a model and an allowance, which its steps leave room for.

    A Routine that may change anything waits for a rehearsal or has had one, so it can never be new and active
    without one; that history is Team's, so only the closed forms are checked here.
    """
    decide = value["output"]["mode"] == "decide"
    return (
        value["state"] in ROUTINE_STATES
        and canonical_permitted(value["permitted"]) == value["permitted"]
        and (canonical_model(value["model"]) is not None if decide else value["model"] is None)
        and _whole(value["allowance"], 1 if decide else 0, MAX_ALLOWANCE if decide else 0)
        and steps + value["allowance"] <= MAX_ROUTINE_STEPS
    )


def _defined(detail: dict[str, object]) -> bool:
    """What a created or changed Routine does: name, plan summary, disposition, schedule, and its standing scope."""
    summary = canonical_summary(detail["plan"])
    return (
        canonical_name(detail["name"]) == detail["name"]
        and summary is not None
        and _disposed(detail["output"], summary["steps"])
        and canonical_schedule(detail["schedule"]) == detail["schedule"]
        and canonical_timezone(detail["timezone"]) is not None
        and _scope(detail, summary["steps"])
    )


def _completed(detail: dict[str, object]) -> bool:
    """A completed run: its plan's summary, its shown result if any, and its decision."""
    summary = canonical_summary(detail["plan"])
    output = detail["output"]
    return (
        summary is not None
        and (output is None or (canonical_output(output) is not None and output["step"] <= summary["steps"]))
        and _decision(detail["decision"])
    )


def _rehearsed(detail: dict[str, object]) -> bool:
    """A rehearsal: as a completed run, with how many effects it did not run, could not test, or found unpermitted."""
    return _completed(detail) and all(
        _whole(detail[key], 0, MAX_ROUTINE_STEPS + MAX_DECISION_CALLS)
        for key in ("rehearsed", "untested", "not_permitted")
    )


def _held_step(detail: dict[str, object]) -> bool:
    """The call a held run stopped at and its position, or all null when the run sealed no plan before it was held."""
    if detail["assistant_id"] is None:
        return detail["action"] is None and detail["position"] is None and detail["steps"] is None
    return (
        _assistant(detail["assistant_id"])
        and _action(detail["action"])
        and canonical_position(detail["position"], detail["steps"]) is not None
    )


_STEP_FIELDS = {"assistant_id", "action", "position", "steps"}
# What a frozen run waits for: a person's answer to a declared human request, an Integration, or a permission to call
# an Action outside the Routine's permitted set (ADR-0101 section 6.7).
REQUEST_KINDS = ("human", "integrations", "permission")


def _frozen(detail: dict[str, object]) -> bool:
    """The one request a frozen run waits for: its kind, the Assistant Action that asked, and that call's position."""
    return detail["request_kind"] in REQUEST_KINDS and detail["assistant_id"] is not None and _held_step(detail)


def _failed_at(detail: dict[str, object]) -> bool:
    """The call a failed run stopped at, by position, or both null when it failed before any call."""
    if (detail["position"], detail["steps"]) == (None, None):
        return True
    return canonical_position(detail["position"], detail["steps"]) is not None


_COMPLETED_FIELDS = {"plan", "output", "decision"}
_DEFINED_FIELDS = {"name", "plan", "output", "schedule", "timezone", "state", "permitted", "model", "allowance"}
# Each outcome's exact detail fields and check: denied and stopped name the Actions that completed; held, paused, and
# user-skipped name the call whose effects are unresolved, and user-skipped the card choice that set the run aside.
_DETAILS = {
    "done": (_COMPLETED_FIELDS, _completed),
    "recovered": (_COMPLETED_FIELDS, _completed),
    "rehearsed": (_COMPLETED_FIELDS | {"rehearsed", "untested", "not_permitted"}, _rehearsed),
    "held": (_STEP_FIELDS, _held_step),
    "paused": (_STEP_FIELDS | {"reason"}, lambda detail: _held_step(detail) and detail["reason"] in PAUSE_REASONS),
    "user-skipped": (
        _STEP_FIELDS | {"choice"},
        lambda detail: _held_step(detail) and detail["choice"] in CARD_CHOICES,
    ),
    "skipped": ({"missed"}, lambda detail: type(detail["missed"]) is int and detail["missed"] >= 1),
    "healthy": ({"runs"}, lambda detail: type(detail["runs"]) is int and 1 <= detail["runs"] <= MAX_ROLLUP_RUNS),
    "scope-changed": ({"assistants"}, _scope_changed),
    "frozen": ({"request_kind"} | _STEP_FIELDS, _frozen),
    "failed": (
        {"code", "actions", "position", "steps"},
        lambda detail: _identity(detail["code"], ERROR_CODE_RE) and _actions(detail["actions"]) and _failed_at(detail),
    ),
    "denied": ({"actions"}, lambda detail: _actions(detail["actions"])),
    "stopped": ({"actions"}, lambda detail: _actions(detail["actions"])),
    "created": (_DEFINED_FIELDS, _defined),
    "changed": (_DEFINED_FIELDS, _defined),
    "deleted": (set(), lambda _detail: True),
}


def canonical_notice_detail(outcome: object, detail: object) -> dict[str, object] | None:
    """The exact closed detail for a run outcome, or None; never an Action's raw input or result."""
    if not isinstance(outcome, str) or outcome not in OUTCOMES or not isinstance(detail, dict):
        return None
    fields, valid = _DETAILS[outcome]
    return copy.deepcopy(detail) if set(detail) == fields and valid(detail) else None


# Views a Local Team returns to Admin for Routines. Admin admits each only in exactly this closed form.
MAX_NOTICE_BATCH = 1024
# The encoded notice list of one batch, under the Local API's 128 KiB response cap with room for its envelope. The
# largest notice, a completed run's shown output of at most MAX_OUTPUT_BYTES beside its plan summary and a decision
# message, fits many times.
MAX_NOTICE_BATCH_BYTES = 112 * 1024
RUN_STATUSES = frozenset({"leased", "frozen", "held"})
# A Routine's state (ADR-0101 section 5.5): it runs, a person paused it, or it waits for a rehearsal before it can run.
ROUTINE_STATES = ("active", "paused", "rehearsal")
LEASE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_INSTANT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")


def _instant(value: object) -> bool:
    """A real UTC instant in whole seconds, written ``YYYY-MM-DDTHH:MM:SSZ``."""
    if not isinstance(value, str) or _INSTANT_RE.fullmatch(value) is None:
        return False
    try:
        datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def encoded_bytes(value: object) -> int:
    """The UTF-8 size of a value in the Local API's JSON encoding."""
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _assistant_ids(value: object, *, minimum: int) -> bool:
    return (
        isinstance(value, list)
        and minimum <= len(value) <= MAX_NOTICE_ASSISTANTS
        and all(_assistant(item) for item in value)
        and value == sorted(set(value))
    )


def _optional(value: object, pattern: re.Pattern[str]) -> bool:
    return value is None or _identity(value, pattern)


def canonical_routine_view(value: object) -> dict[str, object] | None:
    """One Routine as a Supervisor sees it: its plan summary (steps are paged), disposition, and standing scope."""
    fields = {"routine_id", "name", "schedule", "timezone", "assistant_ids", "next_run_at", "needs_reconfirm"}
    scope = {"state", "permitted", "permissions_revision", "model", "allowance"}
    if not isinstance(value, dict) or set(value) != fields | scope | {"deleting", "plan", "output"}:
        return None
    summary = canonical_summary(value["plan"])
    valid = (
        _identity(value["routine_id"], ROUTINE_ID_RE)
        and canonical_name(value["name"]) == value["name"]
        and summary is not None
        and _disposed(value["output"], summary["steps"])
        and value["schedule"] is not None
        and canonical_schedule(value["schedule"]) == value["schedule"]
        and canonical_timezone(value["timezone"]) is not None
        and _assistant_ids(value["assistant_ids"], minimum=0)
        and _instant(value["next_run_at"])
        and type(value["needs_reconfirm"]) is bool
        and type(value["deleting"]) is bool
        and _whole(value["permissions_revision"], 0, 2**31 - 1)
        and _scope(value, summary["steps"])
    )
    return copy.deepcopy(value) if valid else None


def canonical_run_view(value: object) -> dict[str, object] | None:
    """One live run: a frozen run names the request it waits for, and its call; any other only that it is live."""
    fields = {"run_id", "routine_id", "status", "scheduled_at", "request_kind", "assistant_id", "action"}
    if not isinstance(value, dict) or set(value) != fields | {"position", "steps"}:
        return None
    status = value["status"]
    request = (value["request_kind"], value["assistant_id"], value["action"], value["position"], value["steps"])
    frozen = (
        request[0] in REQUEST_KINDS
        and _assistant(request[1])
        and _action(request[2])
        and canonical_position(request[3], request[4]) is not None
    )
    valid = (
        _identity(value["run_id"], ROUTINE_ID_RE)
        and _identity(value["routine_id"], ROUTINE_ID_RE)
        and _instant(value["scheduled_at"])
        and isinstance(status, str)
        and status in RUN_STATUSES
        and (frozen if status == "frozen" else request == (None, None, None, None, None))
    )
    return copy.deepcopy(value) if valid else None


def canonical_incident_view(value: object) -> dict[str, object] | None:
    """One unresolved incident of a held run, its held call by position; it outlives a deleted Routine."""
    fields = {"incident_id", "routine_id", "name", "created_at", "assistant_id", "action", "position", "steps"}
    if not isinstance(value, dict) or set(value) != fields:
        return None
    valid = (
        _identity(value["incident_id"], ROUTINE_ID_RE)
        and _identity(value["routine_id"], ROUTINE_ID_RE)
        and canonical_name(value["name"]) == value["name"]
        and _instant(value["created_at"])
        and _held_step(value)
    )
    return copy.deepcopy(value) if valid else None


# The unresolved incidents a Team holds at most, which its Routine list carries (ADR-0092).
MAX_UNRESOLVED_INCIDENTS = 32
# A plan summary encoded at most: 16 runs of a Local Team's Assistant id (<= 40 chars), Action id (<= 128), and count.
MAX_SUMMARY_BYTES = 4 * 1024
# A Team's whole Routine list, encoded: the one response above the Local API's 128 KiB cap. Beside its summary, a
# Routine view holds at most 8 KiB, a run view 1 KiB, an incident view 4 KiB, and the envelope 4 KiB, for a Local
# Team's identifiers; steps are paged, never listed.
MAX_ROUTINE_LIST_BYTES = (
    MAX_ROUTINES * (MAX_SUMMARY_BYTES + 8 * 1024 + 1024) + MAX_UNRESOLVED_INCIDENTS * 4 * 1024 + 4 * 1024
)
# A held run's recovery card (ADR-0092 section 7, ADR-0101): exactly Rodar and Excluir, in this order, none
# recommended. Team answers only Rodar; Excluir is the Routine's own confirmed deletion.
CARD_CHOICES = ("run", "delete")
CARD_ANSWERS = ("run",)
CARD_SECONDS = 300
NONCE_RE = re.compile(r"[0-9a-f]{32}\Z")
# What an answer did: Rodar set the held run aside and requested one fresh run.
CARD_STATUSES = {"run": "requested"}
# Whether the held call's failure has a diagnostic: recorded (and shown), none kept, or one that could not be read.
CARD_EVIDENCE = ("recorded", "absent", "unavailable")


def _card_evidence(value: dict[str, object]) -> bool:
    """The held operation's latest diagnostic, exactly when one is recorded, and only of the card's own call."""
    evidence, diagnostic = value["evidence"], value["diagnostic"]
    if evidence not in CARD_EVIDENCE or (diagnostic is None) == (evidence == "recorded"):
        return False
    if diagnostic is None:
        return True
    admitted = canonical_diagnostic(diagnostic)
    return admitted is not None and (admitted["assistant_id"], admitted["action"], admitted["position"]) == (
        value["assistant_id"],
        value["action"],
        value["position"],
    )


def canonical_card(value: object) -> dict[str, object] | None:
    """An opened recovery card: the call it stopped at, its failure as recorded, its one-use nonce, and its choices."""
    fields = {"team_id", "incident_id", "routine_id", "revision", "assistant_id", "action", "nonce", "expires_in"}
    if not isinstance(value, dict) or set(value) != fields | {"position", "steps", "evidence", "diagnostic", "choices"}:
        return None
    valid = (
        _team(value["team_id"])
        and _identity(value["incident_id"], ROUTINE_ID_RE)
        and _identity(value["routine_id"], ROUTINE_ID_RE)
        and _revision(value["revision"])
        and _assistant(value["assistant_id"])
        and _action(value["action"])
        and canonical_position(value["position"], value["steps"]) is not None
        and _card_evidence(value)
        and _identity(value["nonce"], NONCE_RE)
        and value["expires_in"] == CARD_SECONDS
        and type(value["expires_in"]) is int
        and value["choices"] == list(CARD_CHOICES)
    )
    return copy.deepcopy(value) if valid else None


def canonical_card_answer_request(value: object) -> dict[str, str] | None:
    """A person's answer to one card: its nonce and Rodar; Excluir is never a card answer."""
    if not isinstance(value, dict) or set(value) != {"nonce", "choice"}:
        return None
    valid = _identity(value["nonce"], NONCE_RE) and value["choice"] in CARD_ANSWERS
    return {"nonce": value["nonce"], "choice": value["choice"]} if valid else None


def canonical_card_answer(value: object) -> dict[str, object] | None:
    """What an answer did: Rodar requested one fresh run."""
    if not isinstance(value, dict) or set(value) != {"team_id", "incident_id", "choice", "status"}:
        return None
    valid = (
        _team(value["team_id"])
        and _identity(value["incident_id"], ROUTINE_ID_RE)
        and value["choice"] in CARD_ANSWERS
        and value["status"] == CARD_STATUSES[value["choice"]]
    )
    return copy.deepcopy(value) if valid else None


def canonical_notice(value: object) -> dict[str, object] | None:
    """One undelivered run or Routine outcome for Admin to write to its Team's transcript.

    Every version names the Routine as it was when Team wrote that version, so a row keeps its own title after the
    Routine is renamed or deleted. A run notice carries the run's usage; a Routine outcome carries none, except the
    healthy rollup, which carries its runs' summed usage. ``protection_lost`` says the run lost the protection of its
    secret values, so nothing it produced after the loss was shown anywhere (ADR-0101 section 6.2); a Routine outcome
    never says so.
    """
    fields = {"team_id", "notice_id", "version", "routine_id", "name", "run_id", "outcome", "created_at"}
    if not isinstance(value, dict) or set(value) != fields | {"detail", "usage", "protection_lost"}:
        return None
    outcome = value["outcome"]
    used = outcome not in ROUTINE_OUTCOMES or outcome == "healthy"
    valid = (
        _team(value["team_id"])
        and _identity(value["notice_id"], ROUTINE_ID_RE)
        and type(value["version"]) is int
        and value["version"] >= 1
        and _identity(value["routine_id"], ROUTINE_ID_RE)
        and canonical_name(value["name"]) == value["name"]
        and _optional(value["run_id"], ROUTINE_ID_RE)
        and (value["run_id"] is None) == (outcome in ROUTINE_OUTCOMES)
        # A run's one notice is keyed by its run id.
        and value["run_id"] in (None, value["notice_id"])
        and _instant(value["created_at"])
        and canonical_notice_detail(outcome, value["detail"]) is not None
        and (canonical_run_usage(value["usage"]) is not None if used else value["usage"] is None)
        and type(value["protection_lost"]) is bool
        and (outcome not in ROUTINE_OUTCOMES or value["protection_lost"] is False)
    )
    return copy.deepcopy(value) if valid else None


def canonical_notice_batch(value: object) -> dict[str, object] | None:
    """A bounded batch of notices; while ``more`` is true Admin acknowledges it and asks again."""
    if not isinstance(value, dict) or set(value) != {"notices", "more"} or type(value["more"]) is not bool:
        return None
    notices = value["notices"]
    if not isinstance(notices, list) or len(notices) > MAX_NOTICE_BATCH or (value["more"] and not notices):
        return None
    admitted = [canonical_notice(item) for item in notices]
    if None in admitted or encoded_bytes(admitted) > MAX_NOTICE_BATCH_BYTES:
        return None
    keys = {(item["team_id"], item["notice_id"]) for item in admitted}
    return {"notices": admitted, "more": value["more"]} if len(keys) == len(admitted) else None


def canonical_challenge_open(value: object) -> dict[str, str] | None:
    """Opening a frozen run's challenge names the Admin interface language its request copy renders in (ADR-0091)."""
    locale = value.get("locale") if isinstance(value, dict) and set(value) == {"locale"} else None
    return {"locale": locale} if isinstance(locale, str) and locale in LOCALES else None


def canonical_claim_request(value: object) -> dict[str, object] | None:
    """Admin's claim says only whether it can take a long run now (it holds one at most); no model key gates it."""
    if not isinstance(value, dict) or set(value) != {"long"} or type(value["long"]) is not bool:
        return None
    return {"long": value["long"]}


# A run's active time grows with its units to a ceiling, and past 600 s it is long (ADR-0092, scale; ADR-0101 §6.4).
SHORT_ACTIVE_SECONDS = 600
MAX_ACTIVE_SECONDS = 7200


def active_seconds(units: int) -> int:
    """The active execution time a run of ``units`` units may spend: 600 s for eight, 7,200 at most."""
    return min(360 + 30 * units, MAX_ACTIVE_SECONDS)


# How a claimed run was scheduled: by its firings, or continuously after the previous run ended (ADR-0092).
RUN_MODES = ("scheduled", "continuous")


def run_mode(schedule: dict[str, object]) -> str:
    return "continuous" if schedule["kind"] == "continuous" else "scheduled"


def _revision(value: object) -> bool:
    return type(value) is int and 1 <= value < 2**31


def canonical_claim(value: object) -> dict[str, object] | None:
    """A claim's answer: one run with the lease token Admin's routine identity signs for, or none and a wake hint.

    A run names the Routine revision and plan digest it was claimed at, which its segment request binds. With no run,
    ``next_due_at`` is the earliest epoch second a Routine Admin can run becomes due, or null; Admin still reconciles
    on its own interval, since a hint can be missed.
    """
    if not isinstance(value, dict) or set(value) != {"run", "next_due_at"}:
        return None
    run, hint = value["run"], value["next_due_at"]
    if run is None:
        return {"run": None, "next_due_at": hint} if hint is None or (type(hint) is int and hint > 0) else None
    fields = {"team_id", "run_id", "routine_id", "lease_token", "lease_expires_at", "provider", "active_seconds"}
    valid = (
        hint is None
        and isinstance(run, dict)
        and set(run) == fields | {"revision", "plan_digest", "mode"}
        and type(run["active_seconds"]) is int
        and 0 < run["active_seconds"] <= MAX_ACTIVE_SECONDS
        and _team(run["team_id"])
        and _identity(run["run_id"], ROUTINE_ID_RE)
        and _identity(run["routine_id"], ROUTINE_ID_RE)
        and _identity(run["lease_token"], LEASE_TOKEN_RE)
        and type(run["lease_expires_at"]) is int
        and run["lease_expires_at"] > 0
        and run["provider"] in MODEL_PROVIDERS
        and _revision(run["revision"])
        and _identity(run["plan_digest"], PLAN_DIGEST_RE)
        and run["mode"] in RUN_MODES
    )
    return copy.deepcopy(value) if valid else None


def canonical_segment_request(value: object) -> dict[str, object] | None:
    """A leased run's segment request: exactly the revision and plan digest its claim named, under the signature."""
    if not isinstance(value, dict) or set(value) != {"revision", "plan_digest", "mode"}:
        return None
    # A request body reaches only the fixed-length digest pattern, never the shared identifier patterns.
    plan_digest = value["plan_digest"]
    valid = (
        _revision(value["revision"])
        and isinstance(plan_digest, str)
        and PLAN_DIGEST_RE.fullmatch(plan_digest) is not None
        and value["mode"] in RUN_MODES
    )
    return {"revision": value["revision"], "plan_digest": plan_digest, "mode": value["mode"]} if valid else None


# Per-execution diagnostics (ADR-0092 section 8): one Team-sanitized handled failure, or one safe transport condition,
# per attempt of one logical operation of a Routine run. Text members are literal evidence that Admin renders escaped,
# never as Markdown or HTML, and they are never effect proof or authority.
MAX_RUN_DIAGNOSTICS = 32
MAX_DIAGNOSTIC_ATTEMPTS = 64
MAX_DIAGNOSTIC_TEXT_BYTES = 2048
OPERATION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
ERROR_TYPE_RE = re.compile(r"[!-~]{1,128}\Z")
PROVIDER_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")
# The closed safe transport conditions; raw child output is never reflected.
CONDITION_RE = re.compile(
    r"(?:exit-status:-?[0-9]{1,10}|stderr-output|timeout|frame-invalid|exit-unavailable|transport-failed)\Z"
)
# Tab and line feed only; every other control, bidi override or isolate, and zero-width formatting character is refused.
_UNSAFE_TEXT_RE = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f-\u009f​-‏‪-‮⁠-⁯﻿]")
_FAILURE_FIELDS = frozenset(
    {"error_type", "message", "provider", "http_status", "response_excerpt", "redacted", "truncated"}
)
_DIAGNOSTIC_FIELDS = frozenset(
    {"operation_id", "attempt", "assistant_id", "action", "position", "recorded_at", "failure", "condition"}
)


def _diagnostic_text(value: object) -> bool:
    if not isinstance(value, str) or _UNSAFE_TEXT_RE.search(value) is not None:
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_DIAGNOSTIC_TEXT_BYTES
    except UnicodeEncodeError:
        return False


def canonical_failure(value: object) -> dict[str, object] | None:
    """One sanitized handled failure: the real type, message, provider, status, excerpt, and both flags."""
    if not isinstance(value, dict) or set(value) != _FAILURE_FIELDS:
        return None
    provider, status, excerpt = value["provider"], value["http_status"], value["response_excerpt"]
    valid = (
        _identity(value["error_type"], ERROR_TYPE_RE)
        and _diagnostic_text(value["message"])
        and (provider is None or (_identity(provider, PROVIDER_RE) and len(provider) <= 253))
        and (status is None or (type(status) is int and 100 <= status <= 599))
        and (excerpt is None or _diagnostic_text(excerpt))
        and type(value["redacted"]) is bool
        and type(value["truncated"]) is bool
    )
    return copy.deepcopy(value) if valid else None


def canonical_diagnostic(value: object) -> dict[str, object] | None:
    """One attempt's diagnostic: exactly one of a sanitized failure or a safe transport condition."""
    if not isinstance(value, dict) or set(value) != _DIAGNOSTIC_FIELDS:
        return None
    failure, condition = value["failure"], value["condition"]
    valid = (
        _identity(value["operation_id"], OPERATION_ID_RE)
        and type(value["attempt"]) is int
        and 1 <= value["attempt"] <= MAX_DIAGNOSTIC_ATTEMPTS
        and _assistant(value["assistant_id"])
        and _action(value["action"])
        and canonical_position(value["position"], MAX_ROUTINE_STEPS) is not None
        and _instant(value["recorded_at"])
        and (failure is None) != (condition is None)
        and (failure is None or canonical_failure(failure) is not None)
        and (condition is None or _identity(condition, CONDITION_RE))
    )
    return copy.deepcopy(value) if valid else None


def canonical_diagnostics(value: object) -> dict[str, object] | None:
    """A run's diagnostics, oldest first, each attempt of each operation at most once."""
    if not isinstance(value, dict) or set(value) != {"team_id", "run_id", "diagnostics"}:
        return None
    entries = value["diagnostics"]
    if (
        not _team(value["team_id"])
        or not _identity(value["run_id"], ROUTINE_ID_RE)
        or not isinstance(entries, list)
        or len(entries) > MAX_RUN_DIAGNOSTICS
    ):
        return None
    admitted = [canonical_diagnostic(item) for item in entries]
    if None in admitted:
        return None
    keys = [(item["recorded_at"], item["operation_id"], item["attempt"]) for item in admitted]
    unique = len({(item["operation_id"], item["attempt"]) for item in admitted}) == len(admitted)
    return {**value, "diagnostics": admitted} if unique and keys == sorted(keys) else None


# What one run did, call by call (ADR-0092 amendment, 2026-10-05, scale; ADR-0101 section 7): each replay step and
# decision call's status, attempt, duration, and inputs as redacted previews (null when a source's secrecy is unknown).
# A rehearsal records an effect it did not run as ``rehearsed`` and a step that needed its output as ``untested``; a
# decision call outside the permitted set in a rehearsal is ``not-permitted``. A missing replay position is ``not_run``
# only when the run's terminal record proves it, else ``unavailable``; a missing decision call is always
# ``unavailable``. Pages bind the run's revision and one records snapshot, and carry the run's decision record.
RUN_STEP_STATUSES = ("done", "recovered", "failed", "stopped", "waiting", "rehearsed", "untested", "not-permitted")
RUN_STEP_GAPS = ("not_run", "unavailable")
RUN_INPUT_SOURCES = frozenset({"literal", "run_clock", "step_output", "decision"})
SNAPSHOT_RE = re.compile(r"[0-9a-f]{32}\Z")
RUN_STEP_FIELDS = frozenset(
    {"position", "status", "assistant_id", "action", "attempt", "duration_ms", "recorded_at", "inputs"}
)
# The one decision record a run page carries at most, encoded.
MAX_DECISION_RECORD_BYTES = 16 * 1024


def _run_input(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"member", "source", "value"}
        and _plain(value["member"], MAX_MEMBER_CHARS)
        and isinstance(value["source"], str)
        and value["source"] in RUN_INPUT_SOURCES
        and (value["value"] is None or _plain(value["value"], MAX_PREVIEW_CHARS))
    )


def _status_phase(status: object, phase: str) -> bool:
    """Which statuses each phase may record: untested and not_run are replay's, not-permitted a decision's."""
    if phase == "replay":
        return status != "not-permitted"
    return status not in ("untested", "not_run")


def canonical_run_step(value: object, position: dict[str, object], steps: int) -> dict[str, object] | None:
    """One entry at ``position``: what its attempt did, or only that it never ran or cannot be shown."""
    if not isinstance(value, dict) or set(value) != RUN_STEP_FIELDS or value.get("position") != position:
        return None
    status, duration = value["status"], value["duration_ms"]
    if status in RUN_STEP_GAPS:
        valid = all(value[key] is None for key in RUN_STEP_FIELDS - {"position", "status"})
    else:
        valid = (
            status in RUN_STEP_STATUSES
            and _assistant(value["assistant_id"])
            and _action(value["action"])
            and type(value["attempt"]) is int
            and 1 <= value["attempt"] <= MAX_DIAGNOSTIC_ATTEMPTS
            and (duration is None or (type(duration) is int and 0 <= duration < 2**53))
            and (status != "recovered" or duration is None)
            and _instant(value["recorded_at"])
            and (value["inputs"] is None or _members(value["inputs"], _run_input))
        )
    valid = (
        valid
        and canonical_position(value["position"], steps) is not None
        and _status_phase(status, position["phase"])
        and encoded_bytes(value) <= MAX_STEP_VIEW_BYTES
    )
    return copy.deepcopy(value) if valid else None


def run_position(index: int, steps: int) -> dict[str, object]:
    """The position of a run page's ``index``-th entry (1-based): its replay steps first, then its decision calls."""
    if index <= steps:
        return {"phase": "replay", "step": index}
    return {"phase": "decision", "call": index - steps}


def canonical_run_steps(value: object) -> dict[str, object] | None:
    """One page of a run's entries from ``offset``: whole consecutive positions of its own revision's run.

    ``replay`` is the revision's step count and ``total`` adds the decision calls the run's records know.
    """
    fields = {"team_id", "run_id", "routine_id", "revision", "plan_digest", "replay", "total", "snapshot", "ended"}
    if not isinstance(value, dict) or set(value) != fields | {"offset", "steps", "next", "decision"}:
        return None
    replay = value["replay"]
    decision = value["decision"]
    valid = (
        _team(value["team_id"])
        and _identity(value["run_id"], ROUTINE_ID_RE)
        and _identity(value["routine_id"], ROUTINE_ID_RE)
        and _revision(value["revision"])
        and _identity(value["plan_digest"], PLAN_DIGEST_RE)
        and _whole(replay, 0, MAX_ROUTINE_STEPS)
        and _whole(value["total"], replay, replay + MAX_DECISION_CALLS)
        and _identity(value["snapshot"], SNAPSHOT_RE)
        and type(value["ended"]) is bool
        and _paged(value, lambda item, index: canonical_run_step(item, run_position(index, replay), replay))
        and (
            decision is None
            or (
                canonical_decision_record(decision) is not None and encoded_bytes(decision) <= MAX_DECISION_RECORD_BYTES
            )
        )
    )
    return copy.deepcopy(value) if valid else None


# The confirmation card of a recorded Routine (ADR-0101 section 5.2): every literal complete and escaped, every source
# and selector described completely, the schedule, the output, every permitted Action, and for a decision its request,
# notes, model, and allowance. It is the one thing a person confirms, so nothing in it is paged or cut; a recording
# whose card does not fit is refused.
MAX_PROPOSAL_BYTES = 160 * 1024
INPUT_ORIGINS = ("request", "assistant", "clock", "step", "selector")
MAX_NEXT_RUNS = 3
MAX_BASE_REQUEST_CHARS = 16_000
MAX_NOTES_CHARS = 4000
_PROPOSAL_INPUT_FIELDS = frozenset({"member", "origin", "value", "step", "pointer", "where", "item"})


def _card_input(value: object, position: int) -> bool:
    """One input as the card shows it: a literal's complete JSON text, the run date, or its source step and path."""
    if not isinstance(value, dict) or set(value) != _PROPOSAL_INPUT_FIELDS or value["origin"] not in INPUT_ORIGINS:
        return False
    origin = value["origin"]
    if not _plain(value["member"], MAX_MEMBER_CHARS):
        return False
    if origin in ("request", "assistant"):
        literal = isinstance(value["value"], str) and _PLAN_UNSAFE_RE.search(value["value"]) is None
        return literal and all(value[key] is None for key in ("step", "pointer", "where", "item"))
    if origin == "clock":
        return all(value[key] is None for key in ("value", "step", "pointer", "where", "item"))
    return (
        value["value"] is None
        and (value["where"] is not None) == (origin == "selector")
        and _reference(value, position)
    )


def _card_step(value: object, position: int) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"position", "assistant", "action", "read_only", "inputs"}
        and value["position"] == position
        and type(value["position"]) is int
        and _assistant(value["assistant"])
        and _action(value["action"])
        and type(value["read_only"]) is bool
        and _members(value["inputs"], lambda item: _card_input(item, position))
    )


def _card_permitted(value: object) -> bool:
    """Every permitted Action, each once, in identity order, with whether its reviewed effect is read-only."""
    if not isinstance(value, list) or len(value) > MAX_PERMITTED:
        return False
    if not all(
        isinstance(item, dict)
        and set(item) == {"assistant", "action", "read_only"}
        and _assistant(item["assistant"])
        and _action(item["action"])
        and type(item["read_only"]) is bool
        for item in value
    ):
        return False
    identities = [(item["assistant"], item["action"]) for item in value]
    return identities == sorted(set(identities))


def _card_decision(value: object) -> bool:
    """A decision's standing scope as the card shows it: the request, the assistant's notes, model, and allowance."""
    if value is None:
        return True
    return (
        isinstance(value, dict)
        and set(value) == {"request", "notes", "model", "allowance"}
        and _plain_text(value["request"], MAX_BASE_REQUEST_CHARS)
        and bool(value["request"])
        and _plain_text(value["notes"], MAX_NOTES_CHARS)
        and canonical_model(value["model"]) is not None
        and _whole(value["allowance"], 1, MAX_ALLOWANCE)
    )


def _card_output(value: object, total: int) -> bool:
    """The card's output: its mode and condition; a shown mode shows the last step."""
    if not isinstance(value, dict) or set(value) != {"mode", "when"}:
        return False
    shown = total if value["mode"] in SHOWN_MODES else None
    return canonical_disposition({**value, "step": shown}, total) is not None


def _changes(value: dict[str, object]) -> bool:
    """Whether a card's Routine may change anything, so it is rehearsed before it can run (ADR-0101 section 8)."""
    return not all(item["read_only"] for item in [*value["steps"], *value["permitted"]])


def canonical_proposal(value: object) -> dict[str, object] | None:
    """One recorded Routine's confirmation card, within its byte bound."""
    fields = {"proposal_id", "expires_at", "replaces", "name", "schedule", "timezone", "next_runs", "daily_cap"}
    rest = {"clamped", "output", "steps", "permitted", "decision", "rehearsal"}
    if not isinstance(value, dict) or set(value) != fields | rest:
        return None
    steps, runs = value["steps"], value["next_runs"]
    if not isinstance(steps, list) or len(steps) > MAX_ROUTINE_STEPS or not _card_output(value["output"], len(steps)):
        return None
    valid = (
        _identity(value["proposal_id"], ROUTINE_ID_RE)
        and _instant(value["expires_at"])
        and _optional(value["replaces"], ROUTINE_ID_RE)
        and canonical_name(value["name"]) == value["name"]
        and canonical_schedule(value["schedule"]) == value["schedule"]
        and canonical_timezone(value["timezone"]) is not None
        and isinstance(runs, list)
        and 1 <= len(runs) <= MAX_NEXT_RUNS
        and all(_instant(item) for item in runs)
        and runs == sorted(runs)
        and _whole(value["daily_cap"], 1, MAX_DAILY_RUNS)
        and type(value["clamped"]) is bool
        and all(_card_step(item, index) for index, item in enumerate(steps, start=1))
        and _card_permitted(value["permitted"])
        and (value["decision"] is not None) == (value["output"]["mode"] == "decide")
        and _card_decision(value["decision"])
        and len(steps) + (0 if value["decision"] is None else value["decision"]["allowance"]) <= MAX_ROUTINE_STEPS
        and value["rehearsal"] is _changes(value)
        and encoded_bytes(value) <= MAX_PROPOSAL_BYTES
    )
    return copy.deepcopy(value) if valid else None


def canonical_refusal(value: object) -> dict[str, str] | None:
    """Why a recording made no card: one code, which Admin words in the interface language; nothing was created."""
    if not isinstance(value, dict) or set(value) != {"code"} or not _identity(value["code"], ERROR_CODE_RE):
        return None
    return {"code": value["code"]}


# A person's answer to a card: Criar rotina creates or changes the Routine; Cancelar revokes the card.
PROPOSAL_STATUSES = ("created", "changed", "revoked")


def canonical_proposal_answer(value: object) -> dict[str, object] | None:
    """What an answer did: the Routine it created or changed, or the card it revoked."""
    if not isinstance(value, dict) or set(value) != {"team_id", "proposal_id", "routine_id", "status"}:
        return None
    status = value["status"]
    valid = (
        _team(value["team_id"])
        and _identity(value["proposal_id"], ROUTINE_ID_RE)
        and status in PROPOSAL_STATUSES
        and (value["routine_id"] is None if status == "revoked" else _identity(value["routine_id"], ROUTINE_ID_RE))
    )
    return copy.deepcopy(value) if valid else None
