"""Validation semantics of an admitted Action input or output schema, which Team and the Brain apply alike.

Team admits a schema only when it fits MAX_NODES JSON values and MAX_BYTES, references only its root or a named
definition, resolves every reference without a cycle within MAX_EXPANDED_SUBSCHEMAS once each is expanded, nests no
subschema more than MAX_SUBSCHEMA_DEPTH levels below the root (a reference's target one level below the subschema that
holds it), and uses only patterns in the dialect `pattern.py` admits; `schema_problem` names the first such problem.
These are the rules Developers publication applies (its `action-schema` crate), so every predicate that admits an
Action schema is one predicate. Validation evaluates every `pattern` and `patternProperties` with RE2, never Python
`re`, whose backtracking would hold the GIL for time exponential in the subject, and charges each search its subject's
UTF-8 length times the pattern's program bound against one MAX_PATTERN_WORK budget per payload. `admission_problem`
is the whole predicate, closed objects and the Draft 2020-12 metaschema included: Team admits an Assistant's schemas
with it, and the Brain re-checks every schema it receives with it before it offers or validates anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import re2
from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError

from . import pattern as dialect

# Bytes alone do not bound what a decoded document retains: dense `default`, `examples`, `const`, or `enum` data costs
# tens of bytes of memory per encoded byte. Every JSON value therefore counts against this bound, which holds real
# SDK-generated schemas (at most a few hundred values) with wide headroom.
MAX_NODES = 4096
MAX_BYTES = 128 * 1024
# Validation visits a subschema once for every path that reaches it, so references multiply work the literal bounds do
# not show. The Assistant protocol pins this bound and its counting rule, and Developers publication applies it alike.
MAX_EXPANDED_SUBSCHEMAS = 4096
# Validation recurses once per subschema level, a reference's target included, so this bound keeps every validation
# far inside Python's recursion limit: no admitted schema can fail validation for its depth alone.
MAX_SUBSCHEMA_DEPTH = 32
# RE2 matches in time linear in the subject and releases the GIL while it searches. Its semantics are pinned by the
# Assistant protocol's pattern vectors: `\d`, `\w`, `\s`, and `\b` are ASCII, `$` without `m` matches only at the end
# of the text, and `i` folds Unicode case. Every admitted pattern's RE2 program fits this bound by `pattern.py`'s upper
# bound of it.
MAX_PATTERN_PROGRAM = dialect.MAX_PATTERN_PROGRAM
# Preparing a pattern (reading its syntax and compiling its program) charges this many times its program bound, once
# per distinct pattern of one payload validation, which then reuses it: the compilation costs far more per program
# instruction than a search does per subject byte, and the charge does not depend on any subject.
PREPARATION_WEIGHT = 256
# Each search charges its subject's UTF-8 length, at least 1, times its pattern's program bound. One payload
# validation may charge at most this much, preparations included, which the budget benchmark
# (.tests/action-schema/benchmark.py) measures at under a quarter second of RE2's slowest work on one CPU, while
# SDK-generated patterns on bounded strings charge a small fraction of it.
MAX_PATTERN_WORK = 1 << 27
# Checking a schema against the Draft 2020-12 metaschema takes milliseconds, and Team re-reads every Assistant's
# schemas while the Brain re-checks each schema of every turn it starts or resumes: the verdict on plain JSON is kept
# per SHA-256 of its exact encoding, never the encoding itself, for at most this many schemas, the oldest used dropped
# first. It holds every schema one chat turn can carry: 16 Assistants of at most 128 Actions, two schemas each.
MAX_CHECKED_SCHEMAS = 4096
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

_PATTERN_OPTIONS = re2.Options()
_PATTERN_OPTIONS.max_mem = 1 << 20
_PATTERN_OPTIONS.never_capture = True
_PATTERN_OPTIONS.log_errors = False


class _Work:
    """One payload validation's matching work: what remains of the budget, and each pattern prepared so far."""

    def __init__(self) -> None:
        self.remaining = MAX_PATTERN_WORK
        self.prepared: dict[str, tuple[object, int]] = {}

    def charge(self, units: int) -> None:
        if units > self.remaining:
            raise PatternError("pattern matching exceeds its work budget")
        self.remaining -= units


_pattern_work: ContextVar[_Work | None] = ContextVar("pattern_work", default=None)


class PatternError(ValueError):
    """Validation cannot complete, so it must fail closed.

    The matcher refused a pattern, a subject is not valid Unicode, the matching work is spent, or a number is beyond
    the arithmetic its keyword needs.
    """


