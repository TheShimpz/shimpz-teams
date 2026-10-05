"""The compiled plan of a Routine and the resolution of each step's inputs, without I/O (ADR-0092 section 3).

A plan is versioned declarative JSON: at most eight ordered steps, each naming one exact Assistant Action by its
complete pin and giving each top-level input member exactly one value: a ``literal``, a ``run_clock`` token rendered
from the run's one immutable start instant in the plan's timezone, or a ``step_output`` that copies one JSON value,
selected by an RFC 6901 pointer, from a completed earlier step of the same run. There is no coercion, interpolation,
expression, branch, loop, or cross-run lookup. Outputs can only fill inputs: they never choose an Assistant, an Action,
a schedule, or a step. A missing path, an invalid index or escape, and a value its destination schema refuses (null
included) fail closed. Only the selected values are retained, never a complete output, and a literal that a secret
belongs in is refused: such a value must be the Action's declared Stored Input. An Action that declares a file input
is refused in v1: a Routine holds no file grant, so no literal id or copied output may stand for an attached file
(ADR-0093).

A plan also states what a completed run does with its result (ADR-0092 amendment, 2026-10-05, output): ``show`` one
step's result to the person after every run, show it only when it ``changes``, hand it to a later step (``chain``), or
show ``none`` of it. A ``step_text`` source copies an earlier step's selected value as deterministic plain text, the one
narrow conversion a plan has, for a destination that takes a string.
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
from protocol.http.v1 import routine as http_routine
from routine import schedule

VERSION = 2
MAX_STEPS = 8
MAX_PLAN_BYTES = 64 * 1024
MAX_RETAINED_BYTES = 128 * 1024
MAX_POINTER = 256
STEP_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
ASSISTANT_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
ACTION_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
PIN_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_POINTER_RE = re.compile(r"(?:/(?:[^/~]|~[01])*)*\Z")
_INDEX_RE = re.compile(r"(?:0|[1-9][0-9]{0,8})\Z")
CLOCK_FORMATS = ("date", "time", "datetime", "epoch_seconds")
# What a completed run does with its result; ``show`` and ``changes`` name the step whose result is shown.
OUTPUT_MODES = ("show", "changes", "chain", "none")
SHOWN_MODES = frozenset({"show", "changes"})
# Sources that copy a value an earlier step of the same run returned.
BINDINGS = frozenset({"step_output", "step_text"})
# The longest text a step_text source renders; a longer one is refused before dispatch, never cut.
MAX_TEXT_BYTES = 16 * 1024
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


@dataclass(frozen=True, slots=True)
class Plan:
    timezone: str
    steps: tuple[Step, ...]
    digest: str
    # What a completed run does with its result: {"mode": one of OUTPUT_MODES, "step": the shown step id or None}.
    output: Mapping[str, object] = dataclasses.field(default_factory=lambda: {"mode": "none", "step": None})

    def shown(self) -> Step | None:
        """The step whose result a completed run shows, or None when the plan shows none."""
        if self.output["mode"] not in SHOWN_MODES:
            return None
        return next(step for step in self.steps if step.step_id == self.output["step"])

    def references(self, step_id: str) -> tuple[str, ...]:
        """The pointers later steps select from ``step_id``'s output, sorted, each once."""
        return tuple(
            sorted(
                {
                    source["pointer"]
                    for step in self.steps
                    for source in step.inputs.values()
                    if source["kind"] in BINDINGS and source["step"] == step_id
                }
            )
        )


@dataclass(frozen=True, slots=True)
class ActionContract:
    """What a plan needs of one Action: its current complete pin, its reviewed input schema, and its file inputs."""

    pin: str
    input_schema: Mapping[str, Any]
    input_files: tuple[str, ...] = ()
    output_schema: Mapping[str, Any] = dataclasses.field(default_factory=dict)


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
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_STEPS:
        raise PlanError("plan-invalid")
    return timezone, raw_steps, document["output"], encoded


def _output(value: object, steps: list[Step]) -> None:
    """The run's output disposition: a shown step must be one of the plan's; a chain needs a binding to hand it on."""
    if not isinstance(value, dict) or set(value) != {"mode", "step"} or value["mode"] not in OUTPUT_MODES:
        raise PlanError("plan-output-invalid")
    shown = value["step"]
    if value["mode"] in SHOWN_MODES:
        valid = isinstance(shown, str) and any(step.step_id == shown for step in steps)
    else:
        valid = shown is None
    if value["mode"] == "chain":
        valid = valid and any(source["kind"] in BINDINGS for step in steps for source in step.inputs.values())
    if not valid:
        raise PlanError("plan-output-invalid")


