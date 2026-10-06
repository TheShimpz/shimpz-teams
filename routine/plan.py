"""The recorded plan of a Routine and the resolution of each step's inputs, without I/O (ADR-0092 §3, ADR-0101).

A plan is versioned declarative JSON: at most 256 ordered steps (its admission budget below), each naming one exact
Assistant Action by its complete pin, the same Action as often as the work needs with its own inputs, and giving each
top-level input member it holds exactly one value: a ``literal``, the ``run_clock`` date of the run's one immutable
start instant in the plan's timezone, or a ``step_output`` that copies one JSON value from a completed earlier step of
the same run, selected by an RFC 6901 pointer, or through the one array item whose ``where`` member equals a constant
and then the item's own ``item`` pointer. Every required member is present. There is no coercion, interpolation,
expression, branch, loop, or cross-run lookup. Outputs can only fill inputs: they never choose an Assistant, an Action,
a schedule, or a step. A missing path, an invalid index or escape, a selector matching no item or several, and a value
its destination schema refuses (null included) fail closed before dispatch. Only the selected values are retained, never
a complete output, and a literal that a secret belongs in is refused: such a value must be the Action's declared Stored
Input. An Action that declares a file input is refused: a Routine holds no file grant, so no literal id or copied output
may stand for an attached file (ADR-0093).

A plan also states what a completed run does with its result: ``show`` one step's result to the person after every run,
show it only when it ``changes``, show ``none`` of it, or ``decide``: a decision turn judges the run's results
``always``, or only when they ``changes``. A ``decide`` plan may have no steps at all (ADR-0101).
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from assistant import action_schema
from assistant import manifest as assistant_manifest
from protocol.http.v1 import identifiers as http_identifiers
from protocol.http.v1 import routine as http_routine
from routine import schedule

VERSION = 3

# The one admission budget (ADR-0092 amendment, 2026-10-05, scale). A plan holds at most 256 steps and one Action may
# repeat with its own inputs. Every other bound of a Routine's scale is stated here or derives from these, so none is
# an isolated maximum: a definition (its canonical plan and grant) fits its Routine's share and the Team's aggregate, so
# the Team's one state file holds every Routine at its bound; a step's resolved input fits one frozen continuation; a
# revision's active time grows with its steps up to a ceiling; and the business steps a Team's runs may start in any
# rolling 24 hours are capped. The wire bounds live in the standalone Team HTTP protocol, which this reads, never the
# reverse.
MAX_STEPS = http_routine.MAX_ROUTINE_STEPS
# The canonical plan document of one revision.
MAX_PLAN_BYTES = 256 * 1024
# One Routine's canonical plan and grant together; the grant repeats per input what granted it, so it is bounded here
# rather than compacted.
MAX_DEFINITION_BYTES = 512 * 1024
# Every definition a Team holds, together: at most two Routines at their own bound, or many smaller ones.
TEAM_DEFINITION_BYTES = 1024 * 1024
# One step's fully resolved input, canonical: literals, run-clock tokens, and values earlier steps returned. A larger
# one is refused before its dispatch, so a frozen run's continuation always holds its one pending request.
MAX_RESOLVED_INPUT_BYTES = 128 * 1024
# The business steps a Team's runs may start in any rolling 24 hours. Each start reserves its revision's whole step
# count; verifier calls and the one retry are bounded apart by each run's recovery budgets, and a continuation after a
# hold or a person's answer starts nothing.
MAX_DAILY_STEPS = 20_000
# A claimed run must start its segment within this window; the segment then extends its lease over its active time.
START_LEASE_SECONDS = 900
LEASE_MARGIN_SECONDS = 300
SHORT_ACTIVE_SECONDS = http_routine.SHORT_ACTIVE_SECONDS
MAX_ACTIVE_SECONDS = http_routine.MAX_ACTIVE_SECONDS


def active_seconds(steps: int) -> int:
    """The active time one run of a revision with ``steps`` steps may spend, which a hold never refills."""
    return http_routine.active_seconds(steps)


def long_run(steps: int) -> bool:
    """Whether a run of this many steps is long: Admin's workers hold at most one long run at a time."""
    return active_seconds(steps) > SHORT_ACTIVE_SECONDS


MAX_RETAINED_BYTES = 128 * 1024
MAX_POINTER = 256
STEP_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
PIN_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_POINTER_RE = re.compile(r"(?:/(?:[^/~]|~[01])*)*\Z")
_INDEX_RE = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
# The one run-clock token a recording infers: the run's date in the plan's timezone (ADR-0101).
CLOCK_FORMATS = ("date",)
# What a completed run does with its result; ``show`` and ``changes`` name the step whose result is shown, and
# ``decide`` hands every result to a decision turn, ``always`` or only when the results ``changes``.
OUTPUT_MODES = http_routine.OUTPUT_MODES
SHOWN_MODES = http_routine.SHOWN_MODES
DECISION_WHEN = http_routine.DECISION_WHEN
# A secret is never a literal: one of these in a destination name, or a destination marked write-only or as a password.
_SECRET_MARKERS = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
    "credential",
)
# Literal nesting and schema indirection checked for secret destinations; anything deeper is refused.
MAX_SECRET_DEPTH = 64
_SAMPLE_INSTANT = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