def _prepared(pattern: str, work: _Work) -> tuple[object, int]:
    """The pattern's program and bound, prepared once per validation and charged before it compiles."""
    prepared = work.prepared.get(pattern)
    if prepared is None:
        bound = dialect.pattern_program(pattern)
        if bound is None:
            raise PatternError("pattern is outside the linear-time matcher")
        work.charge(PREPARATION_WEIGHT * bound)
        try:
            prepared = work.prepared[pattern] = (re2.compile(pattern, _PATTERN_OPTIONS), bound)
        except re2.error as exc:
            raise PatternError("pattern is outside the linear-time matcher") from exc
    return prepared


@contextmanager
def pattern_work_budget() -> Iterator[None]:
    """Charge every preparation and search inside the block against one MAX_PATTERN_WORK budget."""
    token = _pattern_work.set(_Work())
    try:
        yield
    finally:
        _pattern_work.reset(token)


def pattern_matches(pattern: str, subject: str) -> bool:
    """Whether pattern matches anywhere in subject, as JSON Schema `pattern` requires; raises PatternError.

    Outside pattern_work_budget, the search alone is charged against a fresh budget.
    """
    work = _pattern_work.get() or _Work()
    compiled, bound = _prepared(pattern, work)
    try:
        size = len(subject.encode())
    except UnicodeEncodeError as exc:
        raise PatternError("subject is not valid Unicode") from exc
    work.charge(max(size, 1) * bound)
    return compiled.search(subject) is not None


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


_STOCK_MULTIPLE_OF = Draft202012Validator.VALIDATORS["multipleOf"]


def _multiple_of(validator, divisor, instance, schema):
    # An integer beyond the float range divided by a float divisor raises OverflowError; the payload is refused.
    try:
        yield from _STOCK_MULTIPLE_OF(validator, divisor, instance, schema)
    except OverflowError as exc:
        raise PatternError("number is outside the arithmetic of multipleOf") from exc


def _equality_key(value: object) -> object:
    # Two values have equal keys exactly when JSON Schema equality holds: numbers by value, so 1 and 1.0 hash and
    # compare alike, a boolean only as a boolean, arrays in order, and objects whatever their member order.
    if value is True or value is False:
        return ("boolean", value)
    if isinstance(value, list):
        return ("array", tuple(_equality_key(item) for item in value))
    if isinstance(value, dict):
        return ("object", frozenset((name, _equality_key(item)) for name, item in value.items()))
    return ("value", value)


def _unique_items(validator, unique, instance, schema):
    # Exact uniqueness under JSON Schema equality in linear time; jsonschema's sort shortcut misses a duplicate that
    # sorts apart, and its fallback compares every pair.
    if unique and validator.is_type(instance, "array"):
        keys = {_equality_key(item) for item in instance}
        if len(keys) != len(instance):
            yield ValidationError("array items are not unique")


# Every keyword that reads a pattern goes through pattern_matches; `format` is never asserted.
_ActionValidator = validators.extend(
    Draft202012Validator,
    {
        "additionalProperties": _additional_properties,
        "multipleOf": _multiple_of,
        "pattern": _pattern,
        "patternProperties": _pattern_properties,
        "unevaluatedProperties": _unevaluated_properties,
        "uniqueItems": _unique_items,
    },
)


def json_nodes(value: object, limit: int) -> int:
    """Count JSON values, stopping once the count exceeds limit.

    The value itself, every array element, and every object member value count at any depth, whether a subschema, an
    annotation such as `default`, or a literal such as `enum`; member names do not count separately.
    """
    pending = [value]
    count = 0
    while pending and count <= limit:
        node = pending.pop()
        count += 1
        if isinstance(node, Mapping):
            pending.extend(node.values())
        elif isinstance(node, list | tuple):
            pending.extend(node)
    return count


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


def applied_subschemas(node: Mapping[str, Any]) -> Iterator[object]:
    """Every subschema a metaschema-valid node applies directly, through each Draft 2020-12 applicator except `$ref`."""
    for keyword in _APPLICATOR_KEYWORDS & node.keys():
        yield node[keyword]
    for keyword in _APPLICATOR_LIST_KEYWORDS & node.keys():
        yield from node[keyword]
    for keyword in _APPLICATOR_MAP_KEYWORDS & node.keys():
        yield from node[keyword].values()


