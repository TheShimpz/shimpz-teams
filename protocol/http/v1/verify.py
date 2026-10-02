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

import routine

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
routine_vectors = vectors.get("local_routine", {})
if not routine_vectors.get("valid") or not routine_vectors.get("invalid"):
    fail("Team HTTP Local Routine vectors are missing")
for case in routine_vectors["valid"]:
    try:
        admitted = supervisor.canonical_claims(case, audience=supervisor.ROUTINE_AUDIENCE)
    except supervisor.SupervisorAssertionError:
        admitted = None
    if admitted != case:
        fail("Team HTTP Local Routine positive vector differs")
for case in routine_vectors["invalid"]:
    try:
        supervisor.canonical_claims(case, audience=supervisor.ROUTINE_AUDIENCE)
    except supervisor.SupervisorAssertionError:
        continue
    fail("Team HTTP Local Routine negative vector differs")
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


def _conversation_case(case: object) -> object:
    generated = case.get("generated") if isinstance(case, dict) and set(case) == {"generated"} else None
    entry = {"role": "user", "truncated": False}
    if generated == "eight-maximal-entries":
        return [{**entry, "text": "x" * payload.MAX_CONVERSATION_TEXT_CHARS}] * payload.MAX_CONVERSATION_ENTRIES
    if generated == "nine-entries":
        return [{**entry, "text": "x"}] * (payload.MAX_CONVERSATION_ENTRIES + 1)
    if generated == "overlong-entry":
        return [{**entry, "text": "x" * (payload.MAX_CONVERSATION_TEXT_CHARS + 1)}]
    return case


conversations = vectors.get("chat_conversation", {})
if not conversations.get("valid") or not conversations.get("invalid"):
    fail("Team HTTP chat conversation vectors are missing")
for case in conversations["valid"]:
    window = _conversation_case(case)
    if payload.canonical_conversation(window) != window:
        fail("Team HTTP chat conversation positive vector differs")
for case in conversations["invalid"]:
    if payload.canonical_conversation(_conversation_case(case)) is not None:
        fail("Team HTTP chat conversation negative vector differs")

identifiers = vectors.get("identifiers", {})
validators = {
    "team": payload.canonical_team_id,
    "assistant": payload.canonical_assistant_id,
    "action": payload.canonical_action_id,
    "source_digest": payload.canonical_source_digest,
    "assurance_handle": payload.canonical_assurance_handle,
    "local_team_name": payload.canonical_local_team_name,
}
for kind, validator in validators.items():
    cases = identifiers.get(kind, {})
    if any(validator(value) != value for value in cases.get("valid", [])):
        fail(f"Team HTTP {kind} positive vector differs")
    if any(validator(value) is not None for value in cases.get("invalid", [])):
        fail(f"Team HTTP {kind} negative vector differs")

clarifications = vectors.get("clarification", {})
if not clarifications.get("valid") or not clarifications.get("invalid"):
    fail("clarification vectors are missing")
if any(payload.canonical_clarification(value) != value for value in clarifications["valid"]):
    fail("a valid clarification vector was not admitted exactly")
if any(payload.canonical_clarification(value) is not None for value in clarifications["invalid"]):
    fail("an invalid clarification vector was admitted")
if [payload.render_clarification(value) for value in clarifications["valid"]] != clarifications.get("rendered"):
    fail("a clarification rendering vector differs")
for kind, validator in (("memory", payload.canonical_memory), ("memory_changes", payload.canonical_memory_changes)):
    cases = vectors.get(kind, {})
    if not cases.get("valid") or not cases.get("invalid"):
        fail(f"{kind} vectors are missing")
    if any(validator(value) != value for value in cases["valid"]):
        fail(f"a valid {kind} vector was not admitted exactly")
    if any(validator(value) is not None for value in cases["invalid"]):
        fail(f"an invalid {kind} vector was admitted")
skills = vectors.get("skills", {})
if not skills.get("valid") or not skills.get("invalid"):
    fail("skills vectors are missing")
if any(payload.canonical_skills(value) != value for value in skills["valid"]):
    fail("a valid skills vector was not admitted exactly")
if any(payload.canonical_skills(value) is not None for value in skills["invalid"]):
    fail("an invalid skills vector was admitted")
knowledge = vectors.get("knowledge_apply", [])
if not knowledge or any(
    payload.apply_knowledge(case["memory"], case["skills"], case["changes"], case["skill"])
    != (case["result"]["memory"], case["result"]["skills"])
    for case in knowledge
):
    fail("a knowledge application vector differs")