class PlanError(ValueError):
    """A plan or one of its resolutions was refused; ``code`` is the stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Step:
    step_id: str
    assistant_id: str
    action: str
    pin: str
    inputs: Mapping[str, Mapping[str, object]]
    # The Action's reviewed output schema, which orders and redacts a shown result; never part of the document.
    output_schema: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    # The Action's reviewed input schema, which redacts the inputs a run's step record shows; never in the document.
    input_schema: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Plan:
    timezone: str
    steps: tuple[Step, ...]
    digest: str
    # What a completed run does with its result: {"mode": one of OUTPUT_MODES, "step": the shown step id or None,
    # "when": a decision's condition or None}.
    output: Mapping[str, object] = dataclasses.field(
        default_factory=lambda: {"mode": "none", "step": None, "when": None}
    )

    def position(self, step_id: str) -> int:
        """A step's 1-based position, which names it on the wire (ADR-0092 amendment, 2026-10-05, scale)."""
        return next(index for index, step in enumerate(self.steps, start=1) if step.step_id == step_id)

    def shown(self) -> Step | None:
        """The step whose result a completed run shows, or None when the plan shows none."""
        if self.output["mode"] not in SHOWN_MODES:
            return None
        return next(step for step in self.steps if step.step_id == self.output["step"])

    def references(self, step_id: str) -> tuple[Selector, ...]:
        """The complete selectors later steps apply to ``step_id``'s output, sorted, each once."""
        return tuple(
            sorted(
                {
                    selector(source)[1:]
                    for step in self.steps
                    for source in step.inputs.values()
                    if source["kind"] == "step_output" and source["step"] == step_id
                }
            )
        )


# One selection from a step's output: its pointer, its canonical ``where`` text ("" when it selects by pointer
# alone), and its item pointer; two steps selecting different items of one array never share a key.
Selector = tuple[str, str, str]
# A selection's complete key in a run: the step it selects from, then its selector.
Key = tuple[str, str, str, str]


def selector(source: Mapping[str, object]) -> Key:
    """A ``step_output`` source's complete selection key."""
    where = source.get("where")
    return (
        source["step"],
        source["pointer"],
        "" if where is None else canonical(where).decode(),
        source.get("item", ""),
    )


@dataclass(frozen=True, slots=True)
class ActionContract:
    """What a plan needs of one Action: its current complete pin, its reviewed input schema, and its file inputs."""

    pin: str
    input_schema: Mapping[str, Any]
    input_files: tuple[str, ...] = ()
    output_schema: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    # Whether the reviewed effect proves the Action read-only; anything not proven counts as a change (ADR-0092 §4).
    read_only: bool = False
    # The Stored Inputs the Action uses, by name only, sorted.
    stored_inputs: tuple[str, ...] = ()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def admit(document: object, contracts: Mapping[tuple[str, str], ActionContract]) -> Plan:
    """Admit one plan at creation against the exact current contracts of the Actions it names."""
    timezone, raw_steps, output, encoded = _document(document)
    steps: list[Step] = []
    for raw in raw_steps:
        steps.append(_step(raw, contracts, tuple(item.step_id for item in steps)))
    _output(output, steps)
    return Plan(timezone, tuple(steps), "sha256:" + hashlib.sha256(encoded).hexdigest(), copy.deepcopy(output))


def well_formed(document: object) -> bool:
    """Whether a kept plan still has the closed shape and bounds of an admitted one; its contracts are checked apart."""
    try:
        _timezone, raw_steps, output, _encoded = _document(document)
        shapes: list[tuple[str, str, str, str, dict[str, object]]] = []
        for raw in raw_steps:
            shapes.append(_step_shape(raw, tuple(shape[0] for shape in shapes)))
        _output(output, [Step(shape[0], shape[1], shape[2], shape[3], shape[4]) for shape in shapes])
    except PlanError:
        return False
    return True


def _document(document: object) -> tuple[str, list[object], object, bytes]:
    """A plan's timezone, raw steps, raw output, and canonical bytes, within its closed top-level shape and bounds."""
    if not isinstance(document, dict) or set(document) != {"version", "timezone", "steps", "output"}:
        raise PlanError("plan-invalid")
    try:
        encoded = canonical(document)
    except (TypeError, ValueError) as exc:
        raise PlanError("plan-invalid") from exc
    if document["version"] != VERSION or len(encoded) > MAX_PLAN_BYTES:
        raise PlanError("plan-invalid")
    timezone = document["timezone"]
    try:
        schedule.zone(timezone)
    except schedule.ScheduleError as exc:
        raise PlanError("plan-timezone-invalid") from exc
    raw_steps = document["steps"]
    if not isinstance(raw_steps, list) or len(raw_steps) > MAX_STEPS:
        raise PlanError("plan-invalid")
    return timezone, raw_steps, document["output"], encoded


def _output(value: object, steps: list[Step]) -> None:
    """The run's output disposition: a shown step is one of the plan's; only a decision has a condition or no steps."""
    if not isinstance(value, dict) or set(value) != {"mode", "step", "when"} or value["mode"] not in OUTPUT_MODES:
        raise PlanError("plan-output-invalid")
    mode, shown, when = value["mode"], value["step"], value["when"]
    if mode in SHOWN_MODES:
        valid = isinstance(shown, str) and any(step.step_id == shown for step in steps)
    else:
        valid = shown is None
    # Only a decision has a condition, and only a decision may have no step to run.
    decided = when in DECISION_WHEN if mode == "decide" else when is None and bool(steps)
    if not (valid and decided):
        raise PlanError("plan-output-invalid")


