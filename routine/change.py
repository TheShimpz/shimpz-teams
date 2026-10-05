"""A Routine change the Brain compiled from the user's own message, and Team's authoritative check of it (ADR-0092).

The Brain proposes one closed change: ``create``, or ``update`` of a listed Routine at the revision it saw, with a short
name, the user's own words that state the standing request, a schedule, an optional timezone, and at most eight ordered
steps. Each step names one exact Assistant Action and gives every input member one source: a literal with its
provenance, a run-clock token, an earlier step's output selected by an RFC 6901 pointer with the user's own words that
relate the two, or, in an update, the member's source kept exactly as the current revision has it.

Team recomputes everything against the committed message and the person's own earlier sends it cites, each parsed on its
own; the standing request must be the message's own words. The user's own words are the text outside quoted, fenced, and
block-quoted regions. Every scalar of a literal must equal text cited from those words, or from one quoted region that
unquoted words adopt, or the whole literal must equal its destination's declared schema default; only the one field a
Routine question leaves open is instead filled from the option the user selects (``Question``). Mechanical provenance
proves where a value came from, never that the user meant it; the compiled plan is then admitted against the exact
current Action contracts, which derive every pin, so no field of the change can assert approval or elevate authority.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from assistant import manifest as assistant_manifest
from protocol.http.v1 import routine as http_routine
from routine import plan as routine_plan
from routine import request as routine_request
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
_QUESTION_FIELDS = frozenset({"field", "values", "reply"})
MAX_REPLY_CHARS = 280
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# The origin of the one value a bound Routine question's selected option fills; nothing else may claim it.
ANSWER = {"at": "", "from": "answer", "text": None, "region": None, "instruction": None}


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
    # Where the user's own words state the request, as a span of the committed message.
    quote_span: tuple[int, int] = (0, 0)
    # Each step input's validated provenance, by step id and member: a literal's origins, a reference's
    # relating words, or nothing for a run-clock token.
    sources: dict[str, dict[str, dict[str, object]]] = dataclasses.field(default_factory=dict)

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
        "answer": value == ANSWER,
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
    """Where a Routine's text may come from: the user's own words and the numbered quoted regions of each part.

    The parts are the person's own earlier sends the message refers to, oldest first, then the message itself. Each is
    parsed on its own, so no quoted region or stretch of own words ever crosses from one part into another; they join
    with one separator only to give every span one coordinate space.
    """

    def __init__(self, message: str, earlier: tuple[str, ...] = ()) -> None:
        parts = (*earlier, message)
        self.message = routine_request.SEPARATOR.join(parts)
        self.quoted: list[tuple[int, int]] = []
        self.own: list[tuple[int, int]] = []
        offset = 0
        for part in parts:
            self._parse(part, offset)
            offset += len(part) + len(routine_request.SEPARATOR)
        # Where the message itself starts: the standing request must be its own words.
        self.current = len(self.message) - len(message)

    def _parse(self, part: str, offset: int) -> None:
        cursor = 0
        for match in _QUOTED_RE.finditer(part):
            start, end = match.start(), match.end()
            self.quoted.append((offset + start, offset + end))
            if start > cursor:
                self.own.append((offset + cursor, offset + start))
            cursor = max(cursor, end)
        if cursor < len(part):
            self.own.append((offset + cursor, offset + len(part)))

    def current_span(self, text: str) -> tuple[int, int] | None:
        """Where the text first stands inside one stretch of the message's own words, never an earlier send's."""
        for start, end in self.own if text else ():
            found = self.message.find(text, start, end) if start >= self.current else -1
            if found >= 0:
                return found, found + len(text)
        return None

    def mine(self, text: str) -> bool:
        """Whether the text is the user's own words, inside one stretch of them."""
        return self.span(text) is not None

    def span(self, text: str) -> tuple[int, int] | None:
        """Where the text first stands inside one stretch of the user's own words, or None."""
        for start, end in self.own if text else ():
            found = self.message.find(text, start, end)
            if found >= 0:
                return found, found + len(text)
        return None

    def adopted(self, region: int, text: str, instruction: str) -> bool:
        """Whether the text is inside one quoted region and the user's own words adopt it."""
        return self.quoted_span(region, text) is not None and self.mine(instruction)

    def quoted_span(self, region: int, text: str) -> tuple[int, int] | None:
        """Where the text first stands inside one quoted region, or None."""
        if not (0 <= region < len(self.quoted) and text):
            return None
        start, end = self.quoted[region]
        found = self.message.find(text, start, end)
        return None if found < 0 else (found, found + len(text))


