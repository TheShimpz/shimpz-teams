"""Reference validation for Action effect classes and verifier descriptors (Assistant Spec v1).

Every Action declares ``effect``. A ``mutating`` Action may name one ``read_only`` Action of the same contract as
its verifier, with fixed typed input bindings and the output positions of its outcome and recovered result.
"""

from __future__ import annotations

import json
import re

EFFECTS = ("read_only", "mutating")
OUTCOMES = frozenset({"occurred", "not_occurred", "inconclusive"})
OPERATION_ID_SCHEMA = {"type": "string"}
MAX_BINDINGS = 16
MAX_BINDING_NAME = 128
MAX_POINTER = 256
POINTER = re.compile(r"(?:/(?:[^/~]|~[01])+)+")
VERIFIER_KEYS = {"action", "input", "outcome", "result"}


def effect_error(actions: object) -> str | None:
    """Return a stable reason when the Actions' effect or verifier declarations are refused."""
    if not isinstance(actions, list) or not all(isinstance(action, dict) for action in actions):
        return "actions_invalid"
    by_id = {action.get("id"): action for action in actions}
    for action in actions:
        error = _action_error(action, by_id)
        if error is not None:
            return error
    return None


def pointer_tokens(pointer: object) -> tuple[str, ...] | None:
    """Decode one non-root RFC 6901 pointer whose reference tokens are all non-empty."""
    if not isinstance(pointer, str) or len(pointer) > MAX_POINTER or POINTER.fullmatch(pointer) is None:
        return None
    return tuple(token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/"))


def _action_error(action: dict[str, object], by_id: dict[object, dict[str, object]]) -> str | None:
    effect = action.get("effect")
    if effect not in EFFECTS:
        return "effect_invalid"
    if "verifier" not in action:
        return None
    if effect != "mutating":
        return "verifier_on_read_only"
    return _verifier_error(action["verifier"], action, by_id)


def _verifier_error(verifier: object, action: dict[str, object], by_id: dict[object, dict[str, object]]) -> str | None:
    if not isinstance(verifier, dict) or set(verifier) != VERIFIER_KEYS or not isinstance(verifier["action"], str):
        return "verifier_invalid"
    target = by_id.get(verifier["action"])
    return (
        _target_error(target, action)
        or _bindings_error(verifier["input"], action.get("input_schema"), target.get("input_schema"))
        or _outcome_error(verifier["outcome"], target.get("output_schema"))
        or _result_error(verifier, target.get("output_schema"), action.get("output_schema"))
    )


def _target_error(target: dict[str, object] | None, action: dict[str, object]) -> str | None:
    if target is None or target is action:
        return "verifier_unknown"
    if target.get("effect") != "read_only":
        return "verifier_not_read_only"
    return None if _non_interactive(target) else "verifier_interactive"


def _non_interactive(target: dict[str, object]) -> bool:
    """A verifier runs without a person: it may only satisfy its own declared Stored Input internally."""
    requests = target.get("human_requests")
    return requests == [] or (requests == ["input:password"] and bool(target.get("stored_inputs")))


def _bindings_error(bindings: object, source: object, destination: object) -> str | None:
    if (
        not isinstance(bindings, dict)
        or not 1 <= len(bindings) <= MAX_BINDINGS
        or any(not 1 <= len(name) <= MAX_BINDING_NAME for name in bindings)
    ):
        return "verifier_invalid"
    properties = destination.get("properties") if isinstance(destination, dict) else None
    required = destination.get("required", []) if isinstance(destination, dict) else None
    if not isinstance(properties, dict) or not isinstance(required, list):
        return "verifier_input_mismatch"
    if not set(bindings) <= set(properties) or not set(required) <= set(bindings):
        return "verifier_input_mismatch"
    for name, binding in bindings.items():
        error = _binding_error(binding, source, properties[name])
        if error is not None:
            return error
    return None


def _binding_error(binding: object, source: object, destination: object) -> str | None:
    if binding == {"from": "operation_id"}:
        return None if _same(destination, OPERATION_ID_SCHEMA) else "verifier_binding_type"
    if not isinstance(binding, dict) or set(binding) != {"from", "pointer"} or binding["from"] != "input":
        return "verifier_invalid"
    tokens = pointer_tokens(binding["pointer"])
    if tokens is None:
        return "verifier_invalid"
    resolved = _resolve(source, tokens, final_required=True)
    if resolved is None:
        return "verifier_binding_unresolved"
    return None if _same(resolved, destination) else "verifier_binding_type"


def _outcome_error(pointer: object, output: object) -> str | None:
    tokens = pointer_tokens(pointer)
    if tokens is None:
        return "verifier_invalid"
    schema = _resolve(output, tokens, final_required=True)
    if (
        not isinstance(schema, dict)
        or set(schema) != {"type", "enum"}
        or schema["type"] != "string"
        or not isinstance(schema["enum"], list)
        or not all(isinstance(item, str) for item in schema["enum"])
        or len(schema["enum"]) != len(OUTCOMES)
        or set(schema["enum"]) != OUTCOMES
    ):
        return "verifier_outcome_invalid"
    return None


def _result_error(verifier: dict[str, object], output: object, original: object) -> str | None:
    tokens = pointer_tokens(verifier["result"])
    outcome = pointer_tokens(verifier["outcome"])
    if tokens is None or outcome is None:
        return "verifier_invalid"
    shorter = min(len(tokens), len(outcome))
    if tokens[:shorter] == outcome[:shorter]:
        return "verifier_result_invalid"
    schema = _resolve(output, tokens, final_required=False)
    return None if schema is not None and _same(schema, original) else "verifier_result_invalid"


def _resolve(schema: object, tokens: tuple[str, ...], *, final_required: bool) -> object | None:
    """Follow literal ``properties`` only, through required members; the last may be optional when allowed."""
    current = schema
    for index, token in enumerate(tokens):
        properties = current.get("properties") if isinstance(current, dict) else None
        required = current.get("required", []) if isinstance(current, dict) else None
        if not isinstance(properties, dict) or not isinstance(required, list) or token not in properties:
            return None
        if token not in required and (final_required or index < len(tokens) - 1):
            return None
        current = properties[token]
    return current


def _same(left: object, right: object) -> bool:
    """Compare JSON values exactly: booleans, integers, and floats never compare equal to one another."""
    try:
        return _canonical(left) == _canonical(right)
    except TypeError, ValueError:
        return False


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
