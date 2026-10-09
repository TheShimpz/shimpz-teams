"""The memory-only occurrences of a recording span's Action calls, and the values it protects (ADR-0101).

A Routine is recorded from work the ordinary chat agent actually did. While a Local send may record one, Team keeps each
successful Action call in dispatch order: its journal operation id, Assistant Action, complete pin, whether the
reviewed effect proves it read-only, its dispatch instant, and its model-given input and validated result as kept
values. A kept value is the exact JSON with every position that may be secret withheld whole and recorded out of band
as an RFC 6901 pointer: a schema-secret position, a credential-shaped string, a string holding a value Team injected,
and an object holding a credential-shaped or injected key. Private envelopes and human answers are never kept.

The span also keeps a protection set: every value an invocation was given or returned in secret. It only grows, and a
span that would exceed its bound loses it, irreversibly, so nothing it wrote may then enter a Routine. Nothing here is
ever persisted: a Team restart drops every occurrence and set, and recording is then unavailable.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from assistant import manifest as assistant_manifest
from routine import plan as routine_plan

# The calls one recording span may keep: the plan bound.
MAX_OCCURRENCES = routine_plan.MAX_STEPS
# A kept value larger than this is withheld whole, so nothing can be copied from it.
MAX_KEPT_BYTES = 256 * 1024
# Every kept input and result of one recording span, together.
MAX_TRACE_BYTES = 1024 * 1024
# The protection set of one turn or run: past either bound it is lost, never evicted (ADR-0101 section 6.2).
MAX_PROTECTED_VALUES = 128
MAX_PROTECTED_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class Kept:
    """One kept value: exact JSON with every withheld subtree emptied to null, and the pointers withheld."""

    value: object
    withheld: frozenset[str] = frozenset()
    # The value was larger than its bound and is withheld whole.
    oversize: bool = False

    def available(self, pointer: str) -> bool:
        """Whether nothing on the way to the pointer, at it, or under it was withheld."""
        return not any(
            item in ("", pointer) or pointer.startswith(item + "/") or item.startswith(pointer + "/")
            for item in self.withheld
        )


def escape(token: str) -> str:
    """One RFC 6901 reference token."""
    return token.replace("~", "~0").replace("/", "~1")


def keep(value: object, schema: object, protected: Iterable[str]) -> Kept:
    """The kept form of one value under its reviewed schema: every position that may be secret withheld whole."""
    root = schema if isinstance(schema, dict) else {}
    withheld: set[str] = set()
    kept = _kept(root, tuple(item for item in protected if item), withheld, value, root, ("", ""), 0)
    if len(routine_plan.canonical(kept)) > MAX_KEPT_BYTES:
        return Kept(None, frozenset({""}), oversize=True)
    return Kept(kept, frozenset(withheld))


def _secret_at(root: dict, subschema: object, name: str, value: object, depth: int) -> tuple[list, bool]:
    """The subschemas that apply at one position, and whether it may be secret; an unwalkable schema may be."""
    try:
        candidates = routine_plan.applicable(root, subschema, 0, value)
        return candidates, depth > routine_plan.MAX_SAFE_OUTPUT_DEPTH or routine_plan.secret_position(
            root, name, candidates
        )
    except routine_plan.PlanError, RecursionError:
        return [], True


def _kept(root, protected, withheld, value, subschema, place: tuple[str, str], depth: int) -> object:
    name, at = place
    candidates, unsafe = _secret_at(root, subschema, name, value, depth)
    if unsafe or _unsafe(value, protected):
        withheld.add(at)
        return None
    if isinstance(value, dict):
        return {
            key: _kept(
                root,
                protected,
                withheld,
                item,
                routine_plan.member_schemas(candidates, key),
                (key, f"{at}/{escape(key)}"),
                depth + 1,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _kept(
                root,
                protected,
                withheld,
                item,
                routine_plan.item_schemas(candidates, index),
                (name, f"{at}/{index}"),
                depth + 1,
            )
            for index, item in enumerate(value)
        ]
    return value


def _unsafe(value: object, protected: tuple[str, ...]) -> bool:
    """A credential-shaped or injected string, or an object holding such a key: withheld whole."""
    texts = value if isinstance(value, dict) else [value] if isinstance(value, str) else []
    return any(
        assistant_manifest.resembles_credential(text) or any(secret in text for secret in protected) for text in texts
    )


def secret_values(value: object, schema: object) -> tuple[str, ...]:
    """Every string a result holds at a position its reviewed schema may make secret, which the turn then protects."""
    root = schema if isinstance(schema, dict) else {}
    found: list[str] = []
    _collect(root, value, root, "", 0, found)
    return tuple(found)


def _collect(root: dict, value: object, subschema: object, name: str, depth: int, found: list[str]) -> None:
    candidates, secret = _secret_at(root, subschema, name, value, depth)
    if secret:
        found.extend(_strings(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            _collect(root, item, routine_plan.member_schemas(candidates, key), key, depth + 1, found)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _collect(root, item, routine_plan.item_schemas(candidates, index), name, depth + 1, found)


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return [text for item in items for text in _strings(item)]


def exposes(value: object, protected: frozenset[str]) -> bool:
    """Whether any string or key inside a value holds a protected value."""
    if isinstance(value, str):
        return any(secret in value for secret in protected)
    if isinstance(value, list):
        return any(exposes(item, protected) for item in value)
    if isinstance(value, dict):
        return any(exposes(key, protected) or exposes(item, protected) for key, item in value.items())
    return False


@dataclass(frozen=True, slots=True)
class Protection:
    """Every value a turn or run was given or returned in secret; it only grows, and once lost it stays lost."""

    values: frozenset[str] = frozenset()
    lost: bool = False

    def grow(self, values: Iterable[str]) -> Protection:
        grown = self.values | {item for item in values if item}
        if (
            self.lost
            or len(grown) > MAX_PROTECTED_VALUES
            or sum(len(item.encode()) for item in grown) > (MAX_PROTECTED_BYTES)
        ):
            return Protection(self.values, lost=True)
        return Protection(grown)


def kept_bytes(kept: Kept) -> int:
    """The encoded size of a kept value with every pointer it withholds."""
    return len(routine_plan.canonical({"value": kept.value, "withheld": sorted(kept.withheld)}))


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One successful Action call of a recording turn, in the order Team dispatched it."""

    operation_id: str
    assistant: str
    action: str
    pin: str
    # Whether the reviewed contract proves the Action read-only; anything not proven is a change.
    read_only: bool
    dispatched_at: int
    input: Kept
    result: Kept
    # The encoded size of its kept input and result, derived when it is made.
    size: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "size", kept_bytes(self.input) + kept_bytes(self.result))
