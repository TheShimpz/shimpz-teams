#!/usr/bin/env python3
"""Validate the published Assistant protocol artifact set."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "contract-files.sha256"
# The root holds the schemas, this verifier, and the README; golden vectors and reference validators each have one
# directory, so a manifest path is a file name or one directory and a file name. Nothing else may exist at any depth,
# so the reference validators are imported only after the whole tree matches its manifest.
DIRECTORIES = ("validators", "vectors")
ROW = re.compile(r"([0-9a-f]{64})  ((?:(?:validators|vectors)/)?[A-Za-z0-9._-]+)")
SCHEMAS = (
    "fetch.schema.json",
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


def verify_reference_vectors(
    filename: str, label: str, field: str | tuple[str, ...], error: Callable[..., str | None]
) -> None:
    """Require named positive and negative cases whose verdicts the reference validator reproduces."""
    fields = (field,) if isinstance(field, str) else field
    vectors = json.loads((HERE / filename).read_bytes())
    cases = vectors.get("cases") if isinstance(vectors, dict) else None
    if not isinstance(vectors, dict) or vectors.get("version") != 1 or not isinstance(cases, list) or not cases:
        fail(f"Assistant {label} vectors have an invalid root")
    names: set[str] = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or set(case) != {"name", *fields, "valid"}
            or not isinstance(case["name"], str)
            or not case["name"]
            or case["name"] in names
            or not isinstance(case["valid"], bool)
        ):
            fail(f"Assistant {label} vector case is invalid")
        names.add(case["name"])
        if (error(*(case[name] for name in fields)) is None) != case["valid"]:
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


def layout_files() -> set[str]:
    """Return every file below the version root, refusing any directory or link outside the layout."""
    found = set()
    for parent, directories, files in os.walk(HERE):
        relative = Path(parent).relative_to(HERE)
        for name in directories:
            path = Path(parent) / name
            if relative.parts or name not in DIRECTORIES or path.is_symlink():
                fail(f"Assistant protocol layout has an unexpected directory: {(relative / name).as_posix()}")
        for name in files:
            path = Path(parent) / name
            if path.is_symlink() or not path.is_file():
                fail(f"Assistant protocol layout has an unexpected entry: {(relative / name).as_posix()}")
            found.add((relative / name).as_posix())
    return found - {MANIFEST.name}


def regular_bytes(path: Path) -> bytes:
    """Read one regular file without following a link or blocking on a FIFO or device that took its place."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            fail(f"Assistant protocol layout has an unexpected entry: {path.relative_to(HERE).as_posix()}")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        fail(f"Assistant protocol artifact is unreadable: {path.relative_to(HERE).as_posix()}")
    # The descriptor is checked again, so a FIFO or device swapped in after lstat is refused before any read.
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            fail(f"Assistant protocol layout has an unexpected entry: {path.relative_to(HERE).as_posix()}")
        return handle.read()


try:
    manifest_lines = regular_bytes(MANIFEST).decode("ascii").splitlines()
except UnicodeDecodeError:
    fail("Assistant protocol checksum manifest is invalid")
rows: dict[str, str] = {}
for line in manifest_lines:
    match = ROW.fullmatch(line)
    if match is None or match[2] in rows:
        fail("Assistant protocol checksum manifest is invalid")
    rows[match[2]] = match[1]

if set(rows) != layout_files():
    fail("Assistant protocol artifact set differs from its checksum manifest")
for filename, expected in rows.items():
    digest = hashlib.sha256(regular_bytes(HERE / filename)).hexdigest()
    if digest != expected:
        fail(f"{filename} SHA-256 is {digest}, expected {expected}")

# Import the reference validators only now that their bytes are verified, and never leave bytecode in the tree.
sys.dont_write_bytecode = True
action_effect = importlib.import_module("validators.action_effect")
failure = importlib.import_module("validators.failure")
human_request = importlib.import_module("validators.human_request")
input_file = importlib.import_module("validators.input_file")
message_catalog = importlib.import_module("validators.message_catalog")
EFFECTS, effect_error = action_effect.EFFECTS, action_effect.effect_error
FAILURE_KEYS, failure_error = failure.FAILURE_KEYS, failure.failure_error
reference_error, verify_human_vectors = human_request.reference_error, human_request.verify_vectors
FILE_ID_SCHEMA = input_file.FILE_ID_SCHEMA
MAX_BASE64_CHARACTERS, MAX_FILE_BYTES = input_file.MAX_BASE64_CHARACTERS, input_file.MAX_FILE_BYTES
MAX_INPUT_FILES = input_file.MAX_INPUT_FILES
input_files_error, invocation_files_error = input_file.input_files_error, input_file.invocation_files_error
LOCALES, MAX_MESSAGES, PACK_FORMAT = message_catalog.LOCALES, message_catalog.MAX_MESSAGES, message_catalog.PACK_FORMAT
PARAM_BOUNDS, catalog_error = message_catalog.PARAM_BOUNDS, message_catalog.catalog_error
verify_catalog_vectors = message_catalog.verify_vectors

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
    or stored_input.get("properties", {}).get("description", {}).get("maxLength") != 400
    or stored_input.get("required") != ["kind", "label", "description", "help_url", "host"]
    or stored_input.get("oneOf") != [{"required": ["header"]}, {"required": ["query"], "not": {"required": ["scheme"]}}]
    or manifest_schema.get("$defs", {}).get("helpUrl", {}).get("maxLength") != 2048
):
    fail("Assistant Stored Input manifest contract is invalid")

