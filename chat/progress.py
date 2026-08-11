"""Closed, metadata-only progress for one Team-owned chat execution."""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

PHASES = frozenset(
    {
        "model",
        "action",
        "action-delivery",
        "action-preparation",
        "team-context",
    }
)
MAX_SEQUENCE = 2_048
MAX_ELAPSED_MS = 24 * 60 * 60 * 1_000
MAX_ASSISTANT_ID_CHARS = 40
MAX_ACTION_ID_CHARS = 80
_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")

EventSink = Callable[[dict[str, object]], None]
Clock = Callable[[], int]


def _ignore(_event: dict[str, object]) -> None:
    return


def _validate_identity(phase: str, assistant_id: str | None, action: str | None) -> None:
    if phase != "action":
        if assistant_id is not None or action is not None:
            raise ValueError("invalid chat progress identity")
        return
    if (
        assistant_id is None
        or action is None
        or len(assistant_id) > MAX_ASSISTANT_ID_CHARS
        or len(action) > MAX_ACTION_ID_CHARS
        or _IDENTIFIER_RE.fullmatch(assistant_id) is None
        or _IDENTIFIER_RE.fullmatch(action) is None
    ):
        raise ValueError("invalid chat progress identity")


@dataclass(slots=True)
class Reporter:
    """Measure real controller operations without carrying protected payloads."""

    sink: EventSink = _ignore
    clock_ns: Clock = time.monotonic_ns
    sequence: int = 0

    def _emit(
        self,
        phase: str,
        state: str,
        *,
        elapsed_ms: int | None = None,
        index: int | None = None,
        total: int | None = None,
        assistant_id: str | None = None,
        action: str | None = None,
    ) -> None:
        if self.sink is _ignore:
            return
        if phase not in PHASES or state not in {"started", "finished"}:
            raise ValueError("invalid chat progress event")
        if (index is None) != (total is None):
            raise ValueError("incomplete chat progress position")
        if index is not None and (phase != "action" or not 1 <= index <= total <= 512):
            raise ValueError("invalid chat progress position")
        _validate_identity(phase, assistant_id, action)
        if state == "started" and elapsed_ms is not None:
            raise ValueError("started progress cannot have elapsed time")
        if state == "finished" and (type(elapsed_ms) is not int or not 0 <= elapsed_ms <= MAX_ELAPSED_MS):
            raise ValueError("invalid chat progress duration")
        if self.sequence >= MAX_SEQUENCE:
            return
        self.sequence += 1
        event: dict[str, object] = {
            "seq": self.sequence,
            "phase": phase,
            "state": state,
        }
        if elapsed_ms is not None:
            event["elapsed_ms"] = elapsed_ms
        if index is not None:
            event["index"] = index
            event["total"] = total
            event["assistant_id"] = assistant_id
            event["action"] = action
        with contextlib.suppress(Exception):
            self.sink(event)

    @contextlib.contextmanager
    def span(
        self,
        phase: str,
        *,
        index: int | None = None,
        total: int | None = None,
        assistant_id: str | None = None,
        action: str | None = None,
    ) -> Iterator[None]:
        """Emit one measured operation pair without affecting its outcome."""
        if self.sink is _ignore:
            yield
            return
        if self.sequence > MAX_SEQUENCE - 2:
            yield
            return
        started_ns = self.clock_ns()
        self._emit(
            phase,
            "started",
            index=index,
            total=total,
            assistant_id=assistant_id,
            action=action,
        )
        try:
            yield
        finally:
            elapsed_ms = max(0, (self.clock_ns() - started_ns) // 1_000_000)
            self._emit(
                phase,
                "finished",
                elapsed_ms=elapsed_ms,
                index=index,
                total=total,
                assistant_id=assistant_id,
                action=action,
            )
