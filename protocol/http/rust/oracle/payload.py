"""`identifiers`, `purpose`, `payload`, and the canonical JSON v1 writer."""

from __future__ import annotations

import datetime
import importlib.util

import identifiers
import payload
import purpose

from .common import V1, arguments, cases, group, small, text


def _load_canonical_json():
    spec = importlib.util.spec_from_file_location("team_canonical_json", V1.parents[2] / "core" / "canonical_json.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


canonical_json = _load_canonical_json()


def _fromisoformat(value):
    """The fields of `datetime.datetime.fromisoformat` and its UTC offset in microseconds, or None when it raises."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    offset = parsed.utcoffset()
    fields = [parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute, parsed.second, parsed.microsecond]
    return [*fields, None if offset is None else offset // datetime.timedelta(microseconds=1)]


ISOFORMATS = [
    "2027-W01-1",
    "2026-01-01T12:30x00",
    "2026-W01x1",
    "2026-08-28T17:00:00Z",
    "2026-08-28 17:00:00.123456789+05:30",
    "20260828T170000+0100",
    "20260828T1700",
    "2026-W35-5T17:00:00-03:00",
    "2026W355T24:00:00Z",
    "2026W35T12",
    "2026-W53",
    "2020-W53-7",
    "2026-12-31T24:00:00.000000+00:00",
    "2026-02-29T00:00:00+23:59:59.999999",
    "2024-02-29T23:59:59,5-00:00:00.000001",
    "2026-01-01",
    "2026-01-01T",
    "2026-01-01\u00e9",
    "0001-01-01T00:00Z",
    "9999-12-31T23:59:59.999999-23:59",
]


def _fresh(value):
    found = arguments(value, "issued_at", "now")
    if found is None:
        return None
    issued, now = (small(item) for item in found)
    if issued is None or now is None or abs(issued) >= 2**50 or abs(now) >= 2**50:
        return None
    return payload.request_identity_fresh(issued, now)


def _rendered(value):
    found = arguments(value, "text", "reference", "maximum", "nullable")
    if found is None or type(found[3]) is not bool or small(found[2]) is None or not 0 <= found[2] <= 1_000_000:
        return None
    return payload.rendered_text(found[0], found[1], found[2], nullable=found[3])


def _compose(value):
    found = arguments(value, "original", "question", "answer", "locale")
    if found is None or not all(isinstance(item, str) for item in found):
        return None
    try:
        return payload.compose_clarified(*found)
    except KeyError:
        return None


def _segments(value):
    return list(payload.authored_segments(value)) if isinstance(value, str) else None


def _render(value):
    clarification = payload.canonical_clarification(value)
    return None if clarification is None else payload.render_clarification(clarification)


def _apply(value):
    found = arguments(value, "memory", "changes")
    if found is None:
        return None
    memory, changes = payload.canonical_memory(found[0]), payload.canonical_memory_changes(found[1])
    return None if memory is None or changes is None else payload.apply_memory_changes(memory, changes)


def _integer(value):
    found = arguments(value, "value", "minimum")
    if found is None or type(found[1]) is not int or found[1] not in (0, 1):
        return None
    return payload.integer(found[0], minimum=found[1])


def _metadata(value):
    found = arguments(value, "value", "include_usage")
    if found is None or type(found[1]) is not bool:
        return None
    return payload.project_file_metadata(found[0], include_usage=found[1])


def _response(value):
    found = arguments(value, "value", "kind", "expected_team_id")
    if found is None or text(found[1]) is None or text(found[2]) is None:
        return None
    return payload.project_storage_response(found[0], kind=found[1], expected_team_id=found[2])


def _files() -> list[object]:
    files = [{**item, "created_at": 1_700_000_000} for item in group("file_disclosure.valid")]
    return [*files, *({**item, "created_at": 1} for item in group("file_disclosure.invalid") if isinstance(item, dict))]


def _usage() -> list[object]:
    return [
        {"used_bytes": 10, "limit_bytes": 100, "remaining_bytes": 90},
        {"used_bytes": 120, "limit_bytes": 100, "remaining_bytes": 0},
        {"used_bytes": 0, "limit_bytes": 1, "remaining_bytes": 1},
        {"used_bytes": 2**64, "limit_bytes": 2**65, "remaining_bytes": 2**64},
    ]


def _responses() -> list[object]:
    files = _files()
    usage = _usage()
    upload = [
        {"value": {"team_id": "team_1", "file": {**file, **use}}, "kind": "upload", "expected_team_id": "team_1"}
        for file, use in zip(files, usage * len(files), strict=False)
    ]
    listing = [
        {"value": {"team_id": "team_1", "files": files[:count], **use}, "kind": "list", "expected_team_id": "team_1"}
        for count, use in zip(range(len(files) + 1), usage * 4, strict=False)
    ]
    return [*upload, *listing, {"value": {"team_id": "t"}, "kind": "other", "expected_team_id": "t"}]


def _messages() -> list[object]:
    composed = group("clarification_labels.composed")
    segments = group("clarification_labels.authored_segments")
    return [*(case["message"] for case in composed), *(case["message"] for case in segments)]


def _composed() -> list[object]:
    keys = ("original", "question", "answer", "locale")
    return [{key: case[key] for key in keys} for case in group("clarification_labels.composed")]


def _seeds(*paths: str):
    return lambda: cases(*paths)


ENTRIES = {
    "datetime.fromisoformat": (_fromisoformat, lambda: ISOFORMATS),
    "canonical_json.encode": (
        lambda value: canonical_json.encode(value).decode(),
        lambda: [case["value"] for case in group("canonical_json.canonical")],
    ),
    "identifiers.canonical_team_id": (identifiers.canonical_team_id, _seeds("identifiers.team")),
    "identifiers.canonical_assistant_id": (identifiers.canonical_assistant_id, _seeds("identifiers.assistant")),
    "identifiers.canonical_identifier": (identifiers.canonical_identifier, _seeds("identifiers.identifier")),
    "identifiers.canonical_action_id": (identifiers.canonical_action_id, _seeds("identifiers.action")),
    "purpose.canonical_purpose": (
        purpose.canonical_purpose,
        lambda: [*cases("purpose"), "a\u2029b", "a\u2028b", "a\u2010b", "a-b", "a\u200bb", "WWW.x"],
    ),
    "payload.canonical_conversation": (
        payload.canonical_conversation,
        lambda: [
            *(case for case in cases("chat_conversation") if not (isinstance(case, dict) and "generated" in case)),
            [{"role": "user", "text": "a\u200bb\nc\td", "truncated": False}],
            [{"role": "assistant", "text": "a\x01", "truncated": True}],
        ],
    ),
    "payload.canonical_locale": (payload.canonical_locale, _seeds("chat_locale")),
    "payload.request_identity_fresh": (
        _fresh,
        lambda: [{"issued_at": issued, "now": 1_000_000} for issued in (999_100, 999_101, 1_000_060, 1_000_061)],
    ),
    "payload.canonical_request_identity": (payload.canonical_request_identity, _seeds("chat_request_identity")),
    "payload.canonical_help_url": (payload.canonical_help_url, _seeds("help_url", "creator_links")),
    "payload.canonical_stored_input_help": (payload.canonical_stored_input_help, _seeds("stored_input_help")),
    "payload.canonical_creator_links": (payload.canonical_creator_links, _seeds("creator_links")),
    "payload.canonical_pack_digest": (payload.canonical_pack_digest, _seeds("pack_digest")),
    "payload.rendered_text": (
        _rendered,
        lambda: [
            {"text": text, "reference": reference, "maximum": 5, "nullable": nullable}
            for text in (None, "abc", " a", "abcdef", "a\u2028", "e\u0301")
            for reference in (None, "x")
            for nullable in (True, False)
        ],
    ),
    "payload.canonical_snapshot_summary": (payload.canonical_snapshot_summary, _seeds("snapshot_summary")),
    "payload.canonical_local_assistant": (payload.canonical_local_assistant, _seeds("local_assistant")),
    "payload.canonical_created_at": (
        payload.canonical_created_at,
        lambda: [
            "2026-08-28T17:00:00Z",
            "2026-08-28T17:00:00.123456789+05:30",
            "20260828T170000+0100",
            "2026-W35-5T17:00:00-03:00",
            "2026W355T24:00:00Z",
            "2026-12-31T24:00:00.000000+00:00",
            "2026-02-29T00:00:00+23:59:59.999999",
        ],
    ),
    "payload.canonical_assistant_details": (payload.canonical_assistant_details, _seeds("assistant_details")),
    "payload.canonical_turn_usage": (payload.canonical_turn_usage, _seeds("turn_usage")),
    "payload.canonical_run_usage": (payload.canonical_run_usage, _seeds("routine_run_usage")),
    "payload.canonical_clarification": (
        payload.canonical_clarification,
        lambda: [
            *cases("clarification"),
            *(
                {**case, "question": case["question"][:1] + mark + case["question"][1:]}
                for case in group("clarification.valid")
                for mark in ("\u2028", "\u2029", "\u200b", "\x7f")
            ),
        ],
    ),
    "payload.compose_clarified": (_compose, _composed),
    "payload.authored_segments": (_segments, _messages),
    "payload.render_clarification": (_render, _seeds("clarification")),
    "payload.canonical_memory": (payload.canonical_memory, _seeds("memory")),
    "payload.canonical_memory_changes": (payload.canonical_memory_changes, _seeds("memory_changes")),
    "payload.apply_memory_changes": (
        _apply,
        lambda: [{"memory": case["memory"], "changes": case["changes"]} for case in group("memory_apply")],
    ),
    "payload.canonical_source_digest": (payload.canonical_source_digest, _seeds("identifiers.source_digest")),
    "payload.canonical_team_name": (payload.canonical_team_name, _seeds("identifiers.local_team_name")),
    "payload.canonical_chat_limit": (payload.canonical_chat_limit, _seeds("chat_limit")),
    "payload.canonical_local_team_name": (payload.canonical_local_team_name, _seeds("identifiers.local_team_name")),
    "payload.canonical_file_id": (payload.canonical_file_id, lambda: [file["id"] for file in _files()]),
    "payload.canonical_filename": (
        payload.canonical_filename,
        lambda: [*(file["name"] for file in _files()), "", ".", "..", "a/b", "a\\b", " a", "a\x7f", "\u00e9" * 128],
    ),
    "payload.canonical_media_type": (
        payload.canonical_media_type,
        lambda: [*(file["media_type"] for file in _files()), None, "", "Text/Plain", "image/svg+xml"],
    ),
    "payload.integer": (
        _integer,
        lambda: [{"value": value, "minimum": minimum} for value in (0, 1, 2**64, -1, True) for minimum in (0, 1)],
    ),
    "payload.project_storage_usage": (payload.project_storage_usage, _usage),
    "payload.project_file_metadata": (
        _metadata,
        lambda: [
            {"value": {**file, **use}, "include_usage": include}
            for file in _files()
            for use in _usage()
            for include in (True, False)
        ],
    ),
    "payload.project_storage_response": (_response, _responses),
}
