"""Canonical JSON bytes: sorted keys, compact separators, non-ASCII UTF-8, and no NaN or infinity.

Team fingerprints, digests, and sealed-state bindings hash these exact bytes. ``encode`` raises the encoder's own
TypeError, ValueError, UnicodeError, or RecursionError, so each caller keeps its own refusal.
"""

from __future__ import annotations

import json


def encode(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
