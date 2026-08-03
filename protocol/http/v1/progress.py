"""Closed Team chat progress and streamed-terminal framing."""

from __future__ import annotations

import json

import websocket

PHASES = frozenset(
    {
        "model",
        "power",
        "power-preparation",
        "power-result",
        "reply-validation",
        "team-context",
    }
)
STATES = frozenset({"started", "finished"})
MAX_EVENTS = 2_048
MAX_ELAPSED_MS = 24 * 60 * 60 * 1_000
MAX_LINE_BYTES = 128 * 1024
MAX_STREAM_BYTES = 512 * 1024


class ProgressContractError(ValueError):
    """A streamed Team chat record violated the closed protocol."""


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _integer(value: object, *, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
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
    if phase == "power":
        expected.update({"index", "total"})
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
    if phase == "power":
        total = _integer(value["total"], minimum=1, maximum=512, label="Power count")
        event["index"] = _integer(value["index"], minimum=1, maximum=total, label="Power index")
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
        encoded = json.dumps(
            canonical_record(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ProgressContractError("chat stream record is not JSON") from exc
    if len(encoded) > MAX_LINE_BYTES:
        raise ProgressContractError("chat stream record exceeds its limit")
    return encoded


def decode_line(raw: object) -> dict[str, object]:
    """Decode one unique-key UTF-8 NDJSON line and validate its exact shape."""
    if not isinstance(raw, bytes) or not raw.endswith(b"\n") or not 1 <= len(raw) <= MAX_LINE_BYTES:
        raise ProgressContractError("invalid chat stream line")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=websocket.unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError) as exc:
        raise ProgressContractError("invalid chat stream JSON") from exc
    return canonical_record(value)
