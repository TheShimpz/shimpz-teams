"""A Routine change the Brain compiled from the user's own message, and Team's authoritative check of it (ADR-0092).

The Brain proposes one closed change: ``create``, or ``update`` of a listed Routine at the revision it saw, with a
short name, the user's own words that state the standing request, a schedule, an optional timezone, and at most eight
ordered steps. Each step names one exact Assistant Action and gives every input member one source: a literal with its
provenance, a run-clock token, an earlier step's output selected by an RFC 6901 pointer with the user's own words that
relate the two, or, in an update, the member's source kept exactly as the current revision has it.

Team recomputes everything against the committed message. The user's own words are the text outside quoted, fenced,
and block-quoted regions and outside any span the Team's clarification lineage marks as not the user's. Every scalar
of a literal must equal text cited from those words, or from one quoted region that unquoted words adopt, or the whole
literal must equal its destination's declared schema default. Mechanical provenance proves where a value came from,
never that the user meant it; the compiled plan is then admitted against the exact current Action contracts, which
derive every pin, so no field of the change can assert approval or elevate authority.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from protocol.http.v1 import routine as http_routine
from routine import plan as routine_plan
from routine import schedule as routine_schedule

MAX_CHANGE_BYTES = 96 * 1024
MAX_ORIGINS = 64
_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
# The user's own words exclude quoted, fenced, and block-quoted material, exactly as the Brain separates them.
_QUOTED_RE = re.compile(
    r"```[\s\S]*?```|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|‘[^’\n]*’|«[^»\n]*»"
    r"|(?:^|(?<=\s))'[^'\n]*'(?=\s|$|[.,;:!?])|^[ \t]*>[^\n]*",
    re.MULTILINE,
)
_FIELDS = frozenset({"op", "routine_id", "expected_revision", "name", "request", "schedule", "timezone", "steps"})
_STEP_FIELDS = frozenset({"id", "assistant", "action", "input"})
_SOURCE_FIELDS = {
    "literal": frozenset({"kind", "value", "origins"}),
    "run_clock": frozenset({"kind", "format"}),
    "step_output": frozenset({"kind", "step", "pointer", "instruction"}),
    "kept": frozenset({"kind"}),
}
_ORIGIN_FIELDS = frozenset({"at", "from", "text", "region", "instruction"})


class ChangeError(ValueError):
    """The change was refused; ``code`` is the stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Change:
    op: str
    routine_id: str | None
    expected_revision: int | None
    name: str
    request: str
    schedule: dict[str, object]
    timezone: str | None
    steps: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "op": self.op,
            "routine_id": self.routine_id,
            "expected_revision": self.expected_revision,
            "name": self.name,
            "request": self.request,
            "schedule": dict(self.schedule),
            "timezone": self.timezone,
            "steps": copy.deepcopy(list(self.steps)),
        }


@dataclass(frozen=True, slots=True)
class Compiled:
    """A change Team admitted: the definition it commits and the plan admitted against the current contracts."""

    name: str
    quote: str
    schedule: dict[str, object]
    timezone: str
    document: dict[str, object]
    plan: routine_plan.Plan

    @property
    def assistants(self) -> tuple[str, ...]:
        return tuple(sorted({step.assistant_id for step in self.plan.steps}))


