"""Closed Team chat progress and streamed-terminal framing."""

from __future__ import annotations

import json
import re

PHASES = frozenset(
    {
        "model",
        "action",
        "action-delivery",
        "action-preparation",
        "team-context",
    }
)
STATES = frozenset({"started", "finished"})
MAX_EVENTS = 2_048
MAX_ELAPSED_MS = 24 * 60 * 60 * 1_000
MAX_ASSISTANT_ID_CHARS = 40
MAX_ACTION_ID_CHARS = 80
IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
# Exact compact JSON size of the largest valid finished Action progress record, including newline.
MAX_PROGRESS_LINE_BYTES = 263
MAX_LINE_BYTES = 256 * 1024
MAX_STREAM_BYTES = MAX_EVENTS * MAX_PROGRESS_LINE_BYTES + MAX_LINE_BYTES


class ProgressContractError(ValueError):
    """A streamed Team chat record violated the closed protocol."""


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def _integer(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProgressContractError(f"invalid {label}")
    return value


def _identifier(value: object, *, maximum: int, label: str) -> str:
    if not isinstance(value, str) or len(value) > maximum or IDENTIFIER_RE.fullmatch(value) is None:
        raise ProgressContractError(f"invalid {label}")
    return value


def canonical_event(value: object) -> dict[str, object]:
    """Return one metadata-only progress occurrence."""
    if not isinstance(value, dict):
        raise ProgressContractError("invalid progress event")
    phase = value.get("phase")
    state = value.get("state")
    if phase not in PHASES or state not in STATES:
        raise ProgressContractError("invalid progress event")
    expected = {"seq", "phase", "state"}
    if state == "finished":
        expected.add("elapsed_ms")
    if phase == "action":
        expected.update({"assistant_id", "index", "action", "total"})
    if set(value) != expected:
        raise ProgressContractError("invalid progress event fields")
    event: dict[str, object] = {
        "seq": _integer(value["seq"], minimum=1, maximum=MAX_EVENTS, label="progress sequence"),
        "phase": phase,
        "state": state,
    }
    if state == "finished":
        event["elapsed_ms"] = _integer(
            value["elapsed_ms"],
            minimum=0,
            maximum=MAX_ELAPSED_MS,
            label="progress duration",
        )
    if phase == "action":
        total = _integer(value["total"], minimum=1, maximum=512, label="Action count")
        event["assistant_id"] = _identifier(
            value["assistant_id"],
            maximum=MAX_ASSISTANT_ID_CHARS,
            label="Assistant id",
        )
        event["index"] = _integer(value["index"], minimum=1, maximum=total, label="Action index")
        event["action"] = _identifier(value["action"], maximum=MAX_ACTION_ID_CHARS, label="Action id")
        event["total"] = total
    return event


def canonical_record(value: object) -> dict[str, object]:
    """Return one exact progress or terminal stream record."""
    if not isinstance(value, dict):
        raise ProgressContractError("invalid chat stream record")
    kind = value.get("type")
    if kind == "progress":
        event = {key: item for key, item in value.items() if key != "type"}
        return {"type": "progress", **canonical_event(event)}
    if kind == "terminal":
        if set(value) != {"type", "status", "body"} or not isinstance(value.get("body"), dict):
            raise ProgressContractError("invalid chat terminal record")
        return {
            "type": "terminal",
            "status": _integer(value["status"], minimum=200, maximum=599, label="terminal status"),
            "body": dict(value["body"]),
        }
    raise ProgressContractError("invalid chat stream record type")


def encode_record(value: object) -> bytes:
    """Encode one canonical NDJSON record within its independent line bound."""
    try:
        canonical = canonical_record(value)
        encoded = (
            json.dumps(
                canonical,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ProgressContractError("chat stream record is not JSON") from exc
    maximum = MAX_PROGRESS_LINE_BYTES if canonical["type"] == "progress" else MAX_LINE_BYTES
    if len(encoded) > maximum:
        raise ProgressContractError("chat stream record exceeds its limit")
    return encoded


def decode_line(raw: object) -> dict[str, object]:
    """Decode one unique-key UTF-8 NDJSON line and validate its exact shape."""
    if not isinstance(raw, bytes) or not raw.endswith(b"\n") or not 1 <= len(raw) <= MAX_LINE_BYTES:
        raise ProgressContractError("invalid chat stream line")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError) as exc:
        raise ProgressContractError("invalid chat stream JSON") from exc
    record = canonical_record(value)
    if record["type"] == "progress" and len(raw) > MAX_PROGRESS_LINE_BYTES:
        raise ProgressContractError("chat progress line exceeds its limit")
    return record
