"""Canonical Team Routine wire forms (ADR-0086, ADR-0101): schedules, timezones, bounds, and the views Admin admits."""

from __future__ import annotations

import copy
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
# A continuous Routine starts its runs this far apart, at least, and at most a day.
MIN_CONTINUOUS_GAP_SECONDS = 5
MAX_CONTINUOUS_GAP_SECONDS = 86_400
DAY_SECONDS = 86_400
# A continuous Routine's cap is always its gap's whole day, never lowered, so the interval a person stated is what
# runs; the Team's daily Action-step budget alone bounds the sum (ADR-0101).
MAX_CONTINUOUS_CAP = -(-DAY_SECONDS // MIN_CONTINUOUS_GAP_SECONDS)
# The healthy runs one minute bucket rolls up: runs start their gap apart, but a run that outlasted it or a confirmed
# change starts the next at once, so the bound is one a second.
MAX_ROLLUP_RUNS = 60
MAX_NOTICE_ACTIONS = 16
MAX_NOTICE_ASSISTANTS = 16
# A Routine's permitted Actions: every Action its replay steps call, each once (ADR-0101).
MAX_PERMITTED = MAX_ROUTINE_STEPS
OUTCOMES = frozenset(
    {
        "done",
        "healthy",
        "recovered",
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
    86,400) after each run ends, and ``cap`` exactly ``continuous_cap(gap)`` starts in any rolling 24 hours.
    """
    kind = value.get("kind") if isinstance(value, dict) else None
    if not isinstance(kind, str) or kind not in SCHEDULE_KINDS or set(value) != _FIELDS[kind]:
        return None
    if kind == "continuous":
        gap = value["gap"]
        valid = (
            _whole(gap, MIN_CONTINUOUS_GAP_SECONDS, MAX_CONTINUOUS_GAP_SECONDS)
            and type(value["cap"]) is int
            and value["cap"] == continuous_cap(gap)
        )
    elif kind == "hourly":
        valid = _whole(value["every"], 1, 24)
    else:
        valid = _wall_clock(value)
    return dict(value) if valid else None


def continuous_cap(gap: int) -> int:
    """A continuous Routine's starts in any rolling 24 hours: as many as its gap allows all day, ceil(86400 / gap)."""
    return -(-DAY_SECONDS // gap)


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


# Where a Routine's timezone came from (ADR-0101): the person's browser, a zone the person wrote, or none at all, when
# the Routine runs on "UTC" only by convention, which no consumer may read as the person's zone.
TIMEZONE_SOURCES = ("browser", "person", "none")
CONVENTIONAL_TIMEZONE = "UTC"


def zoned(timezone: object, source: object) -> bool:
    """Whether a timezone and its source fit together: a known source names a zone, and none means UTC."""
    if source not in TIMEZONE_SOURCES or canonical_timezone(timezone) is None:
        return False
    return source != "none" or timezone == CONVENTIONAL_TIMEZONE


def daily_rate(schedule: dict[str, object]) -> Fraction:
    """The runs per day a canonical schedule allows, its cap for a continuous one."""
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
# steps. Notices, incidents, freezes, cards, and run records use it.
PHASES = ("replay",)


def _position(value: object, maximum: int = MAX_ROUTINE_STEPS) -> bool:
    return type(value) is int and 1 <= value <= maximum


def canonical_position(value: object, steps: object) -> dict[str, object] | None:
    """A call's position: a replay step among ``steps``; or None."""
    if not isinstance(value, dict) or not _whole(steps, 0, MAX_ROUTINE_STEPS):
        return None
    if value.get("phase") == "replay" and set(value) == {"phase", "step"} and _position(value["step"], steps):
        return {"phase": "replay", "step": value["step"]}
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
_PLAN_UNSAFE_RE = re.compile(r"[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ud800-\udfff\ufeff]")
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
    """Whole consecutive entries of ``total`` from ``offset``, the next offset or null, within the page's bounds."""
    total, offset, steps = value["total"], value["offset"], value["steps"]
    return (
        _whole(total, 1, MAX_ROUTINE_STEPS)
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
        and _paged(value, canonical_step)
    )
    return copy.deepcopy(value) if valid else None


# A revision's summary, which list views and notices carry instead of steps: its digest, step count, and Actions as at
# most 16 runs of consecutive equal Actions; ``more`` counts the steps after them.
MAX_SUMMARY_RUNS = 16


def canonical_summary(value: object) -> dict[str, object] | None:
    """A plan summary: its runs cover the first steps in order, each differs from the one before, and none is empty."""
    if not isinstance(value, dict) or set(value) != {"revision", "plan_digest", "steps", "actions", "more"}:
        return None
    runs, more, total = value["actions"], value["more"], value["steps"]
    valid = (
        _revision(value["revision"])
        and _identity(value["plan_digest"], PLAN_DIGEST_RE)
        and _whole(total, 1, MAX_ROUTINE_STEPS)
        and isinstance(runs, list)
        and 0 < len(runs) <= MAX_SUMMARY_RUNS
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
# after every run, only when it changed, or show none of it. show and changes name the shown step, on the wire by its
# position.
OUTPUT_MODES = ("show", "changes", "none")
SHOWN_MODES = frozenset({"show", "changes"})


def canonical_disposition(value: object, total: object) -> dict[str, object] | None:
    """A plan's output disposition, whose shown step is the position of one of its ``total`` steps, or None."""
    if not isinstance(value, dict) or set(value) != {"mode", "step"} or value["mode"] not in OUTPUT_MODES:
        return None
    mode, shown = value["mode"], value["step"]
    if not _whole(total, 1, MAX_ROUTINE_STEPS):
        return None
    valid = _position(shown, total) if mode in SHOWN_MODES else shown is None
    return {"mode": mode, "step": shown} if valid else None


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
# model, the tokens its recovery calls reported; a run that needed no recovery has no model.
canonical_run_usage = payload.canonical_run_usage


# The model providers a claimed run's recovery may use.
MODEL_PROVIDERS = ("anthropic", "openai")


def canonical_permitted(value: object) -> dict[str, int] | None:
    """A Routine's permitted Actions as views and notices carry them: how many, and how many change something.

    The card showed the complete set when the person confirmed it.
    """
    if not isinstance(value, dict) or set(value) != {"total", "changes"}:
        return None
    valid = _whole(value["total"], 1, MAX_PERMITTED) and _whole(value["changes"], 0, value["total"])
    return {"total": value["total"], "changes": value["changes"]} if valid else None


def encoded_bytes(value: object) -> int:
    """The UTF-8 size of a value in the Local API's JSON encoding."""
    return len(json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8"))


def _revision(value: object) -> bool:
    return type(value) is int and 1 <= value < 2**31
