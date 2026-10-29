"""`websocket`. A frame error is the object `{"frame_error": True, "status": s, "close_code": c}` on both sides."""

from __future__ import annotations

import websocket

from . import urls
from .common import arguments, group, small, text


def _frame(call, *values):
    try:
        return call(*values)
    except websocket.FrameError as error:
        return {"frame_error": True, "status": error.status, "close_code": error.close_code}


def _origin(value):
    return websocket.canonical_origin(value) if value is None or isinstance(value, str) else None


def _public_text(value):
    found = arguments(value, "value", "maximum")
    if found is None or small(found[1]) is None:
        return None
    try:
        return websocket.public_text(found[0], max(found[1], 0))
    except ValueError:
        return None


def _decode(value):
    found = arguments(value, "message", "max_bytes")
    if found is None or not isinstance(found[0], dict) or small(found[1]) is None:
        return None
    return _frame(websocket.decode_bounded_json_frame, *found)


def _terminal(value):
    found = arguments(value, "status", "detail", "fallback_detail", "max_detail_chars")
    if found is None or text(found[1]) is None or text(found[2]) is None or small(found[3]) is None:
        return None
    return websocket.error_terminal(found[0], found[1], fallback_detail=found[2], max_detail_chars=found[3])


def _identity(value):
    found = arguments(value, "value", "expected_team_id")
    if found is None or text(found[1]) is None:
        return None
    return websocket.challenge_identity(*found)


def _frames() -> list[object]:
    seeds = []
    for case in group("frames"):
        message = dict(case["message"])
        if "bytes_hex" in message:
            message["bytes"] = message.pop("bytes_hex")
        seeds.append({"message": message, "max_bytes": case["max_bytes"]})
    return seeds


def _origins() -> list[object]:
    return [None, "null", *urls.corpus(2000)]


def _texts() -> list[object]:
    return [
        {"value": value, "maximum": maximum}
        for value, maximum in (("Ready", 10), ("français ?", 10), ("line break", 40), (" bad", 10), ("", 3))
    ]


def _terminals() -> list[object]:
    return [
        {"status": status, "detail": detail, "fallback_detail": "Team failed", "max_detail_chars": 20}
        for status, detail in ((404, "missing"), (True, "x\x07"), (599, ""), (600, "y" * 21), (502.0, "ok"))
    ]


def _identities() -> list[object]:
    challenge = "a" * 32
    return [
        {"value": {"team_id": "team_1", "challenge_id": challenge, "turn_id": challenge}, "expected_team_id": "team_1"},
        {"value": {"team_id": "team_2", "challenge_id": challenge, "turn_id": "b" * 32}, "expected_team_id": "team_2"},
    ]


def _responses() -> list[object]:
    return [case["frame"] for case in group("human_response_frames")]


ENTRIES = {
    "websocket.canonical_origin": (_origin, _origins),
    "websocket.public_text": (_public_text, _texts),
    "websocket.decode_bounded_json_frame": (_decode, _frames),
    "websocket.safe_status": (websocket.safe_status, lambda: [400, 599, 600, True, 502.0, 10**30, "404"]),
    "websocket.error_terminal": (_terminal, _terminals),
    "websocket.valid_challenge_id": (websocket.valid_challenge_id, lambda: ["a" * 32, "A" * 32, None, 7]),
    "websocket.challenge_identity": (_identity, _identities),
    "websocket.canonical_human_response": (lambda value: _frame(websocket.canonical_human_response, value), _responses),
    "websocket.human_value": (
        websocket.human_value,
        lambda: [frame.get("value") for frame in _responses() if isinstance(frame, dict)] + [True, 1, ["a", "a"]],
    ),
}
