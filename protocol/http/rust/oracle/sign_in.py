"""`sign_in`."""

from __future__ import annotations

import sign_in

from . import urls
from .common import cases


def _sign_ins() -> list[object]:
    found = cases("sign_in_current", "sign_in_saved")
    return [case["sign_in"] for case in found if isinstance(case, dict) and case.get("sign_in") is not None]


def _origins() -> list[object]:
    starts = [case.get("origin") for case in cases("sign_in_start") if isinstance(case, dict)]
    return [*starts, *urls.corpus(1000)]


ENTRIES = {
    "sign_in.canonical_site": (
        sign_in.canonical_site,
        lambda: ["example.com", "a.co", "Example.com", "localhost", "x" * 250 + ".com", "a-.com"],
    ),
    "sign_in.start_site": (
        sign_in.start_site,
        lambda: [case.get("url") for case in cases("sign_in_start") if isinstance(case, dict)] + urls.corpus(2000),
    ),
    "sign_in.canonical_origin": (sign_in.canonical_origin, _origins),
    "sign_in.canonical_start": (sign_in.canonical_start, lambda: cases("sign_in_start")),
    "sign_in.canonical_session": (sign_in.canonical_session, lambda: cases("sign_in_session")),
    "sign_in.canonical_delete": (sign_in.canonical_delete, lambda: cases("sign_in_delete")),
    "sign_in.canonical_started": (sign_in.canonical_started, lambda: cases("sign_in_started")),
    "sign_in.canonical_sign_in": (sign_in.canonical_sign_in, _sign_ins),
    "sign_in.canonical_current": (sign_in.canonical_current, lambda: cases("sign_in_current")),
    "sign_in.canonical_saved": (sign_in.canonical_saved, lambda: cases("sign_in_saved")),
    "sign_in.canonical_deleted": (sign_in.canonical_deleted, lambda: cases("sign_in_deleted")),
    "sign_in.canonical_cancelled": (
        sign_in.canonical_cancelled,
        lambda: [*cases("sign_in_cancelled"), {"cancelled": 1}, {"cancelled": 1.0}],
    ),
}
