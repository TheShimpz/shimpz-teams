"""`routine_run`."""

from __future__ import annotations

import routine_run

from .common import arguments, cases, small


def _active_seconds(value):
    units = small(value)
    return routine_run.active_seconds(units) if units is not None and -(2**40) <= units <= 2**40 else None


def _run_step(value):
    found = arguments(value, "value", "position", "steps")
    if found is None or small(found[1]) is None:
        return None
    return routine_run.canonical_run_step(*found)


def _diagnostics() -> list[object]:
    found = []
    for case in [*cases("routine_diagnostics"), *cases("routine_views.card")]:
        if not isinstance(case, dict):
            continue
        if isinstance(case.get("diagnostics"), list):
            found.extend(case["diagnostics"])
        if isinstance(case.get("diagnostic"), dict):
            found.append(case["diagnostic"])
    return found


def _failures() -> list[object]:
    return [item["failure"] for item in _diagnostics() if isinstance(item, dict) and item.get("failure") is not None]


def _run_steps() -> list[object]:
    found = []
    for page in cases("routine_views.run_steps"):
        if not isinstance(page, dict) or not isinstance(page.get("steps"), list):
            continue
        offset = page.get("offset") if type(page.get("offset")) is int else 0
        for index, step in enumerate(page["steps"]):
            found.append({"value": step, "position": offset + index + 1, "steps": page.get("total")})
    return found


ENTRIES = {
    "routine_run.canonical_card": (routine_run.canonical_card, lambda: cases("routine_views.card")),
    "routine_run.canonical_card_answer_request": (
        routine_run.canonical_card_answer_request,
        lambda: cases("routine_views.card_answer_request"),
    ),
    "routine_run.canonical_card_answer": (
        routine_run.canonical_card_answer,
        lambda: cases("routine_views.card_answer"),
    ),
    "routine_run.canonical_challenge_open": (
        routine_run.canonical_challenge_open,
        lambda: cases("routine_challenge_open"),
    ),
    "routine_run.canonical_claim_request": (
        routine_run.canonical_claim_request,
        lambda: cases("routine_views.claim_request"),
    ),
    "routine_run.active_seconds": (_active_seconds, lambda: [0, 1, 8, 228, 229, -12, -(2**40), 2**40, 2**41]),
    "routine_run.canonical_claim": (routine_run.canonical_claim, lambda: cases("routine_views.claim")),
    "routine_run.canonical_segment_request": (
        routine_run.canonical_segment_request,
        lambda: cases("routine_views.segment_request"),
    ),
    "routine_run.canonical_segment_answer": (
        routine_run.canonical_segment_answer,
        lambda: cases("routine_views.segment_answer"),
    ),
    "routine_run.canonical_failure": (routine_run.canonical_failure, _failures),
    "routine_run.canonical_diagnostic": (routine_run.canonical_diagnostic, _diagnostics),
    "routine_run.canonical_diagnostics": (routine_run.canonical_diagnostics, lambda: cases("routine_diagnostics")),
    "routine_run.canonical_run_step": (_run_step, _run_steps),
    "routine_run.canonical_run_steps": (routine_run.canonical_run_steps, lambda: cases("routine_views.run_steps")),
}
