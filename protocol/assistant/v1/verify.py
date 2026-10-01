#!/usr/bin/env python3
"""Validate the published Assistant protocol artifact set."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path

from action_effect_validator import EFFECTS, effect_error
from failure_validator import FAILURE_KEYS, failure_error
from human_request_validator import reference_error
from human_request_validator import verify_vectors as verify_human_vectors
from message_catalog_validator import LOCALES, MAX_MESSAGES, PACK_FORMAT, PARAM_BOUNDS, catalog_error
from message_catalog_validator import verify_vectors as verify_catalog_vectors

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "contract-files.sha256"
ROW = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")
SCHEMAS = (
    "invocation.schema.json",
    "language-pack.schema.json",
    "machine-contract.schema.json",
    "manifest.schema.json",
    "result.schema.json",
)
# Every Draft 2020-12 position whose value is a subschema; Team, the Brain, and publication walk exactly these.
MAX_EXPANDED_SUBSCHEMAS = 4096
APPLICATORS = (
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
)
LIST_APPLICATORS = ("allOf", "anyOf", "oneOf", "prefixItems")
MAP_APPLICATORS = ("$defs", "definitions", "dependentSchemas", "patternProperties", "properties")
LOCAL_DEFINITION = re.compile(r"#/(\$defs|definitions)/([^/%]+)")
# The canonical lowercase text of a random RFC 9562 version 4 UUID.
OPERATION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def fail(message: str) -> None:
    raise SystemExit(message)


def verify_verdict_vectors(filename: str, label: str, fields: dict[str, type], verdict: str = "valid") -> None:
    vectors = json.loads((HERE / filename).read_bytes())
    cases = vectors.get("cases") if isinstance(vectors, dict) else None
    if not isinstance(vectors, dict) or vectors.get("version") != 1 or not isinstance(cases, list) or not cases:
        fail(f"Assistant {label} vectors have an invalid root")
    names: set[str] = set()
    outcomes: set[bool] = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or set(case) != {*fields, "name", verdict}
            or not isinstance(case["name"], str)
            or not case["name"]
            or case["name"] in names
            or not isinstance(case[verdict], bool)
            or not all(isinstance(case[field], kind) and case[field] for field, kind in fields.items())
        ):
            fail(f"Assistant {label} vector case is invalid")
        names.add(case["name"])
        outcomes.add(case[verdict])
    if outcomes != {False, True}:
        fail(f"Assistant {label} vectors require positive and negative cases")


def verify_reference_vectors(filename: str, label: str, field: str, error: Callable[[object], str | None]) -> None:
    """Require named positive and negative cases whose verdicts the reference validator reproduces."""
    vectors = json.loads((HERE / filename).read_bytes())
    cases = vectors.get("cases") if isinstance(vectors, dict) else None
    if not isinstance(vectors, dict) or vectors.get("version") != 1 or not isinstance(cases, list) or not cases:
        fail(f"Assistant {label} vectors have an invalid root")
    names: set[str] = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or set(case) != {"name", field, "valid"}
            or not isinstance(case["name"], str)
            or not case["name"]
            or case["name"] in names
            or not isinstance(case["valid"], bool)
        ):
            fail(f"Assistant {label} vector case is invalid")
        names.add(case["name"])
        if (error(case[field]) is None) != case["valid"]:
            fail(f"Assistant {label} vector {case['name']!r} disagrees with the reference validator")
    if {case["valid"] for case in cases} != {False, True}:
        fail(f"Assistant {label} vectors require positive and negative cases")


def reference_target(root: dict, reference: object) -> object:
    if reference == "#":
        return root
    match = LOCAL_DEFINITION.fullmatch(reference) if isinstance(reference, str) else None
    definitions = root.get(match[1]) if match else None
    return definitions.get(match[2].replace("~1", "/").replace("~0", "~")) if isinstance(definitions, dict) else None


def expanded_subschemas(root: dict) -> int | None:
    """Count subschema values with every reference expanded; None when one is missing or leads back into itself."""
    counts: dict[int, int] = {}

    def count(node: object, open_nodes: frozenset[int]) -> int | None:
        if not isinstance(node, dict):
            return 1
        if id(node) in open_nodes:
            return None
        if id(node) not in counts:
            edges = [node[keyword] for keyword in APPLICATORS if keyword in node]
            edges += [
                child for keyword in LIST_APPLICATORS if isinstance(node.get(keyword), list) for child in node[keyword]
            ]
            edges += [
                child
                for keyword in MAP_APPLICATORS
                if isinstance(node.get(keyword), dict)
                for child in node[keyword].values()
            ]
            if "$ref" in node:
                edges.append(reference_target(root, node["$ref"]))
                if edges[-1] is None:
                    return None
            sizes = [count(edge, open_nodes | {id(node)}) for edge in edges]
            if None in sizes:
                return None
            counts[id(node)] = 1 + sum(sizes)
        return counts[id(node)]

    return count(root, frozenset())


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
    or "help_url" in stored_input.get("required", [])
    or manifest_schema.get("$defs", {}).get("helpUrl", {}).get("maxLength") != 2048
):
    fail("Assistant Stored Input manifest contract is invalid")

invocation = json.loads((HERE / "invocation.schema.json").read_bytes())
if (
    "stored_inputs" not in invocation.get("required", [])
    or invocation.get("properties", {}).get("stored_inputs", {}).get("maxProperties") != 1
):
    fail("Assistant Stored Input invocation contract is invalid")
if (
    "operation_id" not in invocation.get("required", [])
    or invocation.get("$defs", {}).get("operationId", {}).get("pattern") != f"^{OPERATION_ID.pattern}$"
):
    fail("Assistant operation_id invocation contract is invalid")

result = json.loads((HERE / "result.schema.json").read_bytes())
result_types = {
    envelope.get("properties", {}).get("type", {}).get("const")
    for envelope in result.get("oneOf", [])
    if isinstance(envelope, dict)
}
if result_types != {"result", "request", "stored_input_rejected", "failure"}:
    fail("Assistant result envelope contract is invalid")
failure_schema = result.get("$defs", {}).get("failure", {})
if failure_schema.get("required") != list(FAILURE_KEYS) or failure_schema.get("additionalProperties") is not False:
    fail("Assistant failure envelope contract is invalid")
verify_reference_vectors("failure-vectors.json", "failure", "response", failure_error)


verify_verdict_vectors("manifest-vectors.json", "manifest", {"manifest": str})
verify_verdict_vectors("action-schema-vectors.json", "Action schema", {"schema": dict})
expansions = {
    (case["valid"], expanded_subschemas(case["schema"]))
    for case in json.loads((HERE / "action-schema-vectors.json").read_bytes())["cases"]
}
if not {(True, MAX_EXPANDED_SUBSCHEMAS), (False, MAX_EXPANDED_SUBSCHEMAS + 1)} <= expansions or any(
    valid and (size is None or size > MAX_EXPANDED_SUBSCHEMAS) for valid, size in expansions
):
    fail("Assistant Action schema vectors do not pin the expanded-reference bound")
verify_verdict_vectors("pattern-vectors.json", "pattern", {"pattern": str, "subject": str}, "matches")
verify_verdict_vectors("invocation-vectors.json", "invocation", {"invocation": dict})

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
    if catalog_error(human["catalog"]["messages"], human["catalog"]["summary"]) is not None:
        fail("Assistant human-request vector catalog is invalid")
    verify_human_vectors(human, declared_capabilities)
except KeyError, TypeError, ValueError:
    fail("Assistant human-request vectors are invalid")

action_properties = machine["$defs"]["action"]["properties"]
if (
    "effect" not in machine["$defs"]["action"].get("required", [])
    or action_properties.get("effect", {}).get("enum") != list(EFFECTS)
    or "verifier" in machine["$defs"]["action"].get("required", [])
    or machine["$defs"].get("verifier", {}).get("additionalProperties") is not False
    or "idempotency" in machine["$defs"]["action"].get("required", [])
    or machine["$defs"].get("idempotency", {}).get("additionalProperties") is not False
):
    fail("Assistant Action effect contract is invalid")
verify_reference_vectors("action-effect-vectors.json", "Action effect", "actions", effect_error)

messages = machine.get("properties", {}).get("messages", {})
pack = json.loads((HERE / "language-pack.schema.json").read_bytes())
copy_reference = result.get("$defs", {}).get("copyReference", {})
if (
    "messages" not in machine.get("required", [])
    or messages.get("maxItems") != MAX_MESSAGES
    or machine["$defs"].get("message", {}).get("required") != ["id", "msgid", "max_length", "params"]
    or machine["$defs"].get("messageParam", {}).get("properties", {}).get("kind", {}).get("enum") != list(PARAM_BOUNDS)
    or pack.get("properties", {}).get("format", {}).get("const") != PACK_FORMAT
    or pack.get("properties", {}).get("locales", {}).get("required") != list(LOCALES)
    or copy_reference.get("required") != ["message", "params"]
    or "publicText" in result.get("$defs", {})
):
    fail("Assistant message catalog contract is invalid")
try:
    verify_catalog_vectors(json.loads((HERE / "catalog-vectors.json").read_bytes()), reference_error)
except KeyError, TypeError, ValueError:
    fail("Assistant message catalog vectors are invalid")

print("Assistant protocol artifacts and conformance vectors are valid")
