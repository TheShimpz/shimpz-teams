"""The Brain's task-bound sentence for why an Action pauses for a person (ADR-0090)."""

from __future__ import annotations

import unicodedata

MAX_PURPOSE_CHARS = 280


def canonical_purpose(value: object) -> str | None:
    """Return one plain single-line purpose sentence, or None.

    It carries no control, format, or line-separator character, no dash punctuation other than a hyphen inside a word,
    and nothing that reads as a link, so it can only explain, never point somewhere.
    """
    if (
        not isinstance(value, str)
        or unicodedata.normalize("NFC", value) != value
        or value.strip() != value
        or not 1 <= len(value) <= MAX_PURPOSE_CHARS
        or any(
            unicodedata.category(character)[0] == "C"
            or unicodedata.category(character) in {"Zl", "Zp"}
            or (unicodedata.category(character) == "Pd" and character != "-")
            for character in value
        )
        or " -" in value
        or "- " in value
        or "://" in value
        or "www." in value.casefold()
    ):
        return None
    return value