def _node_problem(node: Mapping[str, Any], *, nested: bool) -> str | None:
    reference = node.get("$ref", "#")
    if "$dynamicRef" in node or not (isinstance(reference, str) and _LOCAL_REFERENCE.fullmatch(reference)):
        return "must reference only its root or a named definition"
    # Another dialect would apply keywords this walk never reads, and a nested base URI could rebind a reference.
    if node.get("$schema", DRAFT_2020_12) != DRAFT_2020_12:
        return "must use only the Draft 2020-12 dialect"
    if nested and "$id" in node:
        return "must not declare a nested identifier"
    if "unevaluatedProperties" in node:
        return "must not use unevaluatedProperties"
    return None


def _patterns_admitted(node: Mapping[str, Any]) -> bool:
    patterns = [*node.get("patternProperties", ())]
    if isinstance(node.get("pattern"), str):
        patterns.append(node["pattern"])
    return all(dialect.pattern_admitted(pattern) for pattern in patterns)


def reference_target(schema: Mapping[str, Any], reference: str) -> object:
    """The subschema an admitted schema's `$ref` names: its root or one direct definition, whose name may escape."""
    if reference == "#":
        return schema
    container, _, name = reference[2:].partition("/")
    return schema.get(container, {}).get(name.replace("~1", "/").replace("~0", "~"))


def _expansion_edges(schema: Mapping[str, Any], node: Mapping[str, Any]) -> list[object]:
    edges = [*applied_subschemas(node)]
    if "$ref" in node:
        edges.append(reference_target(schema, node["$ref"]))
    return edges


def expanded_subschemas(schema: Mapping[str, Any]) -> int | None:
    """Count the subschemas validation can visit, or None when a reference is missing or leads back into itself.

    Every value at a subschema position counts once per path from the root that reaches it, and a `$ref` target counts
    again at every subschema holding that reference, so a `#` reference is always a cycle. Each subschema's count is
    computed once, so the walk is linear in the document however large the expansion.
    """
    expansion = _expansion(schema)
    return None if expansion is None else expansion[0]


def _expansion(schema: Mapping[str, Any]) -> tuple[int, int] | None:
    """The expanded subschema count and the deepest object subschema's level, or None for a missing or cyclic reference.

    A reference's target lies one level below the subschema holding the reference.
    """
    known: dict[int, tuple[int, int]] = {}
    open_nodes = {id(schema)}
    frames: list[tuple[Mapping[str, Any], list[object], list[int]]] = [
        (schema, _expansion_edges(schema, schema), [1, 0])
    ]
    while frames:
        node, edges, total = frames[-1]
        if edges:
            edge = edges.pop()
            if edge is None or id(edge) in open_nodes:
                return None
            if not isinstance(edge, Mapping):
                total[0] += 1
            elif id(edge) in known:
                count, depth = known[id(edge)]
                total[0] += count
                total[1] = max(total[1], depth + 1)
            else:
                open_nodes.add(id(edge))
                frames.append((edge, _expansion_edges(schema, edge), [1, 0]))
            continue
        frames.pop()
        open_nodes.discard(id(node))
        known[id(node)] = (total[0], total[1])
        if frames:
            parent = frames[-1][2]
            parent[0] += total[0]
            parent[1] = max(parent[1], total[1] + 1)
    return known[id(schema)]


def schema_problem(schema: Mapping[str, Any]) -> str | None:
    """The first reference, dialect, depth, or pattern problem of a metaschema-valid schema, or None when admissible.

    Every reference must land on a schema position this walk has checked.
    """
    problem = _walk_problem(schema)
    if problem is not None:
        return problem
    expansion = _expansion(schema)
    if expansion is None:
        return "must resolve every reference without a cycle"
    if expansion[0] > MAX_EXPANDED_SUBSCHEMAS:
        return "is too large once its references are expanded"
    if expansion[1] > MAX_SUBSCHEMA_DEPTH:
        return "nests subschemas too deeply"
    return None


def _walk_problem(schema: Mapping[str, Any]) -> str | None:
    """The first problem of a walked subschema.

    The legacy `dependencies` keyword applies nothing in Draft 2020-12: only its patterns and depth are checked.
    """
    pending: list[tuple[object, int, bool]] = [(schema, 0, True)]
    while pending:
        node, depth, applied = pending.pop()
        if not isinstance(node, Mapping):
            continue
        if depth > MAX_SUBSCHEMA_DEPTH:
            return "nests subschemas too deeply"
        problem = _node_problem(node, nested=node is not schema) if applied else None
        if problem is not None:
            return problem
        if not _patterns_admitted(node):
            return "must use only patterns the linear-time matcher admits"
        pending.extend((child, depth + 1, applied) for child in applied_subschemas(node))
        dependencies = node.get("dependencies")
        if isinstance(dependencies, Mapping):
            pending.extend((child, depth + 1, False) for child in dependencies.values())
    return None


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


