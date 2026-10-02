"""The compiled plan of a Routine and the resolution of each step's inputs, without I/O (ADR-0092 section 3).

A plan is versioned declarative JSON: at most eight ordered steps, each naming one exact Assistant Action by its
complete pin and giving each top-level input member exactly one value: a ``literal``, a ``run_clock`` token rendered
from the run's one immutable start instant in the plan's timezone, or a ``step_output`` that copies one JSON value,
selected by an RFC 6901 pointer, from a completed earlier step of the same run. There is no coercion, interpolation,
expression, branch, loop, or cross-run lookup. Outputs can only fill inputs: they never choose an Assistant, an Action,
a schedule, or a step. A missing path, an invalid index or escape, and a value its destination schema refuses (null
included) fail closed. Only the selected values are retained, never a complete output, and a literal that a secret
belongs in is refused: such a value must be the Action's declared Stored Input.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from assistant import action_schema
from assistant import manifest as assistant_manifest
from routine import schedule

VERSION = 1
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


@dataclass(frozen=True, slots=True)
class Plan:
    timezone: str
    steps: tuple[Step, ...]
    digest: str

    def references(self, step_id: str) -> tuple[str, ...]:
        """The pointers later steps select from ``step_id``'s output, sorted, each once."""
        return tuple(
            sorted(
                {
                    source["pointer"]
                    for step in self.steps
                    for source in step.inputs.values()
                    if source["kind"] == "step_output" and source["step"] == step_id
                }
            )
        )


@dataclass(frozen=True, slots=True)
class ActionContract:
    """What a plan needs of one Action: its current complete pin and its reviewed input schema."""

    pin: str
    input_schema: Mapping[str, Any]


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def admit(document: object, contracts: Mapping[tuple[str, str], ActionContract]) -> Plan:
    """Admit one plan at creation against the exact current contracts of the Actions it names."""
    if not isinstance(document, dict) or set(document) != {"version", "timezone", "steps"}:
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
    steps: list[Step] = []
    for raw in raw_steps:
        steps.append(_step(raw, contracts, tuple(item.step_id for item in steps)))
    return Plan(timezone, tuple(steps), "sha256:" + hashlib.sha256(encoded).hexdigest())


def _step(raw: object, contracts: Mapping[tuple[str, str], ActionContract], earlier: tuple[str, ...]) -> Step:
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
    contract = contracts.get((assistant_id, action))
    if contract is None or contract.pin != pin:
        raise PlanError("plan-pin-drift")
    schema = contract.input_schema
    properties = schema.get("properties", {})
    if not set(inputs) <= set(properties) or not set(schema.get("required", ())) <= set(inputs):
        raise PlanError("plan-input-mismatch")
    for name, source in inputs.items():
        _source(name, source, schema, earlier)
    return Step(step_id, assistant_id, action, pin, copy.deepcopy(inputs))


def _matches(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _source(name: str, source: object, schema: Mapping[str, Any], earlier: tuple[str, ...]) -> None:
    kind = source.get("kind") if isinstance(source, dict) else None
    fields = {"literal": {"kind", "value"}, "run_clock": {"kind", "format"}, "step_output": {"kind", "step", "pointer"}}
    if kind not in fields or set(source) != fields[kind]:
        raise PlanError("plan-input-invalid")
    if kind == "literal":
        if _secret_literal(schema, name, source["value"], schema["properties"][name], 0) or _holds_credential(
            source["value"]
        ):
            raise PlanError("plan-secret-literal")
        _admit_member(schema, name, source["value"])
    elif kind == "run_clock":
        if source["format"] not in CLOCK_FORMATS:
            raise PlanError("plan-input-invalid")
        _admit_member(schema, name, clock_value(source["format"], _SAMPLE_INSTANT, "UTC"))
    elif source["step"] not in earlier or pointer_tokens(source["pointer"]) is None:
        raise PlanError("plan-reference-invalid")


def _secret_literal(root: Mapping[str, Any], name: str, value: object, subschema: object, depth: int) -> bool:
    """Whether a literal reaches a secret destination anywhere inside it.

    Every position of the value is checked against every subschema that can apply there, through local ``$ref``,
    ``allOf``, ``anyOf``, ``oneOf``, object ``properties``, and array ``items`` and ``prefixItems``: a member whose
    name marks a secret, or a destination annotated ``writeOnly`` or ``format: password``, refuses the whole literal.
    A value nested deeper than the bound is refused rather than left unchecked.
    """
    if depth > MAX_SECRET_DEPTH:
        return True
    candidates = _applicable(root, subschema, 0)
    lowered = name.lower().replace("-", "_")
    if any(marker in lowered for marker in _SECRET_MARKERS) or any(_marked(item) for item in candidates):
        return True
    if isinstance(value, dict):
        return any(
            _secret_literal(root, key, item, _children(candidates, "properties", key), depth + 1)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(
            _secret_literal(root, name, item, _items(candidates, index), depth + 1) for index, item in enumerate(value)
        )
    return False


def _marked(subschema: Mapping[str, Any]) -> bool:
    return subschema.get("writeOnly") is True or subschema.get("format") == "password"


def _applicable(root: Mapping[str, Any], subschema: object, depth: int) -> list[Mapping[str, Any]]:
    """The subschemas that apply at one position: the node, its local reference, and its combinator members."""
    if depth > MAX_SECRET_DEPTH:
        raise PlanError("plan-secret-literal")
    if not isinstance(subschema, dict):
        return []
    found = [subschema]
    if isinstance(subschema.get("$ref"), str):
        found.extend(_applicable(root, action_schema.reference_target(root, subschema["$ref"]), depth + 1))
    for combinator in ("allOf", "anyOf", "oneOf"):
        for member in subschema.get(combinator, ()) if isinstance(subschema.get(combinator), list) else ():
            found.extend(_applicable(root, member, depth + 1))
    return found


def _children(candidates: list[Mapping[str, Any]], keyword: str, key: str) -> dict[str, Any]:
    """One synthetic subschema whose combinator holds every candidate's member schema for ``key``."""
    members = [
        item[keyword][key] for item in candidates if isinstance(item.get(keyword), dict) and key in item[keyword]
    ]
    return {"allOf": members}


def _items(candidates: list[Mapping[str, Any]], index: int) -> dict[str, Any]:
    members = []
    for item in candidates:
        prefix = item.get("prefixItems")
        if isinstance(prefix, list) and index < len(prefix):
            members.append(prefix[index])
        elif "items" in item:
            members.append(item["items"])
    return {"allOf": members}


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
            resolved[name] = copy.deepcopy(selected[(source["step"], source["pointer"])])
        else:
            raise PlanError("plan-reference-missing")
    try:
        validate(resolved)
    except ValueError as exc:
        raise PlanError("plan-input-type") from exc
    return resolved


def commitment(resolved: Mapping[str, object]) -> str:
    """The digest binding one dispatch to its exact resolved input."""
    return hashlib.sha256(canonical(dict(resolved))).hexdigest()
