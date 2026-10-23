"""A Browser task's thread: what each Brain call of the task carries (ADR-0117, 2026-10-22 amendment §2-3).

Team keeps it in memory for the task only: each unfolded step's observation, the downloads and screenshot it saw, the
admitted step, and its outcome; the folded steps' records; and the agent's own fold notes. Entries are appended as
outcomes arrive and never rewritten, so the conversation the Brain renders from consecutive requests only grows at its
end. When an entry would push the history past its bound, the oldest unfolded steps fold first (their pages, read
records, and screenshots discarded), then the oldest folded lines and notes go, each counted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from protocol.http.v1 import browser as browser_protocol

# A fold empties the history to at most this share of its bound when folding alone can, so it happens rarely.
FOLD_TARGET_BYTES = browser_protocol.MAX_HISTORY_BYTES // 2


@dataclass(slots=True)
class Thread:
    folded: list[dict[str, object]] = field(default_factory=list)
    folded_omitted: int = 0
    notes: list[str] = field(default_factory=list)
    notes_omitted: int = 0
    steps: list[dict[str, object]] = field(default_factory=list)

    def wire(self) -> dict[str, object]:
        """The thread as a request carries it."""
        return {
            "folded": list(self.folded),
            "folded_omitted": self.folded_omitted,
            "notes": list(self.notes),
            "notes_omitted": self.notes_omitted,
            "steps": list(self.steps),
        }

    def number(self, position: int) -> int:
        """The 1-based step number of the unfolded step at `position`."""
        return self.folded_omitted + len(self.folded) + position + 1

    def history_bytes(self) -> int:
        return browser_protocol.history_bytes(self.wire())

    def to_fold(self, incoming: int) -> int:
        """How many of the oldest unfolded steps fold before an entry of `incoming` bytes joins: none while it fits.

        The count leaves room for the fold's own note, at most MAX_FOLD_NOTE_BYTES.
        """
        if self.history_bytes() + incoming <= browser_protocol.MAX_HISTORY_BYTES:
            return 0
        size = self.history_bytes() + incoming + browser_protocol.MAX_FOLD_NOTE_BYTES + 2
        count = 0
        for entry in self.steps:
            if size <= FOLD_TARGET_BYTES:
                break
            size -= browser_protocol.entry_bytes(entry) - browser_protocol.record_bytes(_record(entry))
            count += 1
        return count

    def fold(self, count: int, note: str) -> None:
        """Move the oldest `count` unfolded steps into folded records, with the note written for them."""
        moved, self.steps = self.steps[:count], self.steps[count:]
        self.folded.extend(_record(entry) for entry in moved)
        self.notes.append(note)
        self._trim()

    def append(self, entry: dict[str, object]) -> None:
        """Add a step as its outcome arrives, dropping the oldest folded lines and then notes while it does not fit."""
        self.steps.append(entry)
        while self.history_bytes() > browser_protocol.MAX_HISTORY_BYTES and (self.folded or self.notes):
            if self.folded:
                self.folded.pop(0)
                self.folded_omitted += 1
            else:
                self.notes.pop(0)
                self.notes_omitted += 1

    def _trim(self) -> None:
        while sum(map(browser_protocol.record_bytes, self.folded)) > browser_protocol.MAX_FOLDED_BYTES:
            self.folded.pop(0)
            self.folded_omitted += 1
        while sum(len(note.encode()) for note in self.notes) > browser_protocol.MAX_NOTES_BYTES:
            self.notes.pop(0)
            self.notes_omitted += 1


def _record(entry: dict[str, object]) -> dict[str, object]:
    return {key: entry[key] for key in ("step", "outcome", "page")}


def omitted_note(first: int, last: int) -> str:
    """The fixed note a fold commits when the agent's own could not be written."""
    return f"(no notes kept for steps {first} to {last})"
