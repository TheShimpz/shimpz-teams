#!/usr/bin/env python3
"""Validate Team HTTP protocol integrity and golden vectors."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import payload
import progress
import supervisor
import websocket

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "contract-files.sha256"
ROW = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")


def fail(message: str) -> None:
    raise SystemExit(message)


rows: dict[str, str] = {}
for line in MANIFEST.read_text(encoding="ascii").splitlines():
    match = ROW.fullmatch(line)
    if match is None or match[2] in rows:
        fail("Team HTTP checksum manifest is invalid")
    rows[match[2]] = match[1]
actual = {path.name for path in HERE.iterdir() if path.is_file() and path.name != MANIFEST.name}
if set(rows) != actual:
    fail("Team HTTP artifact set differs from its checksum manifest")
for filename, expected in rows.items():
    digest = hashlib.sha256((HERE / filename).read_bytes()).hexdigest()
    if digest != expected:
        fail(f"{filename} SHA-256 is {digest}, expected {expected}")

vectors = json.loads((HERE / "vectors.json").read_bytes())
if not isinstance(vectors, dict) or vectors.get("version") != 1:
    fail("Team HTTP vectors have an invalid root")
if vectors.get("headers") != {
    "account_session": payload.ACCOUNT_SESSION_HEADER,
    "local_supervisor": supervisor.ASSERTION_HEADER,
}:
    fail("Team HTTP Account session header vector differs")
supervisor_vectors = vectors.get("local_supervisor", {})
for case in supervisor_vectors.get("valid", []):
    if supervisor.canonical_claims(case) != case:
        fail("Team HTTP Local Supervisor positive vector differs")
for case in supervisor_vectors.get("invalid", []):
    try:
        supervisor.canonical_claims(case)
    except supervisor.SupervisorAssertionError:
        continue
    fail("Team HTTP Local Supervisor negative vector differs")
for case in vectors.get("frames", []):
    message = dict(case["message"])
    if "bytes_hex" in message:
        message["bytes"] = bytes.fromhex(message.pop("bytes_hex"))
    try:
        value = websocket.decode_bounded_json_frame(message, case["max_bytes"])
    except websocket.FrameError as exc:
        if case["valid"] or (exc.status, exc.close_code) != (case["status"], case["close_code"]):
            fail(f"Team HTTP frame vector differs: {case['name']}")
    else:
        if not case["valid"] or value != case["value"]:
            fail(f"Team HTTP frame vector differs: {case['name']}")

for case in vectors.get("human_response_frames", []):
    try:
        value = websocket.canonical_human_response(case["frame"])
    except websocket.FrameError:
        if case["valid"]:
            fail(f"Team HTTP human response vector differs: {case['name']}")
    else:
        if not case["valid"] or value != case["frame"]:
            fail(f"Team HTTP human response vector differs: {case['name']}")

for case in vectors.get("chat_stream", []):
    try:
        value = progress.canonical_record(case["record"])
    except progress.ProgressContractError:
        if case["valid"]:
            fail(f"Team HTTP chat stream vector differs: {case['name']}")
    else:
        if not case["valid"] or value != case["record"]:
            fail(f"Team HTTP chat stream vector differs: {case['name']}")

for case in vectors.get("chat_stream_lines", []):
    if case.get("generated") == "over-max-line":
        raw = b"{}" + (b" " * progress.MAX_LINE_BYTES) + b"\n"
    else:
        raw = case["line"].encode("utf-8")
    try:
        value = progress.decode_line(raw)
    except progress.ProgressContractError:
        if case["valid"]:
            fail(f"Team HTTP chat stream line vector differs: {case['name']}")
    else:
        if not case["valid"] or value != case["record"]:
            fail(f"Team HTTP chat stream line vector differs: {case['name']}")

identifiers = vectors.get("identifiers", {})
validators = {
    "team": payload.canonical_team_id,
    "assistant": payload.canonical_assistant_id,
    "action": payload.canonical_action_id,
    "source_digest": payload.canonical_source_digest,
    "assurance_handle": payload.canonical_assurance_handle,
}
for kind, validator in validators.items():
    cases = identifiers.get(kind, {})
    if any(validator(value) != value for value in cases.get("valid", [])):
        fail(f"Team HTTP {kind} positive vector differs")
    if any(validator(value) is not None for value in cases.get("invalid", [])):
        fail(f"Team HTTP {kind} negative vector differs")

action_label_text = vectors.get("action_label_text", {})
for case in action_label_text.get("exemplars", []):
    if payload.canonical_language_exemplar(case["input"]) != case["canonical"]:
        fail("Team HTTP Action-label exemplar positive vector differs")
if any(
    payload.canonical_language_exemplar(value) is not None
    for value in action_label_text.get("invalid_exemplars", [])
):
    fail("Team HTTP Action-label exemplar negative vector differs")
if any(payload.canonical_action_label(value) != value for value in action_label_text.get("labels", [])):
    fail("Team HTTP Action-label label positive vector differs")
if any(payload.canonical_action_label(value) is not None for value in action_label_text.get("invalid_labels", [])):
    fail("Team HTTP Action-label label negative vector differs")

print("Team HTTP protocol integrity and golden vectors are valid")
