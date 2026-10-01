"""Team admission and validation of one SDK-generated Action input or output schema.

An admitted schema describes a closed object, is a valid Draft 2020-12 schema checked without retrieving anything, fits
its byte and JSON value bounds, references only its root or a named definition, has no boolean subschema, and uses only
patterns the linear-time matcher admits. Validation evaluates every `pattern` and `patternProperties` with RE2, never
Python `re`, whose backtracking would hold the GIL for time exponential in the subject.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from functools import lru_cache
from typing import Any

import re2
from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError
from referencing import Registry

# Bytes alone do not bound what a decoded document retains: dense `default`, `examples`, `const`, or `enum` data costs
# tens of bytes of memory per encoded byte. Every JSON value therefore counts against this bound, which holds real
# SDK-generated schemas (at most a few hundred values) with wide headroom.
MAX_NODES = 4096
MAX_BYTES = 128 * 1024


class ActionSchemaError(ValueError):
    """An Action schema was refused; the message completes "Assistant Action <kind> schema ..."."""


class PatternError(ValueError):
    """The matcher refused a pattern, or a subject that is not valid Unicode; validation must fail closed."""


# RE2 matches in time linear in the subject and releases the GIL while it searches. Its semantics are pinned by the
# Assistant protocol's pattern vectors: `\d`, `\w`, `\s`, and `\b` are ASCII, `$` without `m` matches only at the end
# of the text, and `i` folds Unicode case. The memory and program bounds keep the constant of one search bounded;
# Developers publication admits only patterns whose program provably fits MAX_PATTERN_PROGRAM.
MAX_PATTERN_PROGRAM = 16_384
_PATTERN_OPTIONS = re2.Options()
_PATTERN_OPTIONS.max_mem = 1 << 20
_PATTERN_OPTIONS.never_capture = True
_PATTERN_OPTIONS.log_errors = False


def _compiled_pattern(pattern: str):
    try:
        compiled = re2.compile(pattern, _PATTERN_OPTIONS)
    except (re2.error, UnicodeEncodeError) as exc:
        raise PatternError("pattern is outside the linear-time matcher") from exc
    if compiled.programsize > MAX_PATTERN_PROGRAM:
        raise PatternError("pattern exceeds the linear-time matcher bound")
    return compiled


def pattern_matches(pattern: str, subject: str) -> bool:
    """Whether pattern matches anywhere in subject, as JSON Schema `pattern` requires; raises PatternError."""
    compiled = _compiled_pattern(pattern)
    try:
        return compiled.search(subject) is not None
    except UnicodeEncodeError as exc:
        raise PatternError("subject is not valid Unicode") from exc


def _pattern(validator, pattern, instance, schema):
    if validator.is_type(instance, "string") and not pattern_matches(pattern, instance):
        yield ValidationError("value does not match its pattern")


def _pattern_properties(validator, patterns, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    for pattern, subschema in patterns.items():
        for name, value in instance.items():
            if pattern_matches(pattern, name):
                yield from validator.descend(value, subschema, path=name, schema_path=pattern)


def _additional_properties(validator, additional, instance, schema):
    if not validator.is_type(instance, "object"):
        return
    declared = schema.get("properties", {})
    patterns = schema.get("patternProperties", {})
    extras = [
        name
        for name in instance
        if name not in declared and not any(pattern_matches(pattern, name) for pattern in patterns)
    ]
    if validator.is_type(additional, "object"):
        for extra in extras:
            yield from validator.descend(instance[extra], additional, path=extra)
    elif not additional and extras:
        yield ValidationError("additional properties are not allowed")


def _unevaluated_properties(validator, unevaluated, instance, schema):
    # jsonschema evaluates this keyword's pattern properties with Python `re`; admission refuses it.
    raise PatternError("unevaluatedProperties is outside the linear-time matcher")


# Every keyword that reads a pattern goes through pattern_matches; `format` is never asserted.
_ActionValidator = validators.extend(
    Draft202012Validator,
    {
        "additionalProperties": _additional_properties,
        "pattern": _pattern,
        "patternProperties": _pattern_properties,
        "unevaluatedProperties": _unevaluated_properties,
    },
)


def json_nodes_within(value: object, limit: int) -> bool:
    """Whether value holds at most limit JSON values, counting itself and every array element and member value."""
    # Member names do not count separately. The walk stops at the first excess.
    pending = [value]
    count = 0
    while pending:
        node = pending.pop()
        count += 1
        if count > limit:
            return False
        if isinstance(node, dict):
            pending.extend(node.values())
        elif isinstance(node, list | tuple):
            pending.extend(node)
    return True


_SCHEMA_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "propertyNames",
        "items",
        "contains",
        "not",
        "if",
        "then",
        "else",
    }
)
_SCHEMA_MAP_KEYWORDS = frozenset(
    {
        "properties",
        "patternProperties",
        "dependentSchemas",
        "$defs",
        "definitions",
    }
)
_SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_OBJECT_KEYWORDS = frozenset(
    {
        "properties",
        "patternProperties",
        "additionalProperties",
        "required",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "dependentSchemas",
        "propertyNames",
    }
)


def _subschema_permits_object(node: dict[str, Any]) -> bool:
    schema_type = node.get("type")
    return (
        schema_type == "object"
        or (isinstance(schema_type, list) and "object" in schema_type)
        or (schema_type is None and bool(node.keys() & _OBJECT_KEYWORDS))
    )


def _child_subschemas(node: dict[str, Any]) -> Iterator[object]:
    for keyword in _SCHEMA_KEYWORDS:
        child = node.get(keyword)
        if child is not None and not (keyword == "additionalProperties" and child is False):
            yield child
    for keyword in _SCHEMA_MAP_KEYWORDS:
        children = node.get(keyword)
        if isinstance(children, dict):
            yield from children.values()
    for keyword in _SCHEMA_LIST_KEYWORDS:
        children = node.get(keyword)
        if isinstance(children, list):
            yield from children


def _reject_open_or_boolean_subschema(node: object) -> None:
    if isinstance(node, bool):
        raise ActionSchemaError("must not use a boolean subschema")
    if not isinstance(node, dict):
        raise ActionSchemaError("subschema is invalid")
    if _subschema_permits_object(node) and node.get("additionalProperties") is not False:
        raise ActionSchemaError("must close every object")
    for child in _child_subschemas(node):
        _reject_open_or_boolean_subschema(child)


def _plain_json(value: object) -> bool:
    if type(value) is dict:
        return all(type(key) is str and _plain_json(child) for key, child in value.items())
    if type(value) is list:
        return all(_plain_json(child) for child in value)
    return value is None or type(value) in (str, int, float, bool)


_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
# A reference may name only the root or one direct definition. Both are walked schema positions, so a reference never
# executes a const, enum, default, or examples value as a schema. A percent escape is refused because the resolver
# decodes it before it splits the pointer.
_LOCAL_REFERENCE = re.compile(r"#(?:/(?:\$defs|definitions)/[^/%]+)?")
# The Draft 2020-12 positions that hold subschemas; every other value, such as a property name or a const, enum,
# default, or examples value, is data and never a reference.
_APPLICATOR_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_APPLICATOR_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_APPLICATOR_MAP_KEYWORDS = frozenset({"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"})


def _applied_subschemas(node: Mapping[str, Any]) -> Iterator[object]:
    # The metaschema check already proved each applicator value has its Draft 2020-12 shape.
    for keyword in _APPLICATOR_KEYWORDS & node.keys():
        yield node[keyword]
    for keyword in _APPLICATOR_LIST_KEYWORDS & node.keys():
        yield from node[keyword]
    for keyword in _APPLICATOR_MAP_KEYWORDS & node.keys():
        yield from node[keyword].values()


def _schema_node_problem(node: Mapping[str, Any], *, nested: bool) -> str | None:
    reference = node.get("$ref", "#")
    if "$dynamicRef" in node or not (isinstance(reference, str) and _LOCAL_REFERENCE.fullmatch(reference)):
        return "must reference only its root or a named definition"
    # Another dialect would apply keywords this walk never reads, and a nested base URI could rebind a reference.
    if node.get("$schema", _DRAFT_2020_12) != _DRAFT_2020_12:
        return "must use only the Draft 2020-12 dialect"
    if nested and "$id" in node:
        return "must not declare a nested identifier"
    if "unevaluatedProperties" in node:
        return "must not use unevaluatedProperties"
    patterns = [*node.get("patternProperties", ())]
    if isinstance(node.get("pattern"), str):
        patterns.append(node["pattern"])
    try:
        for pattern in patterns:
            _compiled_pattern(pattern)
    except PatternError:
        return "must use only patterns the linear-time matcher admits"
    return None


def _reject_node_problems(schema: Mapping[str, Any]) -> None:
    # A reviewed package is immutable: every reference must land on a schema position this walk has checked.
    pending: list[object] = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, Mapping):
            problem = _schema_node_problem(node, nested=node is not schema)
            if problem is not None:
                raise ActionSchemaError(problem)
            pending.extend(_applied_subschemas(node))


@lru_cache(maxsize=256)
def _check_schema_json(encoded: bytes) -> None:
    # The cached verdict applies to this exact JSON, never to a mutable caller object.
    Draft202012Validator.check_schema(json.loads(encoded))


def admitted(value: object) -> dict[str, Any]:
    """Return value when Team admits it as an Action input or output schema, else raise ActionSchemaError."""
    if not isinstance(value, dict) or value.get("type") != "object":
        raise ActionSchemaError("must describe an object")
    # Refused before any encoding or metaschema work spends time on the excess.
    if not json_nodes_within(value, MAX_NODES):
        raise ActionSchemaError("is too large")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise ActionSchemaError("is invalid") from exc
    try:
        if len(encoded) <= 4096 and _plain_json(value):
            _check_schema_json(encoded)
        else:
            Draft202012Validator.check_schema(value)
    except (SchemaError, RecursionError) as exc:
        raise ActionSchemaError("is invalid") from exc
    if len(encoded) > MAX_BYTES:
        raise ActionSchemaError("is too large")
    _reject_node_problems(value)
    _reject_open_or_boolean_subschema(value)
    return value


def _without_dialects(node: object) -> object:
    # jsonschema switches to the stock validator class at any subschema declaring `$schema`, which would bypass the
    # pattern keywords above. Admission proved every declaration names Draft 2020-12, so dropping it changes nothing
    # else; data positions such as `const` or `default` are copied untouched.
    if not isinstance(node, Mapping):
        return node
    copied = {key: value for key, value in node.items() if key != "$schema"}
    for keyword in _APPLICATOR_KEYWORDS & copied.keys():
        copied[keyword] = _without_dialects(copied[keyword])
    for keyword in _APPLICATOR_LIST_KEYWORDS & copied.keys():
        copied[keyword] = [_without_dialects(child) for child in copied[keyword]]
    for keyword in _APPLICATOR_MAP_KEYWORDS & copied.keys():
        copied[keyword] = {name: _without_dialects(child) for name, child in copied[keyword].items()}
    return copied


def payload_validator(schema: Mapping[str, Any]) -> Draft202012Validator:
    """Validate against an admitted schema with the linear-time matcher, resolving references only inside it."""
    return _ActionValidator(_without_dialects(schema), registry=Registry())
