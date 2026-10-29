"""`routine_context`."""

from __future__ import annotations

import datetime
import re

import routine_context

from .common import arguments, cases, group

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _today(value: object) -> datetime.date | None:
    if not isinstance(value, str) or _DATE.fullmatch(value) is None:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _may_write_date(value):
    found = arguments(value, "text", "today")
    if found is None or not isinstance(found[0], str) or _today(found[1]) is None:
        return None
    return routine_context.may_write_date(found[0], _today(found[1]))


def _number_tokens(value):
    if not isinstance(value, str):
        return None
    return [token.group() for token in routine_context.NUMBER_TOKEN_RE.finditer(value)]


def _dates() -> list[object]:
    texts = [case["text"] for case in group("routine_date_words")]
    texts += ["2026-02-29", "29/02/2028", "31.12.2026", "1/1", "2026年2月30日", "0000-01-01", "12/13", "9999-12-31"]
    todays = ["2026-10-16", "2028-02-28", "0001-01-01", "9999-12-31", "2026-02-30", "20261016"]
    return [{"text": text, "today": today} for text in texts for today in todays]


def _literals() -> list[object]:
    literals = group("routine_rerun_literal")
    return [*literals["valid"], *literals["invalid"]]


def _tokens() -> list[object]:
    return [case["text"] for case in group("routine_number_tokens")] + [
        "1,234.5 +3 -4e+5 x1 1.2.3 ٣٤ ১২.৫ 7e 8,",
        "(42) [1.5] 1_000 -0 +.5",
    ]


ENTRIES = {
    "routine_context.canonical_routine_listing": (
        routine_context.canonical_routine_listing,
        lambda: (
            [item for listing in group("routine_listing.valid") for item in listing]
            + [item for listing in group("routine_listing.invalid") if isinstance(listing, list) for item in listing]
        ),
    ),
    "routine_context.canonical_routine_listings": (
        routine_context.canonical_routine_listings,
        lambda: cases("routine_listing"),
    ),
    "routine_context.canonical_rerun": (routine_context.canonical_rerun, lambda: cases("routine_rerun")),
    "routine_context.json_literal": (routine_context._json_literal, _literals),
    "routine_context.canonical_selected_routine": (
        routine_context.canonical_selected_routine,
        lambda: cases("routine_selected"),
    ),
    "routine_context.canonical_edits": (
        routine_context.canonical_edits,
        lambda: [case["edits"] for case in cases("routine_refine") if isinstance(case, dict) and "edits" in case],
    ),
    "routine_context.canonical_refine": (routine_context.canonical_refine, lambda: cases("routine_refine")),
    "routine_context.may_write_date": (_may_write_date, _dates),
    "routine_context.number_tokens": (_number_tokens, _tokens),
}
