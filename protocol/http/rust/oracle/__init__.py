"""The Python side of the differential registry: each entry's call and the seed values its cases start from.

Each module of this package mirrors one file of `tests/protocol/support/registry/` with the same names. Add a module
and its line below. A call takes the strictly decoded case and returns what the Python module returns (`None` for a
refusal); an exception a module raises by contract is an `error:` line, any other a `raise:` line, which never matches
the Rust side.
"""

from __future__ import annotations

import json
import struct

import strict_json

from . import (
    browser,
    mutate,
    payload,
    phrase,
    presentation,
    progress,
    routine,
    routine_context,
    routine_notice,
    routine_proposal,
    routine_run,
    sign_in,
    websocket,
)

ENTRIES = {
    **websocket.ENTRIES,
    **sign_in.ENTRIES,
    **browser.ENTRIES,
    **payload.ENTRIES,
    **phrase.ENTRIES,
    **presentation.ENTRIES,
    **progress.ENTRIES,
    **routine.ENTRIES,
    **routine_context.ENTRIES,
    **routine_notice.ENTRIES,
    **routine_proposal.ENTRIES,
    **routine_run.ENTRIES,
}
ERRORS = {**presentation.ERRORS, **progress.ERRORS}
UNEXPECTED = (TypeError, ValueError, KeyError, AttributeError, IndexError, LookupError, RecursionError)

__all__ = ["ENTRIES", "mutate", "outcome_line"]


def to_tree(value: object) -> object:
    """The canonical representation of tests/protocol/support/repr.rs."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        return {"int": str(value)}
    if isinstance(value, float):
        return {"float_bits": struct.pack(">d", value).hex()}
    if isinstance(value, list | tuple):
        return [to_tree(item) for item in value]
    return {"object": [[key, to_tree(item)] for key, item in value.items()]}


def outcome_line(call, data: bytes) -> str:
    """The line the Python side writes for one case."""
    value = strict_json.loads(data)
    try:
        result = call(value)
    except tuple(ERRORS) as exc:
        return f"error:{ERRORS[type(exc)]}"
    except UNEXPECTED as exc:
        return f"raise:{type(exc).__name__}"
    if result is None:
        return "refuse"
    return json.dumps(to_tree(result), ensure_ascii=False, separators=(",", ":"))
