#!/usr/bin/env python3
"""Validate the published Assistant protocol artifact set."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from human_request_validator import verify_vectors as verify_human_vectors

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "contract-files.sha256"
ROW = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")
SCHEMAS = (
    "invocation.schema.json",
    "machine-contract.schema.json",
    "manifest.schema.json",
    "result.schema.json",
)


def fail(message: str) -> None:
    raise SystemExit(message)


rows: dict[str, str] = {}
for line in MANIFEST.read_text(encoding="ascii").splitlines():
    match = ROW.fullmatch(line)
    if match is None or match[2] in rows:
        fail("Assistant protocol checksum manifest is invalid")
    rows[match[2]] = match[1]

actual = {path.name for path in HERE.iterdir() if path.is_file() and path.name != MANIFEST.name}
if set(rows) != actual:
    fail("Assistant protocol artifact set differs from its checksum manifest")
for filename, expected in rows.items():
    digest = hashlib.sha256((HERE / filename).read_bytes()).hexdigest()
    if digest != expected:
        fail(f"{filename} SHA-256 is {digest}, expected {expected}")

for filename in SCHEMAS:
    schema = json.loads((HERE / filename).read_bytes())
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        fail(f"{filename} does not declare JSON Schema 2020-12")
    if schema.get("$id") != f"https://schemas.shimpz.com/assistant/v1/{filename}":
        fail(f"{filename} has an invalid canonical ID")

manifest_schema = json.loads((HERE / "manifest.schema.json").read_bytes())
stored_input = manifest_schema.get("$defs", {}).get("storedInput", {})
if (
    manifest_schema.get("properties", {}).get("stored_inputs", {}).get("maxProperties") != 8
    or stored_input.get("additionalProperties") is not False
    or stored_input.get("properties", {}).get("kind", {}).get("const") != "password"
):
    fail("Assistant Stored Input manifest contract is invalid")

invocation = json.loads((HERE / "invocation.schema.json").read_bytes())
if (
    "stored_inputs" not in invocation.get("required", [])
    or invocation.get("properties", {}).get("stored_inputs", {}).get("maxProperties") != 1
):
    fail("Assistant Stored Input invocation contract is invalid")

result = json.loads((HERE / "result.schema.json").read_bytes())
result_types = {
    envelope.get("properties", {}).get("type", {}).get("const")
    for envelope in result.get("oneOf", [])
    if isinstance(envelope, dict)
}
if result_types != {"result", "request", "stored_input_rejected"}:
    fail("Assistant Stored Input result contract is invalid")

vectors = json.loads((HERE / "manifest-vectors.json").read_bytes())
cases = vectors.get("cases") if isinstance(vectors, dict) else None
if not isinstance(vectors, dict) or vectors.get("version") != 1 or not isinstance(cases, list) or not cases:
    fail("Assistant manifest vectors have an invalid root")
names: set[str] = set()
outcomes: set[bool] = set()
for case in cases:
    if (
        not isinstance(case, dict)
        or set(case) != {"manifest", "name", "valid"}
        or not isinstance(case["name"], str)
        or not case["name"]
        or case["name"] in names
        or not isinstance(case["valid"], bool)
        or not isinstance(case["manifest"], str)
        or not case["manifest"]
    ):
        fail("Assistant manifest vector case is invalid")
    names.add(case["name"])
    outcomes.add(case["valid"])
if outcomes != {False, True}:
    fail("Assistant manifest vectors require positive and negative cases")

human = json.loads((HERE / "human-request-vectors.json").read_bytes())
machine = json.loads((HERE / "machine-contract.schema.json").read_bytes())
declared_capabilities = machine["$defs"]["humanRequestCapability"].get("enum")
human_requests = machine["$defs"]["action"]["properties"]["human_requests"]
stored_inputs = machine["$defs"]["action"]["properties"].get("stored_inputs", {})
authorization_capabilities = ["approval", "auth:password", "auth:totp", "auth:passkey"]
if (
    not isinstance(declared_capabilities, list)
    or human_requests.get("contains", {}).get("enum") != authorization_capabilities
    or human_requests.get("minContains") != 0
    or human_requests.get("maxContains") != 1
    or stored_inputs.get("maxItems") != 1
    or "stored_inputs" not in machine["$defs"]["action"].get("required", [])
):
    fail("Assistant human-request vectors are invalid")
try:
    verify_human_vectors(human, declared_capabilities)
except KeyError, TypeError, ValueError:
    fail("Assistant human-request vectors are invalid")

print("Assistant protocol artifacts and conformance vectors are valid")