def _step_shape(raw: object, earlier: tuple[str, ...]) -> tuple[str, str, str, str, dict[str, object]]:
    """One step's closed shape and value sources, before its Action contract is known."""
    if not isinstance(raw, dict) or set(raw) != {"id", "assistant", "action", "pin", "input"}:
        raise PlanError("plan-step-invalid")
    step_id, assistant_id, action, pin, inputs = (raw[key] for key in ("id", "assistant", "action", "pin", "input"))
    if (
        not _matches(step_id, STEP_ID_RE)
        or step_id in earlier
        or http_identifiers.canonical_assistant_id(assistant_id) is None
        or http_identifiers.canonical_action_id(action) is None
        or not _matches(pin, PIN_RE)
        or not isinstance(inputs, dict)
    ):
        raise PlanError("plan-step-invalid")
    for source in inputs.values():
        _source(source, earlier)
    return step_id, assistant_id, action, pin, inputs


def _step(raw: object, contracts: Mapping[tuple[str, str], ActionContract], earlier: tuple[str, ...]) -> Step:
    step_id, assistant_id, action, pin, inputs = _step_shape(raw, earlier)
    contract = contracts.get((assistant_id, action))
    if contract is None or contract.pin != pin:
        raise PlanError("plan-pin-drift")
    if contract.input_files:
        raise PlanError("plan-file-input")
    schema = contract.input_schema
    properties = schema.get("properties", {})
    if not set(inputs) <= set(properties) or not set(schema.get("required", ())) <= set(inputs):
        raise PlanError("plan-input-mismatch")
    # The whole input is the first position, so the root's own applicators and annotations derive every member's
    # destinations; a member that is not a literal counts only for presence and is never itself checked.
    supplied = {name: source.get("value", _NOT_LITERAL) for name, source in inputs.items()}
    if _secret_literal(schema, "", supplied, schema, 0):
        raise PlanError("plan-secret-literal")
    for name, source in inputs.items():
        _typed(name, source, schema)
    schemas = (copy.deepcopy(contract.output_schema), copy.deepcopy(dict(schema)))
    return Step(step_id, assistant_id, action, pin, copy.deepcopy(inputs), *schemas)


