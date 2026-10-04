"""The bounded canonical JSON an Action result must have before Team keeps it durably or acts on it.

The Action journal stores exactly these bytes, and Team refuses any other result before a follow-up side effect, so a
result is never accepted that the journal could not persist.
"""

from __future__ import annotations

import json
import math

# Matches the Assistant RPC frame bound; the RPC boundary refuses any result this encoding could not admit.
MAX_RESULT_BYTES = 512 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 4096


class ActionResultError(ValueError):
    """An Action result is not bounded canonical JSON."""


def walk(value: object, *, depth: int = 0, budget: list[int] | None = None) -> None:
    """Admit only JSON values within the depth and node bounds."""
    if budget is None:
        budget = [MAX_JSON_NODES]
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_JSON_DEPTH:
        raise ActionResultError("Action result exceeds the JSON structure limit")
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ActionResultError("Action result contains a non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            walk(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ActionResultError("Action result object keys must be strings")
            walk(item, depth=depth + 1, budget=budget)
        return
    raise ActionResultError("Action result must contain only JSON values")


def canonical(value: object, max_bytes: int) -> bytes:
    """The result's canonical UTF-8 JSON bytes, refused beyond ``max_bytes``."""
    walk(value)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ActionResultError("Action result is not canonical JSON") from exc
    if len(encoded) > max_bytes:
        raise ActionResultError("Action result exceeds the durable size limit")
    return encoded


def require_durable(value: object) -> None:
    """Refuse, before any follow-up side effect, a result the journal could not persist."""
    canonical(value, MAX_RESULT_BYTES)
