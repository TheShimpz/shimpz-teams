"""Observed Brain model usage for one Team request, attached to that request's audit event (ADR-0082).

Brain reports what each operation's provider responses said; a failed Brain request reports nothing. The summary is
metadata only: provider, model, operation counts, and token counts, never a prompt, reply, or credential.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from protocol.http.v1 import payload as http_payload

FIELDS = (
    "model_calls",
    "failed_calls",
    "unreported_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
OPERATIONS = ("action-labels", "capability-plan", "intent-route", "purpose", "turn", "turn-resume")
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
    """Accumulate usage per operation, provider, and model for the Brain operations of one request.

    Beside the audit entries, which each audit event drains, the meter keeps what the request's model calls used per
    provider and model for the whole request: a chat request's Brain calls all belong to its turn.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str, str], dict[str, object]] = {}
        self._tokens: dict[tuple[str, str], tuple[int, int]] = {}

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
            if counts["model_calls"]:
                input_tokens, output_tokens = self._tokens.get((provider, model), (0, 0))
                self._tokens[provider, model] = (
                    input_tokens + counts["input_tokens"],
                    output_tokens + counts["output_tokens"],
                )

    def drain(self) -> list[dict[str, object]] | None:
        """Return and forget everything accumulated so far, or None when no Brain operation ran."""
        with self._lock:
            entries = [dict(entry) for _key, entry in sorted(self._entries.items())]
            self._entries.clear()
        return entries or None

    def tokens(self) -> dict[tuple[str, str], tuple[int, int]]:
        """Input and output tokens per provider and model that this request's model calls used; never drained."""
        with self._lock:
            return dict(self._tokens)


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


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """What one logical chat turn consumed so far, carried with its pending state across human and Integration resumes.

    `started_ms` is the wall-clock admission of the turn, so its duration survives a resume in a later request.
    `models` holds sorted `(provider, model, input_tokens, output_tokens)` entries of calls that reported usage.
    """

    started_ms: int
    models: tuple[tuple[str, str, int, int], ...] = ()

    @classmethod
    def start(cls) -> TurnUsage:
        return cls(_now_ms())

    def joined(self) -> TurnUsage:
        """This turn's usage plus what the current request's model calls used, within the wire bounds."""
        meter = _METER.get()
        totals = {(provider, model): (inputs, outputs) for provider, model, inputs, outputs in self.models}
        for key, (inputs, outputs) in ({} if meter is None else meter.tokens()).items():
            prior_inputs, prior_outputs = totals.get(key, (0, 0))
            totals[key] = (prior_inputs + inputs, prior_outputs + outputs)
        maximum = http_payload.MAX_TURN_USAGE_TOKENS
        models = tuple(
            (provider, model, min(inputs, maximum), min(outputs, maximum))
            for (provider, model), (inputs, outputs) in sorted(totals.items())
            if http_payload.TURN_USAGE_ID_RE.fullmatch(provider) and http_payload.TURN_USAGE_ID_RE.fullmatch(model)
        )
        return TurnUsage(self.started_ms, models[: http_payload.MAX_TURN_USAGE_MODELS])

    def wire(self) -> dict[str, object] | None:
        """The closed `usage` of a completed turn, or None when no model call of the turn reported usage."""
        if not self.models:
            return None
        return {
            "duration_ms": min(max(0, _now_ms() - self.started_ms), http_payload.MAX_TURN_DURATION_MS),
            "models": [
                {"provider": provider, "model": model, "input_tokens": inputs, "output_tokens": outputs}
                for provider, model, inputs, outputs in self.models
            ],
        }
