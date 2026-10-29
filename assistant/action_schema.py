"""Team admission and validation of one SDK-generated Action input or output schema.

An admitted schema meets the one admission predicate of the Team Action protocol (`protocol/action/v1/schema.py`): it
describes a closed object, is a valid Draft 2020-12 schema checked without retrieving anything, has no boolean
subschema, fits its JSON value and byte bounds, has local acyclic references within the expansion bound, one dialect,
and patterns the linear-time matcher admits. Validation uses that protocol's bounded RE2 matcher.
"""

from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry

from protocol.action.v1 import schema as action_protocol

MAX_NODES = action_protocol.MAX_NODES
MAX_BYTES = action_protocol.MAX_BYTES
# One Action payload nests at most this deep below its root object. Admission refuses anything deeper, so every Team
# store that keeps an admitted payload, such as a paused local chat continuation, can restore it under the same bound.
MAX_PAYLOAD_DEPTH = 32
# The protocol's matcher and reference walk, which Team's Routine plan admission applies to admitted schemas as well.
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


def admitted(value: object) -> dict[str, Any]:
    """Return value when Team admits it as an Action input or output schema, else raise ActionSchemaError."""
    problem = action_protocol.admission_problem(value)
    if problem is not None:
        raise ActionSchemaError(problem)
    return value


def payload_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Validate against an admitted schema with the protocol's bounded matcher, resolving references only inside it."""
    return action_protocol.payload_validator(schema, Registry())