def payload_validator(schema: Mapping[str, Any], registry) -> Draft202012Validator:
    """Validate against an admitted schema with the linear-time matcher, resolving references only through registry."""
    return _ActionValidator(_without_dialects(schema), registry=registry)


def admission_problem(value: object) -> str | None:
    """Why value is not an admissible Action input or output schema, or None when it is.

    The rules are checked in the order Developers publication applies them: the root describes an object, the JSON value
    bound (before any encoding or metaschema work spends time on the excess), the Draft 2020-12 metaschema, the byte
    bound, `schema_problem`, and closed objects with no boolean subschema.
    """
    if not isinstance(value, Mapping) or value.get("type") != "object":
        return "must describe an object"
    if json_nodes(value, MAX_NODES) > MAX_NODES:
        return "is too large"
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    except TypeError, ValueError, RecursionError:
        return "is invalid"
    if not _metaschema_valid(value, encoded):
        return "is invalid"
    if len(encoded) > MAX_BYTES:
        return "is too large"
    return schema_problem(value) or _open_problem(value)


_CHECKED: OrderedDict[bytes, bool] = OrderedDict()
_CHECKED_LOCK = threading.Lock()


def _metaschema_valid(value: Mapping[str, Any], encoded: bytes) -> bool:
    if not _plain_json(value):
        # A tuple or a non-string member name has a JSON encoding that differs from it, so its verdict is never kept.
        return _metaschema_admits(value)
    digest = hashlib.sha256(encoded).digest()
    with _CHECKED_LOCK:
        known = _CHECKED.get(digest)
        if known is not None:
            _CHECKED.move_to_end(digest)
            return known
    valid = _metaschema_admits(json.loads(encoded))
    with _CHECKED_LOCK:
        _CHECKED[digest] = valid
        while len(_CHECKED) > MAX_CHECKED_SCHEMAS:
            _CHECKED.popitem(last=False)
    return valid


def _metaschema_admits(schema: object) -> bool:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError, RecursionError, OverflowError:
        # The metaschema's `regex` format compiles each pattern with Python `re`, which raises OverflowError for a
        # repetition count beyond its range and RecursionError for deeply nested groups.
        return False
    return True


def _plain_json(value: object) -> bool:
    # Iterative, so an annotation nested as deep as strict JSON admits is walked like any other value.
    pending = [value]
    while pending:
        node = pending.pop()
        if type(node) is dict:
            if not all(type(key) is str for key in node):
                return False
            pending.extend(node.values())
        elif type(node) is list:
            pending.extend(node)
        elif not (node is None or type(node) in (str, int, float, bool)):
            return False
    return True


# The positions the closed-object rule descends into, besides the list applicators.
_CLOSED_APPLICATORS = frozenset(
    {"additionalProperties", "contains", "else", "if", "items", "not", "propertyNames", "then"}
)
_CLOSED_MAP_APPLICATORS = frozenset({"$defs", "definitions", "dependentSchemas", "patternProperties", "properties"})
# The keywords that make an untyped subschema describe an object.
_OBJECT_KEYWORDS = frozenset(
    {
        "additionalProperties",
        "dependentRequired",
        "dependentSchemas",
        "maxProperties",
        "minProperties",
        "patternProperties",
        "properties",
        "propertyNames",
        "required",
    }
)


def _permits_object(node: Mapping[str, Any]) -> bool:
    schema_type = node.get("type")
    return (
        schema_type == "object"
        or (isinstance(schema_type, list) and "object" in schema_type)
        or (schema_type is None and bool(node.keys() & _OBJECT_KEYWORDS))
    )


def _open_problem(schema: Mapping[str, Any]) -> str | None:
    """Whether a metaschema-valid schema leaves an object open or uses a boolean subschema.

    Every subschema that may describe an object declares `additionalProperties: false`, which is the closing marker and
    not a boolean subschema; the metaschema admits only an object or a boolean at every other position walked.
    """
    pending: list[object] = [schema]
    while pending:
        node = pending.pop()
        if not isinstance(node, Mapping):
            return "must not use a boolean subschema"
        if _permits_object(node) and node.get("additionalProperties") is not False:
            return "must close every object"
        pending.extend(
            node[keyword]
            for keyword in _CLOSED_APPLICATORS & node.keys()
            if not (keyword == "additionalProperties" and node[keyword] is False)
        )
        for keyword in _APPLICATOR_LIST_KEYWORDS & node.keys():
            pending.extend(node[keyword])
        for keyword in _CLOSED_MAP_APPLICATORS & node.keys():
            pending.extend(node[keyword].values())
    return None