def _matches(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


_SOURCE_FIELDS = (
    frozenset({"kind", "value"}),
    frozenset({"kind", "format"}),
    frozenset({"kind", "step", "pointer"}),
    frozenset({"kind", "step", "pointer", "where", "item"}),
)
_SOURCES = tuple(zip(("literal", "run_clock", "step_output", "step_output"), _SOURCE_FIELDS, strict=True))


def _source(source: object, earlier: tuple[str, ...]) -> None:
    """One value source's closed shape: a literal, the run date, or a reference to an earlier step's output."""
    kind = source.get("kind") if isinstance(source, dict) else None
    if not any(kind == name and set(source) == fields for name, fields in _SOURCES):
        raise PlanError("plan-input-invalid")
    if kind == "literal" and _holds_credential(source["value"]):
        raise PlanError("plan-secret-literal")
    if kind == "run_clock" and source["format"] not in CLOCK_FORMATS:
        raise PlanError("plan-input-invalid")
    if kind == "step_output" and (
        source["step"] not in earlier
        or pointer_tokens(source["pointer"]) is None
        or ("where" in source and not (_where(source["where"]) and pointer_tokens(source["item"]) is not None))
    ):
        raise PlanError("plan-reference-invalid")


def _where(value: object) -> bool:
    """A selector's one member, whose value is a string or an integer, never a boolean."""
    return (
        isinstance(value, dict)
        and len(value) == 1
        and all(isinstance(key, str) for key in value)
        and all(isinstance(constant, str) or type(constant) is int for constant in value.values())
    )


def _typed(name: str, source: Mapping[str, object], schema: Mapping[str, Any]) -> None:
    """A literal or run-clock value must fit its destination; an output reference is typed when it resolves."""
    if source["kind"] == "literal":
        _admit_member(schema, name, source["value"])
    elif source["kind"] == "run_clock":
        _admit_member(schema, name, clock_value(source["format"], _SAMPLE_INSTANT, "UTC"))


def _secret_literal(root: Mapping[str, Any], name: str, value: object, subschema: object, depth: int) -> bool:
    """Whether a literal reaches a secret destination anywhere inside it.

    The whole step input is the first position, checked against the input schema root, so the root's own applicators
    derive each member's destinations exactly as nested ones do. Every position of the value is checked against
    every subschema that applies there. Applicators whose effect is exact are followed position by position: local
    ``$ref``, ``allOf``, ``anyOf``, ``oneOf``, ``dependentSchemas`` of a present member, object ``properties``,
    ``patternProperties`` (matched by the linear-time matcher), and ``additionalProperties``, and array
    ``prefixItems`` and ``items``. A member whose name marks a secret, or a destination annotated ``writeOnly`` or
    ``format: password``, refuses the whole literal. Every other applicator (``if``, ``then``, ``else``, ``not``,
    ``contains``, ``unevaluatedItems``, ``contentSchema``, ``propertyNames``) is not modelled, so a literal is
    refused when anything that one reaches could hold a secret. A value nested deeper than the bound is refused
    rather than left unchecked.
    """
    if value is _NOT_LITERAL:
        return False
    if depth > MAX_SECRET_DEPTH:
        return True
    candidates = applicable(root, subschema, 0, value)
    if secret_position(root, name, candidates):
        return True
    if isinstance(value, dict):
        return any(
            _secret_literal(root, key, item, member_schemas(candidates, key), depth + 1) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(
            _secret_literal(root, name, item, item_schemas(candidates, index), depth + 1)
            for index, item in enumerate(value)
        )
    return False


def secret_literal(schema: Mapping[str, Any], supplied: Mapping[str, object]) -> bool:
    """Whether a step's whole input, every member taken as a literal, reaches a secret destination anywhere.

    The complete input is checked at once, so a dependent schema that a sibling member activates still applies.
    """
    return _secret_literal(schema, "", dict(supplied), schema, 0)


def secret_position(root: Mapping[str, Any], name: str, candidates: list[Mapping[str, Any]]) -> bool:
    """Whether one position itself is a secret destination, or an unmodelled applicator there could reach one."""
    return (
        _secret_name(name.lower().replace("-", "_"))
        or any(_marked(item) for item in candidates)
        or any(
            _could_hold_secret(root, item[keyword], 0) for item in candidates for keyword in _UNMODELLED & item.keys()
        )
    )


# Stands for an input member that a run fills, not a literal: it counts for presence and is never checked as a secret.
_NOT_LITERAL = object()
# Stands for a value not at hand, such as an earlier step's output on the way to the value a pointer selects.
UNKNOWN = object()
# Applicators whose effect at a position is not modelled; anything they reach that could hold a secret refuses.
_UNMODELLED = frozenset({"if", "then", "else", "not", "contains", "unevaluatedItems", "contentSchema", "propertyNames"})


def _secret_name(lowered: str) -> bool:
    return any(marker in lowered for marker in _SECRET_MARKERS)


def _marked(subschema: Mapping[str, Any]) -> bool:
    return subschema.get("writeOnly") is True or subschema.get("format") == "password"


def applicable(root: Mapping[str, Any], subschema: object, depth: int, value: object) -> list[Mapping[str, Any]]:
    """The subschemas that apply at one position.

    That is the node, its local reference, its combinator members, and the ``dependentSchemas`` of each member the
    value holds.
    """
    if depth > MAX_SECRET_DEPTH:
        raise PlanError("plan-secret-literal")
    if not isinstance(subschema, dict):
        return []
    members: list[object] = []
    if isinstance(subschema.get("$ref"), str):
        members.append(action_schema.reference_target(root, subschema["$ref"]))
    for combinator in ("allOf", "anyOf", "oneOf"):
        if isinstance(subschema.get(combinator), list):
            members.extend(subschema[combinator])
    dependent = subschema.get("dependentSchemas")
    if isinstance(dependent, dict) and (value is UNKNOWN or isinstance(value, dict)):
        # A position whose value is unknown may hold any member, so every dependent schema applies.
        members.extend(item for key, item in dependent.items() if value is UNKNOWN or key in value)
    found: list[Mapping[str, Any]] = [subschema]
    for member in members:
        found.extend(applicable(root, member, depth + 1, value))
    return found


def member_schemas(candidates: list[Mapping[str, Any]], key: str) -> dict[str, Any]:
    """Every subschema the candidates apply to one object member.

    That is its property, each matching pattern, or else the candidate's additional-properties schema.
    """
    members: list[object] = []
    try:
        with action_schema.pattern_work_budget():
            for item in candidates:
                properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
                patterns = item.get("patternProperties") if isinstance(item.get("patternProperties"), dict) else {}
                matched = [
                    schema for pattern, schema in patterns.items() if action_schema.pattern_matches(pattern, key)
                ]
                members.extend([properties[key]] if key in properties else [])
                members.extend(matched)
                if key not in properties and not matched and isinstance(item.get("additionalProperties"), dict):
                    members.append(item["additionalProperties"])
    except action_schema.PatternError as exc:
        raise PlanError("plan-secret-literal") from exc
    return {"allOf": members}


def item_schemas(candidates: list[Mapping[str, Any]], index: int) -> dict[str, Any]:
    members = []
    for item in candidates:
        prefix = item.get("prefixItems")
        if isinstance(prefix, list) and index < len(prefix):
            members.append(prefix[index])
        elif "items" in item:
            members.append(item["items"])
    return {"allOf": members}


def _could_hold_secret(root: Mapping[str, Any], subschema: object, depth: int) -> bool:
    """Whether anything a subschema reaches, through every applicator and local reference, could hold a secret."""
    if depth > MAX_SECRET_DEPTH:
        return True
    if not isinstance(subschema, dict):
        return False
    properties = subschema.get("properties") if isinstance(subschema.get("properties"), dict) else {}
    if _marked(subschema) or any(_secret_name(key.lower().replace("-", "_")) for key in properties):
        return True
    reached = list(action_schema.applied_subschemas(subschema))
    if isinstance(subschema.get("$ref"), str):
        reached.append(action_schema.reference_target(root, subschema["$ref"]))
    return any(_could_hold_secret(root, item, depth + 1) for item in reached)


def _holds_credential(value: object) -> bool:
    if isinstance(value, str):
        return assistant_manifest.resembles_credential(value)
    if isinstance(value, list):
        return any(_holds_credential(item) for item in value)
    if isinstance(value, dict):
        return any(_holds_credential(key) or _holds_credential(item) for key, item in value.items())
    return False


def _admit_member(schema: Mapping[str, Any], name: str, value: object) -> None:
    """Validate one value against exactly its destination member, with the schema's own definitions."""
    member: dict[str, Any] = {
        "type": "object",
        "properties": {name: schema["properties"][name]},
        "required": [name],
        "additionalProperties": False,
    }
    for definitions in ("$defs", "definitions"):
        if definitions in schema:
            member[definitions] = schema[definitions]
    try:
        assistant_manifest.validate_schema_payload(assistant_manifest.action_schema_validator(member), {name: value})
    except ValueError as exc:
        raise PlanError("plan-input-type") from exc


def clock_value(form: str, instant: datetime.datetime, timezone: str) -> object:
    """The run date: the run's start instant as a date in the plan's timezone; ``form`` is always ``date``."""
    del form
    return instant.astimezone(schedule.zone(timezone)).date().isoformat()


def pointer_tokens(pointer: object) -> tuple[str, ...] | None:
    """Decode one RFC 6901 pointer string; the empty pointer selects the whole value."""
    if not isinstance(pointer, str) or len(pointer) > MAX_POINTER or _POINTER_RE.fullmatch(pointer) is None:
        return None
    if pointer == "":
        return ()
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/"))


def select(value: object, pointer: str) -> object:
    """The one value a pointer selects; a missing member or an invalid index fails closed."""
    tokens = pointer_tokens(pointer)
    if tokens is None:
        raise PlanError("plan-reference-invalid")
    current = value
    for token in tokens:
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and _INDEX_RE.fullmatch(token) is not None and int(token) < len(current):
            current = current[int(token)]
        else:
            raise PlanError("plan-reference-missing")
    return copy.deepcopy(current)


def same(left: object, right: object) -> bool:
    """Exact, type-sensitive JSON equality, with no lossy conversion (ADR-0101).

    A boolean never equals a number, two integers are equal only as integers, and a float equals an integer only when it
    is integral and converts to exactly that integer.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return _same_number(left, right)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right, strict=True))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same(left[key], right[key]) for key in left)
    return type(left) is type(right) and left == right


def _same_number(left: int | float, right: int | float) -> bool:
    if isinstance(left, float) and isinstance(right, float):
        return left == right
    if isinstance(left, int) and isinstance(right, int):
        return left == right
    number, whole = (left, right) if isinstance(left, float) else (right, left)
    return math.isfinite(number) and number.is_integer() and int(number) == whole


def select_where(value: object, pointer: str, where: object, item: str) -> object:
    """The value ``item`` selects inside the one array item whose ``where`` member is the given constant (ADR-0101).

    ``pointer`` must select an array; ``where`` is exactly one member whose value is a string or an integer. An item
    that is not an object, lacks the member, or holds another value or type does not match. No match fails closed as a
    missing reference, and several as an ambiguous one, so the dependent step is never dispatched.
    """
    if pointer_tokens(pointer) is None or pointer_tokens(item) is None or not _where(where):
        raise PlanError("plan-reference-invalid")
    items = select(value, pointer)
    if not isinstance(items, list):
        raise PlanError("plan-reference-missing")
    ((key, constant),) = where.items()
    matches = [entry for entry in items if isinstance(entry, dict) and key in entry and same(entry[key], constant)]
    if not matches:
        raise PlanError("plan-reference-missing")
    if len(matches) > 1:
        raise PlanError("plan-reference-ambiguous")
    return select(matches[0], item)


def selections(plan: Plan, step_id: str, result: object) -> dict[Selector, object]:
    """Every value later steps select from one completed step's result, keyed by selector."""
    return {key: _selected(result, key) for key in plan.references(step_id)}


def _selected(result: object, key: Selector) -> object:
    pointer, where, item = key
    return select(result, pointer) if not where else select_where(result, pointer, json.loads(where), item)


def retained_within(selected: Mapping[Key, object]) -> bool:
    """Whether a run's retained selections fit their byte bound."""
    return len(canonical([[*key, value] for key, value in sorted(selected.items())])) <= MAX_RETAINED_BYTES


def resolve(
    plan: Plan,
    step: Step,
    selected: Mapping[Key, object],
    started_at: int,
    validate: Callable[[dict[str, object]], object],
) -> dict[str, object]:
    """One step's complete input, validated against its Action's reviewed input schema before any dispatch."""
    instant = datetime.datetime.fromtimestamp(started_at, datetime.UTC)
    resolved: dict[str, object] = {}
    for name, source in step.inputs.items():
        if source["kind"] == "literal":
            resolved[name] = copy.deepcopy(source["value"])
        elif source["kind"] == "run_clock":
            resolved[name] = clock_value(source["format"], instant, plan.timezone)
        elif selector(source) in selected:
            resolved[name] = copy.deepcopy(selected[selector(source)])
        else:
            raise PlanError("plan-reference-missing")
    if len(canonical(resolved)) > MAX_RESOLVED_INPUT_BYTES:
        # A frozen run's continuation keeps its one pending request whole, so a larger input never dispatches.
        raise PlanError("plan-input-too-large")
    try:
        validate(resolved)
    except ValueError as exc:
        raise PlanError("plan-input-type") from exc
    return resolved


def commitment(resolved: Mapping[str, object]) -> str:
    """The digest binding one dispatch to its exact resolved input."""
    return hashlib.sha256(canonical(dict(resolved))).hexdigest()


# What a completed run shows of its result (ADR-0092 amendment, 2026-10-05, output).
#
# A Routine that shows a result shows one step's validated result, never a model's summary of it: healthy runs stay
# model-free. The result is first projected into its safe form: every value the step's reviewed output schema marks as
# secret (a secret-named member, ``writeOnly``, ``format: password``, or a position an unmodelled applicator could make
# secret, walked exactly as plan admission walks a literal), every credential-shaped string, and the value of every
# credential-shaped key is redacted; every key and text is escaped; and an object's members are in sorted key order,
# the canonical order of the pinned contract itself. Redaction always comes before any cut. The safe form is what a
# change is compared on, so a change only inside redacted content is never shown; the shown form is the safe form cut to
# a fixed ladder of bounds until its whole encoding fits ``MAX_OUTPUT_BYTES``, marking every cut and omission; labels
# that read alike after escaping or shortening are numbered apart. Projection never refuses a result: anything it
# cannot project is shown as unavailable instead, and nothing is replayed.

MAX_OUTPUT_DEPTH = http_routine.MAX_OUTPUT_DEPTH
MAX_OUTPUT_BYTES = http_routine.MAX_OUTPUT_BYTES
MAX_OUTPUT_KEY_CHARS = http_routine.MAX_OUTPUT_KEY_CHARS
# The bounds a shown result is cut to, (items, fields, text characters), each tried in turn until its encoding fits.
OUTPUT_LEVELS = (
    (http_routine.MAX_OUTPUT_ITEMS, http_routine.MAX_OUTPUT_FIELDS, http_routine.MAX_OUTPUT_TEXT_CHARS),
    (25, 24, 120),
    (12, 12, 60),
    (6, 8, 40),
    (3, 4, 20),
    (1, 2, 12),
    (0, 0, 0),
)
# The safe form a change is compared on; a larger one always compares as changed.
MAX_COMPARED_OUTPUT_BYTES = 1024 * 1024
# Nesting the safe form follows; anything deeper is elided, never followed further.
MAX_SAFE_OUTPUT_DEPTH = MAX_SECRET_DEPTH
OUTPUT_REDACTED_KEY = "[redacted]"
OUTPUT_REDACTED = {"kind": "redacted"}
OUTPUT_ELIDED = {"kind": "elided"}


class OutputError(ValueError):
    """A result could not be projected; the run shows its result as unavailable."""


def output_safe(result: object, schema: object, protected: frozenset[str] = frozenset()) -> dict[str, object]:
    """The complete safe form of one validated result under its Action's reviewed output schema.

    It keeps every key, text, and number exactly as the result has them, apart from what it redacts: also every string
    or key that holds a value the run protects (ADR-0101 section 6.2). A change is compared on the complete data;
    escaping, shortening, and every cut belong to the shown form alone.
    """
    root = schema if isinstance(schema, dict) else {}
    try:
        return _output_node(result, (root, frozenset(item for item in protected if item)), root, "", 0)
    except (PlanError, RecursionError, TypeError, ValueError) as exc:
        raise OutputError("routine-output-unavailable") from exc


def output_compared(node: dict[str, object]) -> bytes | None:
    """The exact canonical bytes a change is compared on, or None when they are too large to compare."""
    try:
        encoded = json.dumps(node, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    except ValueError:
        return None
    return encoded if len(encoded) <= MAX_COMPARED_OUTPUT_BYTES else None


def output_shown(step: str, node: dict[str, object]) -> dict[str, object]:
    """The safe form as shown, cut to the first bounds whose whole output fits; the last step elides it entirely.

    Every key and text is escaped, a long key shortened, a number written as its exact JSON text, and every cut,
    shortening, omission, and elision marks the output truncated.
    """
    for limits in OUTPUT_LEVELS:
        value, truncated = _output_display(node, limits, 0)
        candidate = {"step": step, "state": "shown", "value": value, "truncated": truncated}
        if http_routine.encoded_bytes(candidate) <= MAX_OUTPUT_BYTES:
            return candidate
    return {"step": step, "state": "shown", "value": dict(OUTPUT_ELIDED), "truncated": True}


def output_state(step: str, state: str) -> dict[str, object]:
    """A completed run's output that shows no value: unchanged since the last shown one, or unavailable."""
    return {"step": step, "state": state, "value": None, "truncated": False}


def _output_node(value: object, context: tuple, subschema: object, name: str, depth: int) -> dict[str, object]:
    root, protected = context
    if depth > MAX_SAFE_OUTPUT_DEPTH:
        return dict(OUTPUT_ELIDED)
    candidates = applicable(root, subschema, 0, value)
    if secret_position(root, name, candidates):
        return dict(OUTPUT_REDACTED)
    if isinstance(value, dict):
        return _output_fields(value, context, candidates, depth)
    if isinstance(value, list):
        items = [
            _output_node(item, context, item_schemas(candidates, index), name, depth + 1)
            for index, item in enumerate(value)
        ]
        return {"kind": "list", "items": items, "omitted": 0}
    if isinstance(value, str) and _protects(value, protected):
        return dict(OUTPUT_REDACTED)
    return _output_scalar(value)


def _protects(text: str, protected: frozenset[str]) -> bool:
    return any(secret in text for secret in protected)


def _output_scalar(value: object) -> dict[str, object]:
    if value is None:
        return {"kind": "null"}
    if isinstance(value, bool):
        return {"kind": "bool", "value": value}
    if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return {"kind": "number", "value": value}
    if isinstance(value, str):
        if assistant_manifest.resembles_credential(value):
            return dict(OUTPUT_REDACTED)
        return {"kind": "text", "value": value, "cut": False}
    raise OutputError("routine-output-unavailable")


def _output_fields(value: dict, context: tuple, candidates: list, depth: int) -> dict[str, object]:
    """An object's members in sorted key order, each under its member schema; a credential-shaped key is redacted.

    So is a key that holds a value the run protects.
    """
    fields: list[list[object]] = []
    redacted = 0
    for key in sorted(value):
        if assistant_manifest.resembles_credential(key) or _protects(key, context[1]):
            redacted += 1
            label = OUTPUT_REDACTED_KEY if redacted == 1 else f"{OUTPUT_REDACTED_KEY} {redacted}"
            fields.append([label, dict(OUTPUT_REDACTED)])
            continue
        try:
            member = member_schemas(candidates, key)
        except PlanError:
            fields.append([key, dict(OUTPUT_REDACTED)])
            continue
        fields.append([key, _output_node(value[key], context, member, key, depth + 1)])
    return {"kind": "fields", "fields": fields, "omitted": 0}


def output_label(key: str) -> tuple[str, bool]:
    """A key as shown, and whether it was shortened: escaped, quoted when empty, a long one cut with a digest."""
    label = http_routine.escaped(key) or '""'
    if len(label) <= MAX_OUTPUT_KEY_CHARS:
        return label, False
    digest = hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:7]
    return label[: MAX_OUTPUT_KEY_CHARS - 8] + "…" + digest, True


def _output_labels(keys: list[str]) -> tuple[list[str], bool]:
    """Each key's label, numbering any that read alike after escaping or shortening, and whether any was shortened."""
    labels: list[str] = []
    shortened = False
    for key in keys:
        label, cut = output_label(key)
        shortened = shortened or cut
        shown, number = label, 1
        while shown in labels:
            number += 1
            shown = f"{label[: MAX_OUTPUT_KEY_CHARS - 6]} ({number})"
            # Numbering a label that reads alike may cut it further, which is a cut like any other.
            shortened = shortened or len(label) > MAX_OUTPUT_KEY_CHARS - 6
        labels.append(shown)
    return labels, shortened


def _output_display(
    node: dict[str, object], limits: tuple[int, int, int], depth: int
) -> tuple[dict[str, object], bool]:
    """One safe node as shown within ``limits``, and whether anything was cut, shortened, omitted, or elided."""
    items, fields, chars = limits
    kind = node["kind"]
    if kind in ("text", "number"):
        return _output_text(node, chars)
    if kind not in ("list", "fields"):
        return dict(node), False
    if depth >= MAX_OUTPUT_DEPTH:
        return dict(OUTPUT_ELIDED), True
    entries = node["items"] if kind == "list" else node["fields"]
    kept = entries[: items if kind == "list" else fields]
    truncated = len(kept) < len(entries)
    labels: list[str] = []
    if kind == "fields":
        labels, shortened = _output_labels([key for key, _value in kept])
        truncated = truncated or shortened
    shown = []
    for index, entry in enumerate(kept):
        child, cut = _output_display(entry if kind == "list" else entry[1], limits, depth + 1)
        truncated = truncated or cut
        shown.append(child if kind == "list" else [labels[index], child])
    omitted = node["omitted"] + len(entries) - len(kept)
    key = "items" if kind == "list" else "fields"
    return {"kind": kind, key: shown, "omitted": omitted}, truncated


def _output_text(node: dict[str, object], chars: int) -> tuple[dict[str, object], bool]:
    """Escaped text or a number's exact JSON text, cut to ``chars``; a number too long for its node is shown as text."""
    if node["kind"] == "number":
        text = json.dumps(node["value"])
        if len(text) <= http_routine.MAX_OUTPUT_NUMBER_CHARS:
            return {"kind": "number", "value": text}, False
    else:
        text = http_routine.escaped(node["value"])
    if len(text) <= chars:
        return {"kind": "text", "value": text, "cut": False}, False
    return {"kind": "text", "value": text[: max(chars - 1, 0)] + "…", "cut": True}, True


# What a run's step record shows of the inputs one attempt was given (ADR-0092 amendment, 2026-10-05, scale): each
# member as the escaped preview of a redacted copy of its value. A value copied from an earlier step is first walked
# under that step's reviewed output schema along its pointer, so a secret position on the way withholds the whole
# preview and one inside it is redacted; every member is then redacted under the Action's own input schema, every
# credential-shaped string or key is redacted, and every string holding a value Team injected into the attempt is
# redacted, all before any cut. A value selected through an array item is walked along the array's pointer, then under
# every schema any of its items may have, then along the item's own pointer.
INPUT_REDACTED = "[redacted]"


def input_preview(
    plan: Plan, step: Step, resolved: Mapping[str, object], selected: Mapping[Key, object], protected
) -> list[dict[str, object]]:
    """Each input member of one attempt, sorted, as ``{member, source, value}`` with a redacted preview or None."""
    previews = []
    for member in sorted(step.inputs):
        source = step.inputs[member]
        try:
            value = _shown_input(plan, step, member, resolved, selected, tuple(protected))
        except PlanError, RecursionError, KeyError, TypeError, ValueError:
            value = _WITHHELD
        preview = None if value is _WITHHELD else http_routine.literal_preview(value)
        previews.append({"member": member, "source": source["kind"], "value": preview})
    return previews


_WITHHELD = object()


def _shown_input(plan, step, member, resolved, selected, protected) -> object:
    source = step.inputs[member]
    value = resolved[member]
    if source["kind"] == "step_output":
        origin = next(item for item in plan.steps if item.step_id == source["step"])
        root = dict(origin.output_schema)
        path: list[str | None] = list(pointer_tokens(source["pointer"]))
        if "where" in source:
            path += [None, *pointer_tokens(source["item"])]
        subschema = _pointed(root, path)
        if subschema is None:
            return _WITHHELD
        names = [token for token in path if token is not None]
        value = _redacted(selected[selector(source)], root, subschema, names[-1] if names else "", 0)
    root = dict(step.input_schema)
    member_schema = member_schemas(applicable(root, root, 0, dict(resolved)), member)
    # Every schema is walked against the original keys first; injected values and keys are scrubbed only after.
    return _scrubbed(_redacted(value, root, member_schema, member, 0), protected)


def _pointed(root: dict, path: list[str | None]) -> dict | None:
    """The subschemas a path reaches in a source output schema, or None when a position on its way may be secret.

    The root and every position on the way count, each with every dependent schema it might apply. A ``None`` token is
    any one item of an array: every schema any item may have applies there.
    """
    subschema: object = root
    if secret_position(root, "", applicable(root, root, 0, UNKNOWN)):
        return None
    for token in path:
        candidates = applicable(root, subschema, 0, UNKNOWN)
        if token is None:
            reached = {"allOf": _any_item(candidates)}
            token = ""
        else:
            reached = member_schemas(candidates, token)
        if _INDEX_RE.fullmatch(token) is not None:
            reached["allOf"].extend(item_schemas(candidates, int(token))["allOf"])
        if secret_position(root, token, applicable(root, reached, 0, UNKNOWN)):
            return None
        subschema = reached
    return subschema if isinstance(subschema, dict) else {}


def _any_item(candidates: list[Mapping[str, Any]]) -> list[object]:
    """Every schema any item of an array may have: each of its prefix items and its items."""
    found: list[object] = []
    for item in candidates:
        if isinstance(item.get("prefixItems"), list):
            found.extend(item["prefixItems"])
        if "items" in item:
            found.append(item["items"])
    return found


def _scrubbed(value: object, protected: tuple[str, ...]) -> object:
    """The value with injected values and their keys, and credential-shaped keys, hidden; after every schema walk.

    It works structurally, before any encoding, so no escaping can carry an injected value past it.
    """
    if isinstance(value, str):
        return INPUT_REDACTED if any(secret and secret in value for secret in protected) else value
    if isinstance(value, list):
        return [_scrubbed(item, protected) for item in value]
    if isinstance(value, dict):

        def hidden(key: str) -> bool:
            return assistant_manifest.resembles_credential(key) or any(secret and secret in key for secret in protected)

        names = _hidden(value, hidden)
        return {names[key]: _scrubbed(item, protected) for key, item in value.items()}
    return value


def _hidden(value: dict, hide: Callable[[str], bool]) -> dict[str, str]:
    """Each key mapped to itself or, when hidden, to a redacted name no other key of the value holds."""
    taken, names, count = set(value), {}, 0
    for key in sorted(value):
        if not hide(key):
            names[key] = key
            continue
        while f"{INPUT_REDACTED} {count}" in taken:
            count += 1
        names[key] = f"{INPUT_REDACTED} {count}"
        taken.add(names[key])
    return names


def _redacted(value: object, root: dict, subschema: object, name: str, depth: int) -> object:
    """A copy with every secret position, credential-shaped string, and credential-shaped key redacted."""
    if depth > MAX_SAFE_OUTPUT_DEPTH:
        return INPUT_REDACTED
    candidates = applicable(root, subschema, 0, value)
    if secret_position(root, name, candidates):
        return INPUT_REDACTED
    if isinstance(value, dict):
        # Keys stay as they are, so a later schema walk still sees every member; only the final scrub renames them.
        return {
            key: INPUT_REDACTED
            if assistant_manifest.resembles_credential(key)
            else _redacted(item, root, member_schemas(candidates, key), key, depth + 1)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [
            _redacted(item, root, item_schemas(candidates, index), name, depth + 1) for index, item in enumerate(value)
        ]
    if isinstance(value, str) and assistant_manifest.resembles_credential(value):
        return INPUT_REDACTED
    return value
