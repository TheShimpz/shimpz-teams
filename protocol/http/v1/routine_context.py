"""Canonical Routine forms a recording chat turn's Brain context carries (ADR-0101): the listing and rerun work."""

from __future__ import annotations

import copy
import json
import re

if __package__:
    from . import routine, routine_proposal
else:  # The protocol verifier runs every module of this directory flat.
    import routine_proposal

    import routine


# The Team's Routines as a recording chat's Brain sees them (ADR-0101): enough to name one and see its steps, never an
# input's value. A step keeps its plan id and its input members only.
ROUTINE_STEP_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_LISTING_FIELDS = frozenset(
    {"routine_id", "name", "schedule", "timezone", "timezone_source", "revision", "daily_steps", "output", "steps"}
)
# A Routine's Action units in any rolling 24 hours: every start of its whole cap may use every unit.
MAX_LISTED_DAILY_STEPS = routine.MAX_CONTINUOUS_CAP * routine.MAX_ROUTINE_STEPS


def _members_listed(value: object, limit: int, maximum: int) -> bool:
    """Input member names: at most ``limit``, each plain and at most ``maximum`` characters, sorted, none twice."""
    return (
        isinstance(value, list)
        and len(value) <= limit
        and all(routine._plain(item, maximum) for item in value)
        and value == sorted(set(value))
    )


def _listed_step(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"id", "assistant", "action", "inputs"}
        and isinstance(value["id"], str)
        and ROUTINE_STEP_ID_RE.fullmatch(value["id"]) is not None
        and routine._assistant(value["assistant"])
        and routine._action(value["action"])
        and _members_listed(value["inputs"], routine.MAX_STEP_INPUTS, routine.MAX_MEMBER_CHARS)
    )


def canonical_routine_listing(value: object) -> dict[str, object] | None:
    """One Routine as the Brain sees it: its id, name, when it runs and where, revision, daily steps, output, steps."""
    if not isinstance(value, dict) or set(value) != _LISTING_FIELDS:
        return None
    steps = value["steps"]
    valid = (
        routine._identity(value["routine_id"], routine.ROUTINE_ID_RE)
        and routine.canonical_name(value["name"]) == value["name"]
        and routine.canonical_schedule(value["schedule"]) == value["schedule"]
        and routine.zoned(value["timezone"], value["timezone_source"])
        and routine._whole(value["revision"], 1, 2**31 - 1)
        and routine._whole(value["daily_steps"], 1, MAX_LISTED_DAILY_STEPS)
        and isinstance(steps, list)
        and len(steps) <= routine.MAX_ROUTINE_STEPS
        and routine_proposal._card_output(value["output"], len(steps))
        and all(_listed_step(item) for item in steps)
    )
    return copy.deepcopy(value) if valid else None


def canonical_routine_listings(value: object) -> list[dict[str, object]] | None:
    """The Team's whole Routine listing: at most MAX_ROUTINES, each admitted, no Routine twice."""
    if not isinstance(value, list | tuple) or len(value) > routine.MAX_ROUTINES:
        return None
    listed = [canonical_routine_listing(item) for item in value]
    if any(item is None for item in listed) or len({item["routine_id"] for item in listed}) != len(listed):
        return None
    return listed if list(value) == listed else None


# The work a pending unsourced or rerun question asks the Brain to run again (ADR-0101): one entry per call, or per
# ``count`` consecutive identical calls, at most a plan's steps in all. Each input member has its kind: ``value`` with
# its exact JSON text (null when withheld) and whether it is a target the person chose; ``clock``, the run's date; or
# ``fresh``, never shown, with the Action whose earlier result held it, if any.
MAX_RERUN_ENTRIES = routine.MAX_ROUTINE_STEPS
MAX_RERUN_CALLS = routine.MAX_ROUTINE_STEPS
MAX_RERUN_INPUTS = 64
MAX_RERUN_MEMBER_CHARS = 128
MAX_RERUN_LITERAL_CHARS = 1024


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
        and routine._assistant(value["assistant"])
        and routine._action(value["action"])
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
    return valid and routine._plain(value["member"], MAX_RERUN_MEMBER_CHARS)


def _rerun_entry(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"assistant", "action", "count", "inputs"}:
        return False
    inputs = value["inputs"]
    return (
        routine._assistant(value["assistant"])
        and routine._action(value["action"])
        and routine._whole(value["count"], 1, MAX_RERUN_CALLS)
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
