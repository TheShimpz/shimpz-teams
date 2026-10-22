"""Canonical Routine forms a recording chat turn's Brain context carries (ADR-0101): the listing and rerun work."""

from __future__ import annotations

import copy
import datetime
import json
import re

if __package__:
    from . import routine
else:  # The protocol verifier runs every module of this directory flat.
    import routine


# The Team's Routines as a recording chat's Brain sees them (ADR-0101): enough to name one and see its steps, never an
# input's value. A step keeps its plan id and its input members only.
ROUTINE_STEP_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_LISTING_FIELDS = frozenset(
    {"routine_id", "name", "schedule", "timezone", "timezone_source", "revision", "daily_steps", "output", "steps"}
)
# A Routine's Action units in any rolling 24 hours: every start of its whole cap may use every unit.
MAX_LISTED_DAILY_STEPS = routine.MAX_CONTINUOUS_CAP * routine.MAX_ROUTINE_STEPS
# The listed steps' compact UTF-8 JSON: at most this for each Routine, and for the whole listing together. Team bounds a
# Routine's plan, and every plan of a Team together, by these, and a listing's steps are a part of its plans.
MAX_LISTED_STEPS_BYTES = 256 * 1024
MAX_LISTING_STEPS_BYTES = 1024 * 1024


def _members_listed(value: object, limit: int, maximum: int) -> bool:
    """Input member names: at most ``limit``, each plain and at most ``maximum`` characters, sorted, none twice."""
    return (
        isinstance(value, list)
        and len(value) <= limit
        and all(routine.plain(item, maximum) for item in value)
        and value == sorted(set(value))
    )


def _listed_output(value: object, total: int) -> bool:
    """A listed Routine's output: its mode alone, of a plan with steps; a shown mode shows the last step."""
    return (
        isinstance(value, dict)
        and set(value) == {"mode"}
        and value["mode"] in routine.OUTPUT_MODES
        and routine.whole(total, 1, routine.MAX_ROUTINE_STEPS)
    )


def _listed_step(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "assistant", "action", "inputs"}
        and routine.matches(value["id"], ROUTINE_STEP_ID_RE)
        and routine.valid_assistant(value["assistant"])
        and routine.valid_action(value["action"])
        and _members_listed(value["inputs"], routine.MAX_STEP_INPUTS, routine.MAX_MEMBER_CHARS)
    )


def canonical_routine_listing(value: object) -> dict[str, object] | None:
    """One Routine as the Brain sees it: its id, name, when it runs and where, revision, daily steps, output, steps."""
    if not isinstance(value, dict) or set(value) != _LISTING_FIELDS:
        return None
    steps = value["steps"]
    valid = (
        routine.matches(value["routine_id"], routine.ROUTINE_ID_RE)
        and routine.exact_name(value["name"])
        and routine.exact_schedule(value["schedule"])
        and routine.zoned(value["timezone"], value["timezone_source"])
        and routine.valid_revision(value["revision"])
        and routine.whole(value["daily_steps"], 1, MAX_LISTED_DAILY_STEPS)
        and isinstance(steps, list)
        and len(steps) <= routine.MAX_ROUTINE_STEPS
        and _listed_output(value["output"], len(steps))
        and all(_listed_step(item) for item in steps)
    )
    return copy.deepcopy(value) if valid else None


def _steps_bytes(listed: dict[str, object]) -> int:
    return len(json.dumps(listed["steps"], ensure_ascii=False, separators=(",", ":")).encode())


def canonical_routine_listings(value: object) -> list[dict[str, object]] | None:
    """The Team's whole Routine listing: at most MAX_ROUTINES, each admitted, none twice, within its byte bounds."""
    if not isinstance(value, list | tuple) or len(value) > routine.MAX_ROUTINES:
        return None
    listed = [canonical_routine_listing(item) for item in value]
    if any(item is None for item in listed) or len({item["routine_id"] for item in listed}) != len(listed):
        return None
    sizes = [_steps_bytes(item) for item in listed]
    if any(size > MAX_LISTED_STEPS_BYTES for size in sizes) or sum(sizes) > MAX_LISTING_STEPS_BYTES:
        return None
    return listed if list(value) == listed else None


