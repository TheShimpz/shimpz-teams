"""Observed Brain model usage for one Team request, attached to that request's audit event (ADR-0082).

Brain reports what each operation's provider responses said; a failed Brain request reports nothing. The summary is
metadata only: provider, model, operation counts, and token counts, never a prompt, reply, or credential.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

FIELDS = (
    "model_calls",
    "failed_calls",
    "unreported_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
OPERATIONS = ("action-labels", "capability-plan", "intent-route", "turn", "turn-resume")
MAX_COUNT = 10**12


class UsageError(ValueError):
    pass


def parse(value: object) -> dict[str, int]:
    """Admit only Brain's closed usage shape: exact non-negative integer counts with consistent call totals."""
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise UsageError("invalid Brain usage")
    counts = {name: value[name] for name in FIELDS}
    if any(type(count) is not int or not 0 <= count <= MAX_COUNT for count in counts.values()):
        raise UsageError("invalid Brain usage")
    if counts["failed_calls"] + counts["unreported_calls"] > counts["model_calls"]:
        raise UsageError("invalid Brain usage")
    return counts


class Meter:
    """Accumulate usage per operation, provider, and model for the Brain operations of one request."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str, str], dict[str, object]] = {}

    def add(self, operation: str, provider: str, model: str, counts: Mapping[str, int]) -> None:
        if operation not in OPERATIONS:
            raise UsageError("invalid Brain usage operation")
        with self._lock:
            entry = self._entries.setdefault(
                (operation, provider, model),
                {"operation": operation, "provider": provider, "model": model, "count": 0, **dict.fromkeys(FIELDS, 0)},
            )
            entry["count"] += 1
            for name in FIELDS:
                entry[name] += counts[name]

    def drain(self) -> list[dict[str, object]] | None:
        """Return and forget everything accumulated so far, or None when no Brain operation ran."""
        with self._lock:
            entries = [dict(entry) for _key, entry in sorted(self._entries.items())]
            self._entries.clear()
        return entries or None


_METER: ContextVar[Meter | None] = ContextVar("team_brain_usage", default=None)


@contextmanager
def metered() -> Iterator[Meter]:
    """Collect the Brain usage of every operation this request makes in its own thread."""
    meter = Meter()
    token = _METER.set(meter)
    try:
        yield meter
    finally:
        _METER.reset(token)


def record(operation: str, provider: str, model: str, counts: Mapping[str, int]) -> None:
    meter = _METER.get()
    if meter is not None:
        meter.add(operation, provider, model, counts)


def drain() -> list[dict[str, object]] | None:
    meter = _METER.get()
    return None if meter is None else meter.drain()
