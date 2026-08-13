"""Pure, closed contract between one Assistant and a tool-free inference provider."""

from __future__ import annotations

import json
import re

ACTION_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")


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