def _step_shape(raw: object, earlier: tuple[str, ...]) -> tuple[str, str, str, str, dict[str, object]]:
    """One step's closed shape and value sources, before its Action contract is known."""
    if not isinstance(raw, dict) or set(raw) != {"id", "assistant", "action", "pin", "input"}:
        raise PlanError("plan-step-invalid")
    step_id, assistant_id, action, pin, inputs = (raw[key] for key in ("id", "assistant", "action", "pin", "input"))
    if (
        not _matches(step_id, STEP_ID_RE)
        or step_id in earlier
        or not _matches(assistant_id, ASSISTANT_ID_RE)
        or not _matches(action, ACTION_ID_RE)
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
    return Step(step_id, assistant_id, action, pin, copy.deepcopy(inputs), copy.deepcopy(contract.output_schema))


def _matches(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _source(source: object, earlier: tuple[str, ...]) -> None:
    """One value source's closed shape: a literal, a run-clock format, or a reference to an earlier step."""
    kind = source.get("kind") if isinstance(source, dict) else None
    fields = {
        "literal": {"kind", "value"},
        "run_clock": {"kind", "format"},
        "step_output": {"kind", "step", "pointer"},
        "step_text": {"kind", "step", "pointer"},
    }
    if kind not in fields or set(source) != fields[kind]:
        raise PlanError("plan-input-invalid")
    if kind == "literal" and _holds_credential(source["value"]):
        raise PlanError("plan-secret-literal")
    if kind == "run_clock" and source["format"] not in CLOCK_FORMATS:
        raise PlanError("plan-input-invalid")
    if kind in BINDINGS and (source["step"] not in earlier or pointer_tokens(source["pointer"]) is None):
        raise PlanError("plan-reference-invalid")


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
    if isinstance(dependent, dict) and isinstance(value, dict):
        members.extend(dependent[key] for key in value if key in dependent)
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
    """One run-clock token rendered from the run's start instant in the plan's timezone."""
    local = instant.astimezone(schedule.zone(timezone))
    rendered: dict[str, Callable[[], object]] = {
        "date": lambda: local.date().isoformat(),
        "time": lambda: local.strftime("%H:%M"),
        "datetime": lambda: local.isoformat(timespec="seconds"),
        "epoch_seconds": lambda: int(instant.timestamp()),
    }
    return rendered[form]()


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


def selections(plan: Plan, step_id: str, result: object) -> dict[str, object]:
    """Every value later steps select from one completed step's result, keyed by pointer."""
    return {pointer: select(result, pointer) for pointer in plan.references(step_id)}


def retained_within(selected: Mapping[tuple[str, str], object]) -> bool:
    """Whether a run's retained selections fit their byte bound."""
    return len(canonical([[step, pointer, value] for (step, pointer), value in sorted(selected.items())])) <= (
        MAX_RETAINED_BYTES
    )


def resolve(
    plan: Plan,
    step: Step,
    selected: Mapping[tuple[str, str], object],
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
        elif (source["step"], source["pointer"]) in selected:
            chosen = copy.deepcopy(selected[(source["step"], source["pointer"])])
            resolved[name] = chosen if source["kind"] == "step_output" else _text_within(chosen)
        else:
            raise PlanError("plan-reference-missing")
    try:
        validate(resolved)
    except ValueError as exc:
        raise PlanError("plan-input-type") from exc
    return resolved


def text(value: object) -> str:
    """One selected value as deterministic, locale-neutral plain text, complete and never cut.

    A string selected whole is itself, verbatim. Anything else is unambiguous: every key and scalar inside it is its
    JSON text, so a string is quoted and escaped and can never make another line, field, or item. A list is one ``-``
    line per item and an object one ``"key":`` line per member in sorted key order; a scalar follows on the same line
    after a space, a non-empty list or object on the next lines indented two spaces more. An empty list or object is
    its JSON text.
    """
    if isinstance(value, str):
        return value
    return "\n".join(_text_lines(value, 0)) if isinstance(value, dict | list) and value else _json_text(value)


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _text_lines(value: object, depth: int) -> list[str]:
    indent = "  " * depth
    if isinstance(value, list):
        entries = [("-", item) for item in value]
    else:
        entries = [(_json_text(key) + ":", value[key]) for key in sorted(value)]
    lines: list[str] = []
    for label, item in entries:
        if isinstance(item, dict | list) and item:
            lines.append(indent + label)
            lines.extend(_text_lines(item, depth + 1))
        else:
            lines.append(f"{indent}{label} {_json_text(item)}")
    return lines


def _text_within(value: object) -> str:
    """The value's text when it is encodable and within its bound; anything else is refused before dispatch."""
    rendered = text(value)
    try:
        size = len(rendered.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PlanError("plan-input-type") from exc
    if size > MAX_TEXT_BYTES:
        raise PlanError("plan-input-type")
    return rendered


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


def output_safe(result: object, schema: object) -> dict[str, object]:
    """The complete safe form of one validated result under its Action's reviewed output schema.

    It keeps every key, text, and number exactly as the result has them, apart from what it redacts, so a change is
    compared on the complete data; escaping, shortening, and every cut belong to the shown form alone.
    """
    root = schema if isinstance(schema, dict) else {}
    try:
        return _output_node(result, root, root, "", 0)
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


def _output_node(value: object, root: dict, subschema: object, name: str, depth: int) -> dict[str, object]:
    if depth > MAX_SAFE_OUTPUT_DEPTH:
        return dict(OUTPUT_ELIDED)
    candidates = applicable(root, subschema, 0, value)
    if secret_position(root, name, candidates):
        return dict(OUTPUT_REDACTED)
    if isinstance(value, dict):
        return _output_fields(value, root, candidates, depth)
    if isinstance(value, list):
        items = [
            _output_node(item, root, item_schemas(candidates, index), name, depth + 1)
            for index, item in enumerate(value)
        ]
        return {"kind": "list", "items": items, "omitted": 0}
    return _output_scalar(value)


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


def _output_fields(value: dict, root: dict, candidates: list, depth: int) -> dict[str, object]:
    """An object's members in sorted key order, each under its member schema; a credential-shaped key is redacted."""
    fields: list[list[object]] = []
    redacted = 0
    for key in sorted(value):
        if assistant_manifest.resembles_credential(key):
            redacted += 1
            label = OUTPUT_REDACTED_KEY if redacted == 1 else f"{OUTPUT_REDACTED_KEY} {redacted}"
            fields.append([label, dict(OUTPUT_REDACTED)])
            continue
        try:
            member = member_schemas(candidates, key)
        except PlanError:
            fields.append([key, dict(OUTPUT_REDACTED)])
            continue
        fields.append([key, _output_node(value[key], root, member, key, depth + 1)])
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
