"""`progress`."""

from __future__ import annotations

import progress

from .common import group

ERRORS = {progress.ProgressContractError: "progress"}


def _records() -> list[object]:
    edges = [
        {"type": "terminal", "status": 200, "body": []},
        {"type": "terminal", "status": 200, "body": {}, "x": 1},
        {"type": "terminal", "status": 600, "body": {}},
        {"type": "other"},
        [],
    ]
    return [
        *(case["record"] for case in group("chat_stream")),
        *(case["record"] for case in group("canonical_json.encode_record")),
        *edges,
    ]


def _decode(value):
    return progress.decode_line(value.encode()) if isinstance(value, str) else None


ENTRIES = {
    "progress.canonical_event": (
        progress.canonical_event,
        lambda: [
            {key: item for key, item in record.items() if key != "type"}
            for record in _records()
            if isinstance(record, dict)
        ],
    ),
    "progress.canonical_record": (progress.canonical_record, _records),
    "progress.encode_record": (lambda value: progress.encode_record(value).decode(), _records),
    "progress.decode_line": (
        _decode,
        lambda: [case["line"] for case in group("chat_stream_lines") if "line" in case],
    ),
}
