"""Reference validation for the sanitized Action failure envelope (Assistant Spec v1).

A handled failure is ``{"type": "failure", "failure": {...}}`` with exactly these closed diagnostic members. Text
bounds count UTF-8 bytes, so a stored diagnostic stays bounded whatever its script.
"""

from __future__ import annotations

import re

FAILURE_KEYS = ("error_type", "message", "provider", "http_status", "response_excerpt", "redacted", "truncated")
MAX_TEXT_BYTES = 2048
MAX_PROVIDER = 253
# An error type is one printable ASCII word, such as ``httpx.HTTPStatusError``.
ERROR_TYPE = re.compile(r"[!-~]{1,128}")
# Diagnostic text keeps tab and line feed; every other control, bidi override or isolate, and zero-width formatting
# character is refused, so escaped literal rendering cannot be reordered or hidden.
UNSAFE_TEXT = re.compile(r"[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
PROVIDER = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")


def failure_error(envelope: object) -> str | None:
    """Return a stable reason when one stdout failure envelope is refused."""
    if not isinstance(envelope, dict) or set(envelope) != {"type", "failure"} or envelope["type"] != "failure":
        return "envelope_invalid"
    failure = envelope["failure"]
    if not isinstance(failure, dict) or set(failure) != set(FAILURE_KEYS):
        return "failure_invalid"
    return next((code for code, admitted in MEMBER_RULES if not admitted(failure)), None)


def _error_type_admitted(failure: dict[str, object]) -> bool:
    error_type = failure["error_type"]
    return isinstance(error_type, str) and ERROR_TYPE.fullmatch(error_type) is not None


def _excerpt_admitted(failure: dict[str, object]) -> bool:
    return failure["response_excerpt"] is None or text_admitted(failure["response_excerpt"])


def _provider_admitted(failure: dict[str, object]) -> bool:
    provider = failure["provider"]
    return provider is None or (
        isinstance(provider, str) and len(provider) <= MAX_PROVIDER and PROVIDER.fullmatch(provider) is not None
    )


def _status_admitted(failure: dict[str, object]) -> bool:
    status = failure["http_status"]
    return status is None or (type(status) is int and 100 <= status <= 599)


def _flags_admitted(failure: dict[str, object]) -> bool:
    return type(failure["redacted"]) is bool and type(failure["truncated"]) is bool


def text_admitted(value: object) -> bool:
    """Return whether one diagnostic text is safe Unicode within the byte bound."""
    if not isinstance(value, str) or UNSAFE_TEXT.search(value) is not None:
        return False
    try:
        return len(value.encode("utf-8")) <= MAX_TEXT_BYTES
    except UnicodeEncodeError:
        return False


MEMBER_RULES = (
    ("error_type_invalid", _error_type_admitted),
    ("message_invalid", lambda failure: text_admitted(failure["message"])),
    ("response_excerpt_invalid", _excerpt_admitted),
    ("provider_invalid", _provider_admitted),
    ("http_status_invalid", _status_admitted),
    ("flag_invalid", _flags_admitted),
)