applied = vectors.get("memory_apply", [])
if not applied or any(
    payload.apply_memory_changes(case["memory"], case["changes"]) != case["result"] for case in applied
):
    fail("a memory application vector differs")
action_label_text = vectors.get("action_label_text", {})
for name, admit in (
    ("chat_locale", payload.canonical_locale),
    ("chat_request_identity", payload.canonical_request_identity),
    ("help_url", payload.canonical_help_url),
    ("file_disclosure", payload.canonical_file_disclosure),
    ("purpose", payload.canonical_purpose),
    ("pack_digest", payload.canonical_pack_digest),
    ("snapshot_summary", payload.canonical_snapshot_summary),
    ("routine_challenge_open", routine.canonical_challenge_open),
    ("turn_usage", payload.canonical_turn_usage),
):
    cases = vectors.get(name, {})
    if not cases.get("valid") or not cases.get("invalid"):
        fail(f"Team HTTP {name} vectors are missing")
    if any(admit(value) != value for value in cases["valid"]):
        fail(f"Team HTTP {name} positive vector differs")
    if any(admit(value) is not None for value in cases["invalid"]):
        fail(f"Team HTTP {name} negative vector differs")
rendered_copy = vectors.get("rendered_copy", {})
if not rendered_copy.get("valid") or not rendered_copy.get("invalid"):
    fail("Team HTTP rendered copy vectors are missing")
if any(
    payload.canonical_rendered(case["rendered"], case["request"]) != case["rendered"] for case in rendered_copy["valid"]
):
    fail("Team HTTP rendered copy positive vector differs")
if any(payload.canonical_rendered(case["rendered"], case["request"]) is not None for case in rendered_copy["invalid"]):
    fail("Team HTTP rendered copy negative vector differs")
if any(payload.canonical_action_label(value) != value for value in action_label_text.get("labels", [])):
    fail("Team HTTP Action-label label positive vector differs")
if any(payload.canonical_action_label(value) is not None for value in action_label_text.get("invalid_labels", [])):
    fail("Team HTTP Action-label label negative vector differs")

schedules = vectors.get("routine_schedule", {})
if not schedules.get("valid") or not schedules.get("invalid") or not schedules.get("daily_rate"):
    fail("routine schedule vectors are missing")
if any(routine.canonical_schedule(value) != value for value in schedules["valid"]):
    fail("a valid routine schedule vector was not admitted exactly")
if any(routine.canonical_schedule(value) is not None for value in schedules["invalid"]):
    fail("an invalid routine schedule vector was admitted")
if any(str(routine.daily_rate(case["schedule"])) != case["rate"] for case in schedules["daily_rate"]):
    fail("a routine daily rate vector differs")
timezones = vectors.get("routine_timezone", {})
if not timezones.get("valid") or not timezones.get("invalid"):
    fail("routine timezone vectors are missing")
if any(routine.canonical_timezone(value) != value for value in timezones["valid"]):
    fail("a valid routine timezone vector was not admitted exactly")
if any(routine.canonical_timezone(value) is not None for value in timezones["invalid"]):
    fail("an invalid routine timezone vector was admitted")

views = vectors.get("routine_views", {})
admit_view = {
    "routine": routine.canonical_routine_view,
    "run": routine.canonical_run_view,
    "notice_batch": routine.canonical_notice_batch,
    "claim": routine.canonical_claim,
    "claim_request": routine.canonical_claim_request,
    "incident": routine.canonical_incident_view,
    "card": routine.canonical_card,
    "card_answer_request": routine.canonical_card_answer_request,
    "card_answer": routine.canonical_card_answer,
    "segment_request": routine.canonical_segment_request,
}
if set(views) != set(admit_view) or any(
    not views[kind].get("valid") or not views[kind].get("invalid") for kind in views
):
    fail("routine view vectors are missing")
for kind, admit in admit_view.items():
    if any(admit(value) != value for value in views[kind]["valid"]):
        fail(f"a valid routine {kind} vector was not admitted exactly")
    if any(admit(value) is not None for value in views[kind]["invalid"]):
        fail(f"an invalid routine {kind} vector was admitted")

diagnostics = vectors.get("routine_diagnostics", {})
if not diagnostics.get("valid") or not diagnostics.get("invalid"):
    fail("routine diagnostics vectors are missing")
if any(routine.canonical_diagnostics(value) != value for value in diagnostics["valid"]):
    fail("a valid routine diagnostics vector was not admitted exactly")
if any(routine.canonical_diagnostics(value) is not None for value in diagnostics["invalid"]):
    fail("an invalid routine diagnostics vector was admitted")

print("Team HTTP protocol integrity and golden vectors are valid")
