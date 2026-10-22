"""Which one Routine a person's recording send refers to, read with no model (ADR-0122).

A send refers to a Routine when it comes from that Routine's Refine entry, which names it by id, or when its
person-authored text names it: the Routine's name, compared without case after Unicode composition, with no letter,
digit, or underscore on either side. A Refine entry for a Routine the Team does not list, or text naming several
Routines, refers to none Team can resolve; a send that names none refers to nothing at all, so the span keeps what an
earlier send of it selected.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

# What one send says: exactly one Routine by id, none Team can resolve, or nothing about any Routine.
SELECTED = "selected"
UNRESOLVED = "unresolved"
NONE = "none"


def _folded(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _named(name: str, texts: Iterable[str]) -> bool:
    pattern = re.compile(r"(?<!\w)" + re.escape(_folded(name)) + r"(?!\w)")
    return any(pattern.search(text) is not None for text in texts)


def referred(
    routines: Sequence[Mapping[str, object]], segments: Sequence[str], refine: str | None
) -> tuple[str, str | None]:
    """What a send says about the listed ``routines``: one selected by id, none Team can resolve, or nothing.

    ``segments`` are the person-authored segments of its message; ``refine`` the id its Refine entry sends, if any.
    """
    listed = [str(item["routine_id"]) for item in routines]
    if refine is not None:
        return (SELECTED, refine) if refine in listed else (UNRESOLVED, None)
    texts = [_folded(segment) for segment in segments]
    found = [str(item["routine_id"]) for item in routines if _named(str(item["name"]), texts)]
    if not found:
        return NONE, None
    return (SELECTED, found[0]) if len(found) == 1 else (UNRESOLVED, None)
