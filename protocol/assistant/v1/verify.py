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
if not isinstance(declared_capabilities, list):
    fail("Assistant human-request vectors are invalid")
try:
    verify_human_vectors(human, declared_capabilities)
except KeyError, TypeError, ValueError:
    fail("Assistant human-request vectors are invalid")

print("Assistant protocol artifacts and conformance vectors are valid")
