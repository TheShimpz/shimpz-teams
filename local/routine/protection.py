"""Run protection: every value a Routine run was given or returned in secret, in this Team process only (ADR-0101).

A run's protection is bound once, when its cursor is first sealed in this Team boot, and then only grows: every value
Team injects into an attempt (Stored Inputs, Integration tokens, secret human answers, and the workload's capabilities)
before its RPC, and every string at a secret position of every result before the result reaches anything outward.
Team never rebuilds it from current stores: a run resumed in another boot, or one whose protection is missing here, has
lost it, and so has a run whose protection would exceed its bound. Loss is never undone.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Iterable

from routine import trace


class RunProtections:
    """Every run's protection in this Team process, keyed by run id, with the boot it was bound in."""

    def __init__(self, boot: str | None = None) -> None:
        self.boot = boot or secrets.token_hex(16)
        self._guard = threading.Lock()
        self._runs: dict[str, trace.Protection] = {}

    def bind(self, run_id: str) -> trace.Protection:
        """A fresh run's protection, bound in this boot; binding again keeps what it holds."""
        with self._guard:
            return self._runs.setdefault(run_id, trace.Protection())

    def current(self, run_id: str, boot: str) -> trace.Protection:
        """The run's protection, lost when it was bound in another boot or is not held here."""
        with self._guard:
            found = self._runs.get(run_id)
            if boot != self.boot or found is None:
                found = trace.Protection(lost=True)
                self._runs[run_id] = found
            return found

    def grow(self, run_id: str, values: Iterable[str]) -> trace.Protection:
        """Protect more values of one run; a run with no protection here has lost it."""
        with self._guard:
            found = self._runs.get(run_id, trace.Protection(lost=True)).grow(values)
            self._runs[run_id] = found
            return found

    def drop(self, run_id: str) -> None:
        """Forget an ended run's protection."""
        with self._guard:
            self._runs.pop(run_id, None)

    def clear(self) -> None:
        """Forget every run's protection, as a Space reset does."""
        with self._guard:
            self._runs.clear()
