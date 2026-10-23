"""A Browser task's thread: what each Brain call of the task carries (ADR-0117, 2026-10-22 and 2026-10-23 amendments).

Team keeps it in memory for the task only: each unfolded step's observation, the downloads and screenshot it saw, the
admitted step, and its outcome; the folded steps' records; and the agent's task memory. Entries are appended as
outcomes arrive and never rewritten, so the conversation the Brain renders from consecutive requests only grows at its
end. When an entry would push the history past its bound, the oldest unfolded steps fold (their pages, read records,
and screenshots discarded) and the agent rewrites its whole memory, which replaces the one before; the oldest folded
lines go first when even that does not fit, counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from protocol.http.v1 import browser as browser_protocol

# A fold leaves the history at most this large with the incoming entry, so the next one comes only after a few steps.
FOLD_TARGET_BYTES = browser_protocol.MAX_HISTORY_BYTES // 2


@dataclass(slots=True)
class Thread:
    folded: list[dict[str, object]] = field(default_factory=list)
    folded_omitted: int = 0
    memory: str = ""
    steps: list[dict[str, object]] = field(default_factory=list)

    def wire(self) -> dict[str, object]:
        """The thread as a request carries it."""
        return {
            "folded": list(self.folded),
            "folded_omitted": self.folded_omitted,
            "memory": self.memory,
            "steps": list(self.steps),
        }

    def number(self, position: int) -> int:
        """The 1-based step number of the unfolded step at `position`."""
        return self.folded_omitted + len(self.folded) + position + 1

    def history_bytes(self) -> int:
        return browser_protocol.history_bytes(self.wire())

    def to_fold(self, incoming: int) -> int:
        """How many of the oldest unfolded steps fold before an entry of `incoming` bytes joins: none while it fits.

        The count assumes the rewritten memory takes its whole bound. Folding aims at FOLD_TARGET_BYTES, and only steps
        fold, so a large incoming entry may fold them all; MAX_HISTORY_BYTES is the bound.
        """
        if self.history_bytes() + incoming <= browser_protocol.MAX_HISTORY_BYTES:
            return 0
        size = self.history_bytes() - len(self.memory.encode()) + browser_protocol.MAX_MEMORY_BYTES + incoming
        count = 0
        for entry in self.steps:
            if size <= FOLD_TARGET_BYTES:
                break
            size -= browser_protocol.entry_bytes(entry) - browser_protocol.record_bytes(_record(entry))
            count += 1
        return count

    def fold(self, count: int, memory: str | None) -> None:
        """Move the oldest `count` unfolded steps into folded records; `memory` replaces the agent's memory.

        Without a new memory, the old one stays and says which steps' pages left with nothing kept, while that fits.
        """
        first, last = self.number(0), self.number(count - 1)
        moved, self.steps = self.steps[:count], self.steps[count:]
        self.folded.extend(_record(entry) for entry in moved)
        if memory is not None:
            self.memory = memory
        else:
            gap = "\n".join(part for part in (self.memory, omitted_note(first, last)) if part)
            if len(gap.encode()) <= browser_protocol.MAX_MEMORY_BYTES:
                self.memory = gap
        while sum(map(browser_protocol.record_bytes, self.folded)) > browser_protocol.MAX_FOLDED_BYTES:
            self.folded.pop(0)
            self.folded_omitted += 1

    def append(self, entry: dict[str, object]) -> None:
        """Add a step as its outcome arrives, dropping the oldest folded lines while it does not fit."""
        self.steps.append(entry)
        while self.history_bytes() > browser_protocol.MAX_HISTORY_BYTES and self.folded:
            self.folded.pop(0)
            self.folded_omitted += 1


def _record(entry: dict[str, object]) -> dict[str, object]:
    return {key: entry[key] for key in ("step", "outcome", "page")}


def omitted_note(first: int, last: int) -> str:
    """The fixed line a fold adds to the memory when the agent's own rewrite could not be written."""
    return f"(no notes kept for steps {first} to {last})"