# The work a pending rerun question asks the Brain to run again (ADR-0101): one entry per call, or per
# ``count`` consecutive identical calls, at most a plan's steps in all. Each input member has its kind: ``value`` with
# its exact JSON text (null when withheld) and whether it is a target the person chose; ``clock``, the run's date; or
# ``fresh``, never shown, with the Action whose earlier result held it, if any.
MAX_RERUN_ENTRIES = routine.MAX_ROUTINE_STEPS
MAX_RERUN_CALLS = routine.MAX_ROUTINE_STEPS
MAX_RERUN_INPUTS = routine.MAX_STEP_INPUTS
MAX_RERUN_MEMBER_CHARS = routine.MAX_MEMBER_CHARS
MAX_RERUN_LITERAL_CHARS = 1024
# The shortest string, and the fewest digits of a whole number, Team's recorder takes as an identifier that only a
# result can source; any shorter value is the agent's own choice. The Brain reads them to check rerun work as Team does.
MIN_IDENTIFIER_CHARS = 6
MIN_IDENTIFIER_DIGITS = 6
# A complete number as written, its sign and exponent included, never a fragment of a longer number or word: Team's
# recorder takes a number as the person's only when one of their texts writes it as one such token.
NUMBER_TOKEN_RE = re.compile(r"(?<![\w.,+-])[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?(?!\w|[.,]\d)")
# The date forms without a space that Team's recorder writes again for each run's own date inside a text no result
# holds (``routine.dates``), and only near the date the send that recorded it started on: ISO, numeric with a year,
# short ``d/m``, and CJK, a numeric one day or month first and a missing year the nearest one.
MAX_DATE_OFFSET_DAYS = 31
UNSPACED_DATE_FORMS = (
    ("iso", re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?![\d-])")),
    ("numeric", re.compile(r"(?<![\d/.])(\d{1,2})([/.])(\d{1,2})\2(\d{4})(?![\d/]|\.\d)")),
    ("short", re.compile(r"(?<![\d/.])(\d{1,2})/(\d{1,2})(?![\d/]|\.\d)")),
    ("cjk", re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日")),
)


def may_write_date(text: str, today: datetime.date) -> bool:
    """Whether a text writes a date Team's recorder may read as each run's own: in an unspaced form, near ``today``.

    It reads every match in either numeric order and a day more than Team's reach, so the date a send started on in
    any zone counts. Team writes a date in ASCII digits only, so a match with any other digit never counts. The Brain
    leaves such a text to Team, which decides.
    """
    return any(
        _near(year, month, day, today)
        for kind, pattern in UNSPACED_DATE_FORMS
        for match in pattern.finditer(text)
        if all(group is None or group.isascii() for group in match.groups())
        for year, month, day in _dates_written(kind, match.groups())
    )


def _dates_written(kind: str, groups: tuple[str | None, ...]) -> tuple[tuple[str | None, str, str], ...]:
    """Each (year, month, day) a match may write, a missing year None: a numeric form day first or month first."""
    if kind in ("iso", "cjk"):
        return ((groups[0], groups[1], groups[2]),)
    first, second, year = (groups[0], groups[2], groups[3]) if kind == "numeric" else (groups[0], groups[1], None)
    return ((year, second, first), (year, first, second))


def _near(year: str | None, month: str, day: str, today: datetime.date) -> bool:
    years = (int(year),) if year else (today.year - 1, today.year, today.year + 1)
    for candidate in years:
        try:
            written = datetime.date(candidate, int(month), int(day))
        except ValueError:
            continue
        if abs((written - today).days) <= MAX_DATE_OFFSET_DAYS + 1:
            return True
    return False


def _rejected_constant(_text: str) -> object:
    raise ValueError


def _json_literal(text: object) -> bool:
    """One value's exact compact JSON text, within its bound."""
    if not isinstance(text, str) or not 0 < len(text) <= MAX_RERUN_LITERAL_CHARS:
        return False
    try:
        decoded = json.loads(text, parse_constant=_rejected_constant)
    except ValueError:
        return False
    return json.dumps(decoded, ensure_ascii=False) == text


def _rerun_source(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"assistant", "action"}
        and routine.valid_assistant(value["assistant"])
        and routine.valid_action(value["action"])
    )