def _text(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= routine_plan.MAX_PLAN_BYTES


def _origin(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _ORIGIN_FIELDS:
        return False
    kind, text, region, instruction = value["from"], value["text"], value["region"], value["instruction"]
    shapes = {
        "message": _text(text) and region is None and instruction is None,
        "quote": _text(text) and type(region) is int and region >= 0 and _text(instruction),
        "default": value["at"] == "" and text is None and region is None and instruction is None,
    }
    return routine_plan.pointer_tokens(value["at"]) is not None and shapes.get(kind, False)


def _source(value: object) -> bool:
    kind = value.get("kind") if isinstance(value, dict) else None
    if not isinstance(kind, str) or kind not in _SOURCE_FIELDS or set(value) != _SOURCE_FIELDS[kind]:
        return False
    if kind == "literal":
        origins = value["origins"]
        return isinstance(origins, list) and 0 < len(origins) <= MAX_ORIGINS and all(map(_origin, origins))
    if kind == "step_output":
        return _text(value["instruction"])
    return True


def _step(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _STEP_FIELDS
        and isinstance(value["id"], str)
        and routine_plan.STEP_ID_RE.fullmatch(value["id"]) is not None
        and isinstance(value["assistant"], str)
        and routine_plan.ASSISTANT_ID_RE.fullmatch(value["assistant"]) is not None
        and isinstance(value["action"], str)
        and routine_plan.ACTION_ID_RE.fullmatch(value["action"]) is not None
        and isinstance(value["input"], dict)
        and all(_source(source) for source in value["input"].values())
    )


def parse(value: object) -> Change:
    """Admit one compiled change in its closed shape; nothing about its sources is trusted yet."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ChangeError("routine-change-invalid")
    try:
        encoded = routine_plan.canonical(value)
    except (TypeError, ValueError) as exc:
        raise ChangeError("routine-change-invalid") from exc
    op, routine_id, revision, steps = value["op"], value["routine_id"], value["expected_revision"], value["steps"]
    target = (
        (routine_id, revision) == (None, None)
        if op == "create"
        else op == "update"
        and isinstance(routine_id, str)
        and _ID_RE.fullmatch(routine_id) is not None
        and type(revision) is int
        and revision >= 1
    )
    timezone = value["timezone"]
    schedule = http_routine.canonical_schedule(value["schedule"])
    if (
        len(encoded) > MAX_CHANGE_BYTES
        or not target
        or http_routine.canonical_name(value["name"]) is None
        or http_routine.canonical_quote(value["request"]) is None
        or schedule is None
        or (timezone is not None and http_routine.canonical_timezone(timezone) is None)
        or not isinstance(steps, list)
        or not 0 < len(steps) <= routine_plan.MAX_STEPS
        or not all(map(_step, steps))
    ):
        raise ChangeError("routine-change-invalid")
    return Change(op, routine_id, revision, value["name"], value["request"], schedule, timezone, tuple(steps))


class Words:
    """Where a message's text may come from: the user's own words, and its numbered quoted regions."""

    def __init__(self, message: str, excluded: tuple[tuple[int, int], ...] = ()) -> None:
        self.message = message
        regions = [(match.start(), match.end()) for match in _QUOTED_RE.finditer(message)]
        blocked = sorted([*regions, *excluded])
        self.own: list[tuple[int, int]] = []
        cursor = 0
        for start, end in blocked:
            if start > cursor:
                self.own.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < len(message):
            self.own.append((cursor, len(message)))
        # A quoted region that a lineage span overlaps is not the user's either.
        self.quoted = [
            (start, end) for start, end in regions if not any(start < stop and begin < end for begin, stop in excluded)
        ]

    def mine(self, text: str) -> bool:
        """Whether the text is the user's own words, inside one stretch of them."""
        return bool(text) and any(text in self.message[start:end] for start, end in self.own)

    def adopted(self, region: int, text: str, instruction: str) -> bool:
        """Whether the text is inside one quoted region and the user's own words adopt it."""
        if not (0 <= region < len(self.quoted) and text and self.mine(instruction)):
            return False
        start, end = self.quoted[region]
        return text in self.message[start:end]


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _leaves(value: object, at: str = "") -> Iterator[str]:
    """The pointer of every scalar, and of every empty container, inside one literal."""
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _leaves(item, f"{at}/{_escape(key)}")
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            yield from _leaves(item, f"{at}/{index}")
    else:
        yield at


def _cited(origin: Mapping[str, object], target: object, words: Words) -> bool:
    """Whether one scalar equals the text its origin cites from the user's own or adopted words."""
    text = origin["text"]
    if origin["from"] == "message":
        found = words.mine(text)
    else:
        found = words.adopted(origin["region"], text, origin["instruction"])
    if not found or isinstance(target, bool) or target is None:
        return False
    if isinstance(target, str):
        return target == text
    if not isinstance(target, int | float) or _NUMBER_RE.fullmatch(text) is None:
        return False
    parsed = json.loads(text)
    return type(parsed) is type(target) and parsed == target


def _literal(source: Mapping[str, object], member: Mapping[str, object], words: Words) -> dict[str, object]:
    """A literal whose every scalar has exactly one origin, or whose whole value is its destination's default."""
    value = source["value"]
    leaves = list(_leaves(value))
    covered: list[str] = []
    for origin in source["origins"]:
        if origin["from"] == "default":
            default_valid = "default" in member and routine_plan.canonical(member["default"]) == (
                routine_plan.canonical(value)
            )
            if not default_valid:
                raise ChangeError("routine-literal-unproven")
            covered.extend(leaves)
            continue
        try:
            target = routine_plan.select(value, origin["at"])
        except routine_plan.PlanError as exc:
            raise ChangeError("routine-literal-unproven") from exc
        if origin["at"] not in leaves or not _cited(origin, target, words):
            raise ChangeError("routine-literal-unproven")
        covered.append(origin["at"])
    if sorted(covered) != sorted(leaves):
        raise ChangeError("routine-literal-unproven")
    return {"kind": "literal", "value": copy.deepcopy(value)}


def _plan_source(
    name: str,
    source: Mapping[str, object],
    schema: Mapping[str, object],
    kept: Mapping[str, object] | None,
    words: Words,
) -> dict[str, object]:
    kind = source["kind"]
    if kind == "literal":
        properties = schema.get("properties", {})
        member = properties.get(name) if isinstance(properties, dict) else None
        return _literal(source, member if isinstance(member, dict) else {}, words)
    if kind == "run_clock":
        return {"kind": "run_clock", "format": source["format"]}
    if kind == "step_output":
        if not words.mine(source["instruction"]):
            raise ChangeError("routine-reference-unproven")
        return {"kind": "step_output", "step": source["step"], "pointer": source["pointer"]}
    if kept is None or name not in kept:
        raise ChangeError("routine-kept-invalid")
    return copy.deepcopy(kept[name])


def compile_change(
    change: Change,
    words: Words,
    contracts: Mapping[tuple[str, str], routine_plan.ActionContract],
    current: Mapping[str, object] | None,
    default_timezone: str,
) -> Compiled:
    """Admit a parsed change against the committed message and the exact current contracts; refuse anything unproven.

    An update also names the current revision's plan document, whose sources a ``kept`` member copies exactly.
    """
    if (change.op == "update") != (current is not None):
        raise ChangeError("routine-change-invalid")
    if not words.mine(change.request):
        raise ChangeError("routine-request-unproven")
    timezone = change.timezone or default_timezone
    try:
        routine_schedule.zone(timezone)
    except routine_schedule.ScheduleError as exc:
        raise ChangeError("routine-timezone-invalid") from exc
    previous = {} if current is None else {step["id"]: step for step in current["steps"]}
    steps = []
    for raw in change.steps:
        contract = contracts.get((raw["assistant"], raw["action"]))
        if contract is None:
            raise ChangeError("routine-action-unavailable")
        before = previous.get(raw["id"])
        same = before is not None and (before["assistant"], before["action"]) == (raw["assistant"], raw["action"])
        kept = before["input"] if same else None
        inputs = {
            name: _plan_source(name, source, contract.input_schema, kept, words)
            for name, source in raw["input"].items()
        }
        identity = {"id": raw["id"], "assistant": raw["assistant"], "action": raw["action"]}
        steps.append({**identity, "pin": contract.pin, "input": inputs})
    document = {"version": routine_plan.VERSION, "timezone": timezone, "steps": steps}
    try:
        plan = routine_plan.admit(document, contracts)
    except routine_plan.PlanError as exc:
        raise ChangeError(exc.code) from exc
    return Compiled(change.name, change.request, dict(change.schedule), timezone, document, plan)