invocation = json.loads((HERE / "invocation.schema.json").read_bytes())
held = invocation.get("properties", {}).get("stored_inputs", {})
if (
    "stored_inputs" not in invocation.get("required", [])
    or held.get("type") != "array"
    or held.get("maxItems") != 8
    or held.get("uniqueItems") is not True
    or "integrations" in invocation.get("properties", {})
):
    fail("Assistant Stored Input invocation contract is invalid")

fetch = json.loads((HERE / "fetch.schema.json").read_bytes())
fetch_error = fetch.get("$defs", {}).get("response", {}).get("oneOf", [{}, {}])[-1]
if (
    fetch.get("$ref") != "#/$defs/request"
    or fetch.get("$defs", {}).get("request", {}).get("required") != ["type", "method", "url", "headers"]
    or fetch.get("$defs", {}).get("request", {}).get("additionalProperties") is not False
    or set(fetch.get("$defs", {}).get("request", {}).get("properties", {}))
    != {"type", "method", "url", "headers", "body", "timeout_ms"}
    or fetch_error.get("properties", {}).get("error", {}).get("enum")
    != ["refused", "credential-missing", "unavailable", "failed"]
):
    fail("Assistant provider call contract is invalid")
verify_verdict_vectors("vectors/fetch.json", "provider call", {"frame": dict})
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
verify_reference_vectors("vectors/failure.json", "failure", "response", failure_error)


verify_verdict_vectors("vectors/manifest.json", "manifest", {"manifest": str})
verify_verdict_vectors("vectors/action-schema.json", "Action schema", {"schema": dict})
expansions = {
    (case["valid"], expanded_subschemas(case["schema"]))
    for case in json.loads((HERE / "vectors/action-schema.json").read_bytes())["cases"]
}
if not {(True, MAX_EXPANDED_SUBSCHEMAS), (False, MAX_EXPANDED_SUBSCHEMAS + 1)} <= expansions or any(
    valid and (size is None or size > MAX_EXPANDED_SUBSCHEMAS) for valid, size in expansions
):
    fail("Assistant Action schema vectors do not pin the expanded-reference bound")
verify_verdict_vectors("vectors/pattern.json", "pattern", {"pattern": str, "subject": str}, "matches")
verify_verdict_vectors("vectors/invocation.json", "invocation", {"invocation": dict})

human = json.loads((HERE / "vectors/human-request.json").read_bytes())
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
    or stored_inputs.get("maxItems") != 8
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
verify_reference_vectors("vectors/action-effect.json", "Action effect", "actions", effect_error)

file_schema = invocation.get("$defs", {}).get("file", {})
file_content = file_schema.get("properties", {}).get("content", {}).get("oneOf", [{}, {}])
if (
    "files" not in invocation.get("required", [])
    or invocation.get("properties", {}).get("files", {}).get("maxProperties") != MAX_INPUT_FILES
    or invocation.get("$defs", {}).get("fileId", {}).get("pattern") != FILE_ID_SCHEMA["pattern"]
    or file_schema.get("additionalProperties") is not False
    or file_schema.get("properties", {}).get("size", {}).get("maximum") != MAX_FILE_BYTES
    or file_content[-1].get("properties", {}).get("base64", {}).get("maxLength") != MAX_BASE64_CHARACTERS
    or "input_files" not in machine["$defs"]["action"].get("required", [])
    or action_properties.get("input_files", {}).get("maxItems") != MAX_INPUT_FILES
):
    fail("Assistant Action file input contract is invalid")
verify_reference_vectors("vectors/input-file.json", "Action file input", "actions", input_files_error)
verify_reference_vectors(
    "vectors/file-invocation.json", "file invocation", ("action", "invocation"), invocation_files_error
)

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
    verify_catalog_vectors(json.loads((HERE / "vectors/catalog.json").read_bytes()), reference_error)
except KeyError, TypeError, ValueError:
    fail("Assistant message catalog vectors are invalid")

print("Assistant protocol artifacts and conformance vectors are valid")
