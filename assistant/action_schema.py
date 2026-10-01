"""Team admission of one SDK-generated Action input or output schema.

An admitted schema describes a closed object, is a valid Draft 2020-12 schema checked without retrieving anything, fits
its byte and JSON value bounds, references only its root or a named definition, and has no boolean subschema.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

# Bytes alone do not bound what a decoded document retains: dense `default`, `examples`, `const`, or `enum` data costs
# tens of bytes of memory per encoded byte. Every JSON value therefore counts against this bound, which holds real
# SDK-generated schemas (at most a few hundred values) with wide headroom.
MAX_NODES = 4096
MAX_BYTES = 128 * 1024


class ActionSchemaError(ValueError):
    """An Action schema was refused; the message completes "Assistant Action <kind> schema ..."."""


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
    return None


def _reject_unwalked_references(schema: Mapping[str, Any]) -> None:
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
    _reject_unwalked_references(value)
    _reject_open_or_boolean_subschema(value)
    return value