def _rerun_input(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"member", "kind", "value", "chosen", "source"}:
        return False
    kind, shown, chosen, source = value["kind"], value["value"], value["chosen"], value["source"]
    if kind == "value":
        valid = (shown is None or _json_literal(shown)) and type(chosen) is bool and source is None
    elif kind == "fresh":
        valid = shown is None and chosen is False and (source is None or _rerun_source(source))
    else:
        valid = kind == "clock" and shown is None and chosen is False and source is None
    return valid and routine.plain(value["member"], MAX_RERUN_MEMBER_CHARS)


def _rerun_entry(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"assistant", "action", "count", "inputs"}:
        return False
    inputs = value["inputs"]
    return (
        routine.valid_assistant(value["assistant"])
        and routine.valid_action(value["action"])
        and routine.whole(value["count"], 1, MAX_RERUN_CALLS)
        and isinstance(inputs, list)
        and len(inputs) <= MAX_RERUN_INPUTS
        and all(_rerun_input(item) for item in inputs)
        and _members_listed([item["member"] for item in inputs], MAX_RERUN_INPUTS, MAX_RERUN_MEMBER_CHARS)
    )


def canonical_rerun(value: object) -> list[dict[str, object]] | None:
    """The work a rerun question asks the Brain to repeat, in order, within its bounds, or None."""
    if not isinstance(value, list | tuple) or not 0 < len(value) <= MAX_RERUN_ENTRIES:
        return None
    if not all(_rerun_entry(item) for item in value) or sum(item["count"] for item in value) > MAX_RERUN_CALLS:
        return None
    return copy.deepcopy(list(value))


# The one Routine a recording send selects (ADR-0122): the Routine the person's message names by Team's own name match,
# or the one the panel's Refine entry sends. The Brain sees its bounded definition: its description, revision, and each
# step as a Supervisor's plan view projects it (``routine.canonical_step``: its position, Action, effect, every input's
# source with a literal or a run date as a bounded preview and a reference by the earlier step's position, pointer, and
# selector, and its Stored Inputs by name), beside the step's revision-scoped ``id``, which an edit names; its output;
# and the latest run's outcome per step with the revision that run executed, or null when that evidence is unavailable.
# It is untrusted data bound to its Team, Routine, revision, and run.
# A step's outcome in the latest run: its own record's status, or a gap the run's records prove (never run) or cannot
# show; Team pins these equal to ``routine_run``'s statuses and gaps.
LAST_RUN_STATUSES = ("done", "recovered", "failed", "stopped", "waiting", "not_run", "unavailable")
# The selected definition's compact UTF-8 JSON at most; a larger one is not sent.
MAX_SELECTED_BYTES = 256 * 1024
_SELECTED_FIELDS = frozenset({"routine_id", "name", "description", "revision", "output", "steps", "last_run"})


def _selected_step(value: object, position: int, earlier: list[str]) -> bool:
    """One step at ``position``: its id, not one an earlier step has, beside its plan projection."""
    return (
        isinstance(value, dict)
        and routine.matches(value.get("id"), ROUTINE_STEP_ID_RE)
        and value["id"] not in earlier
        and routine.canonical_step({key: item for key, item in value.items() if key != "id"}, position) is not None
    )


