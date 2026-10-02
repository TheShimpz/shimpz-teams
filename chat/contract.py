"""Pure, closed contract between one Assistant and a tool-free inference provider."""

from __future__ import annotations

import json

from inference import client as brain_runtime_client
from protocol.http.v1 import payload as http_payload


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


def conversation_window(value: object) -> tuple[brain_runtime_client.RuntimeConversationEntry, ...]:
    """Admit one untrusted window of committed presentation history through Team's HTTP reference validator."""
    entries = http_payload.canonical_conversation(value)
    if entries is None:
        raise ValueError("invalid conversation window")
    return tuple(
        brain_runtime_client.RuntimeConversationEntry(entry["role"], entry["text"], entry["truncated"])
        for entry in entries
    )
