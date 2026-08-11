"""Pure, closed contract between one Assistant and a tool-free inference provider."""

from __future__ import annotations

import json
import re

ACTION_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")


class ChatContractError(ValueError):
    """A provider decision or trusted prompt input violated the closed contract."""


def build_prompt(
    message: str,
    files: list[dict[str, object]],
) -> str:
    request = {
        "files": [
            {
                "id": item["id"],
                "name": item["name"],
                "media_type": item["media_type"],
                "size": item["size"],
            }
            for item in files
        ],
        "message": message,
    }
    return json.dumps(request, separators=(",", ":"), ensure_ascii=False)


def parse_decision(
    raw: str,
    *,
    max_message_chars: int,
    max_input_bytes: int,
) -> tuple[str, str, str, dict[str, object]]:
    try:
        decision = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ChatContractError("invalid Assistant decision") from exc
    if not isinstance(decision, dict) or set(decision) != {"kind", "message", "action", "input"}:
        raise ChatContractError("invalid Assistant decision")
    kind = decision["kind"]
    message = decision["message"]
    action = decision["action"]
    input_json = decision["input"]
    if (
        kind not in {"message", "action"}
        or not isinstance(message, str)
        or len(message) > max_message_chars
        or not isinstance(action, str)
        or not isinstance(input_json, str)
        or len(input_json.encode("utf-8")) > max_input_bytes
    ):
        raise ChatContractError("invalid Assistant decision")
    try:
        action_input = json.loads(input_json)
    except json.JSONDecodeError as exc:
        raise ChatContractError("invalid Action input") from exc
    if not isinstance(action_input, dict):
        raise ChatContractError("invalid Action input")
    if kind == "message" and (not message.strip() or action or action_input):
        raise ChatContractError("invalid direct message")
    if kind == "action" and (message or ACTION_ID_RE.fullmatch(action) is None):
        raise ChatContractError("invalid Action decision")
    return kind, message, action, action_input