def _cited_span(origin: Mapping[str, object], words: Words) -> dict[str, object]:
    """One validated origin as normalized spans of the committed message, never the cited prose itself."""
    kind = origin["from"]
    if kind == "message":
        return {"at": origin["at"], "from": kind, "span": list(words.span(origin["text"]))}
    if kind == "quote":
        return {
            "at": origin["at"],
            "from": kind,
            "region": origin["region"],
            "span": list(words.quoted_span(origin["region"], origin["text"])),
            "instruction": list(words.span(origin["instruction"])),
        }
    return {"at": origin["at"], "from": kind}


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


def _literal(
    source: Mapping[str, object], member: Mapping[str, object], words: Words, *, selected: bool = False
) -> dict[str, object]:
    """A literal whose every scalar has exactly one origin, or whose whole value is its destination's default.

    Only the member a bound Routine question leaves open, ``selected``, holds the value of the option the user picks.
    """
    value = source["value"]
    if selected or any(origin["from"] == "answer" for origin in source["origins"]):
        if not selected or source["origins"] != [ANSWER]:
            raise ChangeError("routine-literal-unproven")
        return {"kind": "literal", "value": copy.deepcopy(value)}
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


def _pending(proof: dict[str, object]) -> dict[str, object]:
    """New provenance, whose message, receipt, revision, and any selected answer the commit binds."""
    return {"proof": proof, "by": None}


