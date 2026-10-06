"""Team admission and validation of one SDK-generated Action input or output schema.

An admitted schema describes a closed object, is a valid Draft 2020-12 schema checked without retrieving anything, has
no boolean subschema, and meets every rule of the Team Action protocol (`protocol/action/v1/schema.py`): its JSON value
and byte bounds, local acyclic references within the expansion bound, one dialect, and patterns the linear-time matcher
admits. Validation uses that protocol's bounded RE2 matcher.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry

from protocol.action.v1 import schema as action_protocol

MAX_NODES = action_protocol.MAX_NODES
MAX_BYTES = action_protocol.MAX_BYTES
# One Action payload nests at most this deep below its root object. Admission refuses anything deeper, so every Team
# store that keeps an admitted payload, such as a paused local chat continuation, can restore it under the same bound.
MAX_PAYLOAD_DEPTH = 32
# The protocol's matcher and reference walk, which Team's Routine compiler applies to admitted schemas as well.
PatternError = action_protocol.PatternError
pattern_matches = action_protocol.pattern_matches
pattern_work_budget = action_protocol.pattern_work_budget
applied_subschemas = action_protocol.applied_subschemas
reference_target = action_protocol.reference_target


class ActionSchemaError(ValueError):
    """An Action schema was refused; the message completes "Assistant Action <kind> schema ..."."""


def json_nodes_within(value: object, limit: int) -> bool:
    """Whether value holds at most limit JSON values, counting itself and every array element and member value."""
    return action_protocol.json_nodes(value, limit) <= limit


def json_depth_within(value: object, limit: int) -> bool:
    """Whether no array element or member value of value nests more than limit levels below it."""
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > limit:
            return False
        if isinstance(node, dict):
            pending.extend((child, depth + 1) for child in node.values())
        elif isinstance(node, list | tuple):
            pending.extend((child, depth + 1) for child in node)
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
    problem = action_protocol.schema_problem(value)
    if problem is not None:
        raise ActionSchemaError(problem)
    _reject_open_or_boolean_subschema(value)
    return value


def payload_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Validate against an admitted schema with the protocol's bounded matcher, resolving references only inside it."""
    return action_protocol.payload_validator(schema, Registry())
