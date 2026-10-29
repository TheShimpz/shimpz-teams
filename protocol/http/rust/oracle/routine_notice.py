"""`routine_notice`."""

from __future__ import annotations

import routine_notice

from .common import arguments, cases, group


def _notices() -> list[object]:
    """Every notice of the batch vectors and of the rollup deliveries."""
    found = []
    for batch in cases("routine_views.notice_batch"):
        if isinstance(batch, dict) and isinstance(batch.get("notices"), list):
            found.extend(batch["notices"])
    for case in group("routine_rollup_delivery").values():
        for delivery in case["deliveries"]:
            found.extend(delivery)
    return found


def _outcome_pairs(member: str) -> list[object]:
    pairs = [
        {"outcome": notice["outcome"], member: notice[member]}
        for notice in _notices()
        if isinstance(notice, dict) and "outcome" in notice and member in notice
    ]
    outcomes = sorted({pair["outcome"] for pair in pairs if isinstance(pair["outcome"], str)})
    shapes = [pair[member] for pair in pairs]
    return [*pairs, *({"outcome": outcome, member: shape} for outcome in outcomes for shape in shapes[:12])]


def _usage(value):
    found = arguments(value, "outcome", "usage")
    return None if found is None else routine_notice.notice_usage(*found)


def _detail(value):
    found = arguments(value, "outcome", "detail")
    return None if found is None else routine_notice.canonical_notice_detail(*found)


INSTANTS = [
    "2026-10-01T09:00:00Z",
    "2026-02-29T00:00:00Z",
    "2024-02-29T00:00:00Z",
    "2026-12-31T24:00:00Z",
    "2026-13-01T00:00:00Z",
    "0000-01-01T00:00:00Z",
    "2026-10-01T09:00:60Z",
    "2026-10-01T09:00:00+00:00",
    "2026-10-01 09:00:00Z",
]


def _instants() -> list[object]:
    return [*INSTANTS, *(notice.get("created_at") for notice in _notices() if isinstance(notice, dict))]


ENTRIES = {
    "routine_notice.instant": (routine_notice.instant, _instants),
    "routine_notice.notice_usage": (_usage, lambda: _outcome_pairs("usage")),
    "routine_notice.canonical_notice_detail": (_detail, lambda: _outcome_pairs("detail")),
    "routine_notice.canonical_routine_view": (
        routine_notice.canonical_routine_view,
        lambda: cases("routine_views.routine"),
    ),
    "routine_notice.canonical_run_view": (routine_notice.canonical_run_view, lambda: cases("routine_views.run")),
    "routine_notice.canonical_incident_view": (
        routine_notice.canonical_incident_view,
        lambda: cases("routine_views.incident"),
    ),
    "routine_notice.canonical_notice": (routine_notice.canonical_notice, _notices),
    "routine_notice.canonical_notice_batch": (
        routine_notice.canonical_notice_batch,
        lambda: cases("routine_views.notice_batch"),
    ),
}
