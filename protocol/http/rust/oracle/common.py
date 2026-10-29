"""Helpers the oracle modules share: argument objects, the vectors, and Python's own typed integer test."""

from __future__ import annotations

import json
from pathlib import Path

V1 = Path(__file__).resolve().parents[2] / "v1"
_VECTORS: dict[str, object] = {}


def vectors() -> dict[str, object]:
    if not _VECTORS:
        _VECTORS.update(json.loads((V1 / "vectors.json").read_bytes()))
    return _VECTORS


def group(path: str) -> object:
    value: object = vectors()
    for key in path.split("."):
        value = value[key]
    return value


def cases(*paths: str) -> list[object]:
    """The valid and invalid cases of each group."""
    found: list[object] = []
    for path in paths:
        value = group(path)
        found.extend([*value.get("valid", []), *value.get("invalid", [])] if isinstance(value, dict) else value)
    return found


def arguments(value: object, *names: str) -> tuple[object, ...] | None:
    """The members of an argument object with exactly these names, in this order."""
    if not isinstance(value, dict) or set(value) != set(names):
        return None
    return tuple(value[name] for name in names)


def small(value: object) -> int | None:
    """An integer argument within i64, never a boolean."""
    return value if type(value) is int and -(2**63) <= value < 2**63 else None


def text(value: object) -> str | None:
    return value if isinstance(value, str) else None
