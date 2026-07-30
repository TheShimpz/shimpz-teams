#!/usr/bin/env python3
"""Verify the producer-owned Account authority contract."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTRACT_FILES = (
    "README.md",
    "evaluation-request.schema.json",
    "evaluation-response.schema.json",
    "vectors.json",
)
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def fail(message: str) -> None:
    raise SystemExit(message)


hash_rows = {}
for line in (HERE / "contract-files.sha256").read_text(encoding="ascii").splitlines():
    expected, filename = line.split("  ", 1)
    hash_rows[filename] = expected
if set(hash_rows) != set(CONTRACT_FILES):
    fail("authority contract hash inventory is incomplete")
for filename, expected in hash_rows.items():
    actual = hashlib.sha256((HERE / filename).read_bytes()).hexdigest()
    if actual != expected:
        fail(f"{filename} SHA-256 is {actual}, expected {expected}")

for filename in ("evaluation-request.schema.json", "evaluation-response.schema.json"):
    schema = json.loads((HERE / filename).read_bytes())
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        fail(f"{filename} does not declare JSON Schema 2020-12")
    expected_id = f"https://schemas.shimpz.com/account/authority/v1/{filename}"
    if schema.get("$id") != expected_id:
        fail(f"{filename} has an invalid canonical ID")

vectors = json.loads((HERE / "vectors.json").read_bytes())
if vectors.get("version") != 1 or not isinstance(vectors.get("vectors"), list):
    fail("authority vectors have an invalid envelope")
for vector in vectors["vectors"]:
    binding = vector.get("binding")
    expected = vector.get("binding_digest")
    if not isinstance(binding, dict) or not isinstance(expected, str) or SHA256.fullmatch(expected) is None:
        fail("authority vector shape is invalid")
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    actual = hashlib.sha256(encoded).hexdigest()
    if actual != expected:
        fail(f"{vector.get('name', 'unnamed')} digest is {actual}, expected {expected}")

print("Account authority protocol v1 verified")