def _plan_source(
    name: str,
    source: Mapping[str, object],
    schema: Mapping[str, object],
    kept: tuple[Mapping[str, object], Mapping[str, object]] | None,
    words: Words,
    selected: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    """One input's plan source and its provenance.

    A kept member copies both from the current revision, with the message, receipt, revision, and selected answer that
    first granted it.
    """
    kind = source["kind"]
    if selected and kind != "literal":
        raise ChangeError("routine-change-invalid")
    if kind == "literal":
        properties = schema.get("properties", {})
        member = properties.get(name) if isinstance(properties, dict) else None
        literal = _literal(source, member if isinstance(member, dict) else {}, words, selected=selected)
        return literal, _pending({"origins": [_cited_span(origin, words) for origin in source["origins"]]})
    if kind == "run_clock":
        return {"kind": "run_clock", "format": source["format"]}, _pending({})
    if kind == "step_output":
        relation_span = words.span(source["instruction"])
        if relation_span is None:
            raise ChangeError("routine-reference-unproven")
        relation = _pending({"instruction": list(relation_span)})
        return {"kind": "step_output", "step": source["step"], "pointer": source["pointer"]}, relation
    if kept is None or name not in kept[0] or name not in kept[1]:
        raise ChangeError("routine-kept-invalid")
    return copy.deepcopy(kept[0][name]), copy.deepcopy(kept[1][name])


def compile_change(
    change: Change,
    words: Words,
    contracts: Mapping[tuple[str, str], routine_plan.ActionContract],
    current: tuple[Mapping[str, object], Mapping[str, object]] | None,
    default_timezone: str,
    selected: tuple[str, str] | None = None,
) -> Compiled:
    """Admit a parsed change against the committed message and the exact current contracts; refuse anything unproven.

    An update also names the current revision's plan document and its inputs' provenance, which a ``kept`` member
    copies exactly. ``selected`` names the one step input a bound Routine question fills from the option the user
    selects.
    """
    if (change.op == "update") != (current is not None):
        raise ChangeError("routine-change-invalid")
    quote_span = words.current_span(change.request)
    if quote_span is None:
        raise ChangeError("routine-request-unproven")
    if assistant_manifest.resembles_credential(change.request) or assistant_manifest.resembles_credential(change.name):
        # The request and name are kept and shown in plaintext; a secret never becomes either.
        raise ChangeError("routine-request-secret")
    timezone = change.timezone or default_timezone
    try:
        routine_schedule.zone(timezone)
    except routine_schedule.ScheduleError as exc:
        raise ChangeError("routine-timezone-invalid") from exc
    previous = {} if current is None else {step["id"]: step for step in current[0]["steps"]}
    steps, sources = [], {}
    for raw in change.steps:
        contract = contracts.get((raw["assistant"], raw["action"]))
        if contract is None:
            raise ChangeError("routine-action-unavailable")
        before = previous.get(raw["id"])
        same = before is not None and (before["assistant"], before["action"]) == (raw["assistant"], raw["action"])
        kept = (before["input"], current[1].get(raw["id"], {})) if same else None
        admitted = {
            name: _plan_source(name, source, contract.input_schema, kept, words, selected == (raw["id"], name))
            for name, source in raw["input"].items()
        }
        identity = {"id": raw["id"], "assistant": raw["assistant"], "action": raw["action"]}
        steps.append({**identity, "pin": contract.pin, "input": {name: item[0] for name, item in admitted.items()}})
        sources[raw["id"]] = {name: item[1] for name, item in admitted.items()}
    document = {"version": routine_plan.VERSION, "timezone": timezone, "steps": steps}
    try:
        plan = routine_plan.admit(document, contracts)
    except routine_plan.PlanError as exc:
        raise ChangeError(exc.code) from exc
    return Compiled(change.name, change.request, dict(change.schedule), timezone, document, plan, quote_span, sources)


@dataclass(frozen=True, slots=True)
class Question:
    """A Routine question: the candidate change with exactly one field left open, one complete change per option.

    ``field`` is ``("schedule",)``, ``("timezone",)``, or ``("input", step_id, member)``. Each change differs from the
    candidate only in that field, which holds its option's value; ``reply`` is what the user is told once the change
    their selected option completes commits.
    """

    field: tuple[str, ...]
    changes: tuple[Change, ...]
    reply: str

    @property
    def selected(self) -> tuple[str, str] | None:
        return (self.field[1], self.field[2]) if self.field[0] == "input" else None


def _field(value: object) -> tuple[str, ...] | None:
    if value in ({"kind": "schedule"}, {"kind": "timezone"}):
        return (value["kind"],)
    if not isinstance(value, dict) or set(value) != {"kind", "step", "member"} or value["kind"] != "input":
        return None
    step, member = value["step"], value["member"]
    if not isinstance(step, str) or routine_plan.STEP_ID_RE.fullmatch(step) is None or not _text(member):
        return None
    return ("input", step, member)


def _filled(candidate: dict[str, object], field: tuple[str, ...], value: object) -> dict[str, object]:
    """The candidate with its one open field set to one option's value; the field must be open in the candidate."""
    filled = copy.deepcopy(candidate)
    if field[0] == "input":
        step = next((item for item in filled["steps"] if isinstance(item, dict) and item.get("id") == field[1]), None)
        if step is None or not isinstance(step.get("input"), dict) or field[2] in step["input"]:
            raise ChangeError("routine-question-invalid")
        step["input"][field[2]] = {"kind": "literal", "value": copy.deepcopy(value), "origins": [dict(ANSWER)]}
        return filled
    if filled[field[0]] is not None:
        raise ChangeError("routine-question-invalid")
    filled[field[0]] = value
    return filled


def _reply(value: object) -> bool:
    """One NFC line of at most 280 characters with no control, format, private, or unassigned character."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_REPLY_CHARS
        and unicodedata.normalize("NFC", value).strip() == value
        and not any(unicodedata.category(item) in _UNSAFE_CATEGORIES for item in value)
    )


def parse_question(value: object, options: int) -> Question:
    """Admit one Routine question whose ``options`` visible choices each carry exactly one value of its open field."""
    if not isinstance(value, dict) or set(value) != {*_FIELDS, "question"}:
        raise ChangeError("routine-question-invalid")
    question = value["question"]
    if not isinstance(question, dict) or set(question) != _QUESTION_FIELDS:
        raise ChangeError("routine-question-invalid")
    field, values = _field(question["field"]), question["values"]
    if field is None or not isinstance(values, list) or len(values) != options or not _reply(question["reply"]):
        raise ChangeError("routine-question-invalid")
    try:
        distinct = len({routine_plan.canonical(item) for item in values}) == len(values)
    except (TypeError, ValueError) as exc:
        raise ChangeError("routine-question-invalid") from exc
    if not distinct:
        raise ChangeError("routine-question-invalid")
    candidate = {key: value[key] for key in _FIELDS}
    try:
        changes = tuple(parse(_filled(candidate, field, item)) for item in values)
    except ChangeError as exc:
        raise ChangeError("routine-question-invalid") from exc
    return Question(field, changes, question["reply"])