def _last_run(value: object) -> bool:
    """The latest run's outcome at each position of the revision it executed, from 1, or null when unavailable."""
    if value is None:
        return True
    if (
        not isinstance(value, dict)
        or set(value) != {"revision", "steps"}
        or not routine.valid_revision(value["revision"])
    ):
        return False
    steps = value["steps"]
    return (
        isinstance(steps, list)
        and 0 < len(steps) <= routine.MAX_ROUTINE_STEPS
        and all(
            isinstance(item, dict)
            and set(item) == {"position", "status"}
            and item["position"] == position
            and type(item["position"]) is int
            and item["status"] in LAST_RUN_STATUSES
            for position, item in enumerate(steps, start=1)
        )
    )


def canonical_selected_routine(value: object) -> dict[str, object] | None:
    """The selected Routine's definition as the Brain sees it, within its byte bound, or None.

    Its output is its mode and, for a shown mode, its last step's id.
    """
    if not isinstance(value, dict) or set(value) != _SELECTED_FIELDS:
        return None
    steps, output = value["steps"], value["output"]
    ids: list[str] = []
    for position, item in enumerate(steps if isinstance(steps, list) else (), start=1):
        if not _selected_step(item, position, ids):
            return None
        ids.append(item["id"])
    valid = (
        routine.matches(value["routine_id"], routine.ROUTINE_ID_RE)
        and routine.exact_name(value["name"])
        and routine.exact_description(value["description"])
        and routine.valid_revision(value["revision"])
        and isinstance(steps, list)
        and 0 < len(steps) <= routine.MAX_ROUTINE_STEPS
        and isinstance(output, dict)
        and set(output) == {"mode", "step"}
        and output["mode"] in routine.OUTPUT_MODES
        and output["step"] == (ids[-1] if output["mode"] in routine.SHOWN_MODES else None)
        and _last_run(value["last_run"])
        and len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) <= MAX_SELECTED_BYTES
    )
    return copy.deepcopy(value) if valid else None


# The agent's edits of the selected Routine (ADR-0122): each is closed and names steps by their revision-scoped ids and
# this turn's traced calls by the handles Team issued with their results. ``insert`` adds the calls after a step, in
# their traced order; ``replace`` puts one call in a step's place; ``remove`` drops a step. No step and no call is named
# twice.
REFINE_KINDS = ("insert", "replace", "remove")
# A traced call of the current send, by its 1-based place among the send's traced calls: "c1", "c2", ...
CALL_HANDLE_RE = re.compile(r"c[1-9][0-9]{0,2}\Z")
MAX_REFINE_EDITS = routine.MAX_ROUTINE_STEPS
_REFINE_FIELDS = frozenset({"routine_id", "base_revision", "description", "edits"})


def _edit(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"kind", "step", "calls"} or value["kind"] not in REFINE_KINDS:
        return False
    calls = value["calls"]
    if not isinstance(calls, list):
        return False
    counted = {"insert": 0 < len(calls) <= routine.MAX_ROUTINE_STEPS, "replace": len(calls) == 1, "remove": not calls}
    return (
        routine.matches(value["step"], ROUTINE_STEP_ID_RE)
        and all(routine.matches(item, CALL_HANDLE_RE) for item in calls)
        and counted[value["kind"]]
    )


def canonical_edits(value: object) -> list[dict[str, object]] | None:
    """A refinement's edits: at most one per step and each call named once, in their given order, or None."""
    if not isinstance(value, list) or len(value) > MAX_REFINE_EDITS or not all(_edit(item) for item in value):
        return None
    steps = [item["step"] for item in value]
    calls = [call for item in value for call in item["calls"]]
    if len(set(steps)) != len(steps) or len(set(calls)) != len(calls):
        return None
    return copy.deepcopy(value)


def canonical_refine(value: object) -> dict[str, object] | None:
    """The agent's ``refine`` as Brain reports it: the Routine, the revision it edits, the description, and edits."""
    if not isinstance(value, dict) or set(value) != _REFINE_FIELDS:
        return None
    valid = (
        routine.matches(value["routine_id"], routine.ROUTINE_ID_RE)
        and routine.valid_revision(value["base_revision"])
        and routine.exact_description(value["description"])
        and canonical_edits(value["edits"]) is not None
    )
    return copy.deepcopy(value) if valid else None
