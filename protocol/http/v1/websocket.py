"""Vendored, dependency-free primitives shared by both shimpz.chat.v4 surfaces."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

HEX_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
CHALLENGE_ID_RE = HEX_ID_RE
MAX_HUMAN_TEXT_CHARS = 16_000
MAX_HUMAN_CHOICES = 32
MAX_HUMAN_CHOICE_CHARS = 128


class FrameError(ValueError):
    def __init__(self, status: int, detail: str, close_code: int = 1007) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.close_code = close_code


def canonical_origin(value: str | None) -> str | None:
    """Return one exact HTTP(S) Origin, preserving an explicitly supplied port."""
    if not value or value == "null":
        return None
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def public_text(value: object, maximum: int, *, field: str = "public text") -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise ValueError(f"invalid {field}")
    return value


def decode_bounded_json_frame(
    message: dict[str, object],
    max_bytes: int,
    *,
    invalid_json_detail: str = "WebSocket frame must be valid unique-key JSON",
) -> dict[str, object]:
    if message.get("type") != "websocket.receive":
        raise FrameError(400, "invalid WebSocket frame")
    text = message.get("text")
    if not isinstance(text, str) or message.get("bytes") is not None:
        raise FrameError(415, "WebSocket frame must be text JSON", 1003)
    try:
        encoded = text.encode("utf-8")
    except UnicodeError as exc:
        raise FrameError(400, "WebSocket frame must be UTF-8 JSON") from exc
    if len(encoded) > max_bytes:
        raise FrameError(413, "WebSocket frame too large", 1009)
    try:
        value = json.loads(
            text,
            object_pairs_hook=unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError, UnicodeError, ValueError, RecursionError:
        raise FrameError(400, invalid_json_detail) from None
    if not isinstance(value, dict):
        raise FrameError(400, "WebSocket JSON must be an object")
    return value


def safe_status(value: object, fallback: int = 502) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599 else fallback


def error_terminal(
    status: object,
    detail: str,
    *,
    fallback_detail: str,
    max_detail_chars: int,
) -> dict[str, object]:
    safe_detail = (
        detail if 0 < len(detail) <= max_detail_chars and _CONTROL_RE.search(detail) is None else fallback_detail
    )
    return {"type": "error", "status": safe_status(status), "detail": safe_detail}


def valid_challenge_id(value: object) -> bool:
    return isinstance(value, str) and CHALLENGE_ID_RE.fullmatch(value) is not None


def challenge_identity(value: object, expected_team_id: str) -> tuple[str, str] | None:
    """Validate the identity fields common to every challenge schema."""
    if not isinstance(value, dict):
        return None
    challenge_id = value.get("challenge_id")
    turn_id = value.get("turn_id")
    if value.get("team_id") != expected_team_id or not valid_challenge_id(challenge_id) or turn_id != challenge_id:
        return None
    return challenge_id, turn_id


def canonical_human_response(value: object) -> dict[str, object]:
    """Validate one exact Local Admin response to a pending Action human challenge."""
    if (
        not isinstance(value, dict)
        or value.get("type") != "human-response"
        or value.get("decision") not in {"submit", "deny"}
        or not valid_challenge_id(value.get("challenge_id"))
    ):
        raise FrameError(400, "invalid human response frame")
    decision = value["decision"]
    expected = (
        {"type", "challenge_id", "decision", "value"}
        if decision == "submit"
        else {
            "type",
            "challenge_id",
            "decision",
        }
    )
    if set(value) != expected or (decision == "submit" and not _human_value(value["value"])):
        raise FrameError(400, "invalid human response frame")
    return dict(value)


def _human_value(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return len(value) <= MAX_HUMAN_TEXT_CHARS
    return (
        isinstance(value, list)
        and len(value) <= MAX_HUMAN_CHOICES
        and len(value) == len(set(value))
        and all(isinstance(item, str) and len(item) <= MAX_HUMAN_CHOICE_CHARS for item in value)
    )
