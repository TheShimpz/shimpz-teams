"""Reference semantics for Assistant Spec v1 human-request vectors."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

BASE = {"kind", "ordinal", "title", "description"}
LENGTH_KINDS = {
    "input:text": 4096,
    "input:textarea": 16000,
    "input:password": 1024,
    "input:phone": 64,
}
CHOICE_KINDS = {"input:select", "input:choice"}
AUTH_KINDS = {"auth:password", "auth:totp", "auth:passkey"}
AUTHORIZATION_KINDS = {"approval", *AUTH_KINDS}
COPY_BOUNDS = {
    "title": 80,
    "description": 500,
    "label": 80,
    "placeholder": 120,
    "option_label": 80,
    "option_description": 160,
}
Catalog = Mapping[str, dict[str, object]]
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# An exact DNS record name: lowercase ASCII labels of [a-z0-9_-] that neither start nor end with a hyphen, one or more,
# dot-separated, with no wildcard label and no trailing dot. The declared bound (at most 253) caps the whole name.
DNS_NAME = re.compile(r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?(?:\.[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?)*")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
PATTERNS = {"domain": DOMAIN, "dns_name": DNS_NAME, "identifier": IDENTIFIER}


def fingerprint(request: object) -> str:
    """Return the canonical request preimage's lowercase SHA-256."""
    encoded = json.dumps(
        request,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def request_error(request: object, catalog: Catalog) -> str | None:
    """Return the first semantic request error against the reviewed message catalog, if any."""
    base_error = _request_base_error(request, catalog)
    if base_error is not None or not isinstance(request, dict):
        return base_error
    kind = request["kind"]
    if kind == "approval" or kind in AUTH_KINDS:
        error = None if set(request) == BASE else "request_shape"
    elif kind in LENGTH_KINDS:
        error = _length_error(request, LENGTH_KINDS[kind], catalog)
    elif kind in CHOICE_KINDS:
        error = _choice_error(request, catalog, multiple=False)
    elif kind == "input:choices":
        error = _choice_error(request, catalog, multiple=True)
    else:
        error = "request_kind"
    return error


def reference_error(reference: object, catalog: Catalog, bound: int) -> str | None:
    """Return the first error of one copy reference used by a field with this character bound."""
    if not isinstance(reference, dict) or set(reference) != {"message", "params"}:
        return "copy_reference"
    identifier = reference["message"]
    message = catalog.get(identifier) if isinstance(identifier, str) else None
    if message is None or not isinstance(reference["params"], dict):
        return "copy_reference"
    if message["max_length"] > bound:
        return "copy_bound"
    declared = {item["name"]: item for item in message["params"]}
    values = reference["params"]
    if set(values) != set(declared) or not all(param_value(declared[name], values[name]) for name in declared):
        return "copy_params"
    return None


def param_value(declaration: Mapping[str, object], value: object) -> bool:
    """Return whether one parameter value satisfies its declared kind and maximum length."""
    maximum = declaration["max_length"]
    if declaration["kind"] == "integer":
        # Compare numerically: a decimal conversion of a huge integer raises ValueError instead of refusing it.
        return type(value) is int and 0 <= value < 10**maximum
    if not isinstance(value, str) or len(value) > maximum:
        return False
    return PATTERNS[declaration["kind"]].fullmatch(value) is not None


def transcript_error(requests: object, responses: object, catalog: Catalog) -> str | None:
    """Return the first deterministic replay transcript error, if any."""
    prefix_error = _transcript_prefix_error(requests, responses, catalog)
    if prefix_error is not None or not isinstance(requests, list) or not isinstance(responses, list):
        return prefix_error
    password_positions = [index for index, item in enumerate(requests) if item["kind"] == "input:password"]
    if sum(item["kind"] in AUTHORIZATION_KINDS for item in requests) > 1:
        return "authorization_once"
    if password_positions and password_positions != [len(requests) - 1]:
        return "secret_last"
    if len(responses) != len(requests):
        return "response_count"
    for request, response in zip(requests, responses, strict=True):
        error = _response_error(request, response)
        if error is not None:
            return error
    return None


def _request_base_error(request: object, catalog: Catalog) -> str | None:
    if not isinstance(request, dict) or not set(request) >= BASE:
        return "request_shape"
    ordinal = request.get("ordinal")
    if not isinstance(request.get("kind"), str) or type(ordinal) is not int or not 0 <= ordinal <= 7:
        return "request_shape"
    return _copy_error(request, catalog, {"title": False, "description": False})


def _copy_error(value: Mapping[str, object], catalog: Catalog, fields: dict[str, bool]) -> str | None:
    """Return the first error of the named copy references; a nullable field also admits null."""
    for field, nullable in fields.items():
        reference = value[field.removeprefix("option_")]
        if reference is None and nullable:
            continue
        error = reference_error(reference, catalog, COPY_BOUNDS[field])
        if error is not None:
            return error
    return None


def _transcript_prefix_error(requests: object, responses: object, catalog: Catalog) -> str | None:
    if not isinstance(requests, list) or not isinstance(responses, list) or len(requests) > 8:
        return "transcript_shape"
    ordinals = [item.get("ordinal") if isinstance(item, dict) else None for item in requests]
    if ordinals != list(range(len(requests))):
        return "ordinal_sequence"
    return next((error for item in requests if (error := request_error(item, catalog)) is not None), None)


def verify_vectors(document: object, capabilities: list[object]) -> None:
    """Fail when a human-request vector no longer proves its stated outcome.

    The caller validates the vector catalog with the message catalog reference before relying on this check.
    """
    if not isinstance(document, dict) or set(document) != {
        "version",
        "capabilities",
        "limits",
        "catalog",
        "fingerprint",
        "request_cases",
        "transcript_cases",
    }:
        raise ValueError("root")
    if document["version"] != 1 or document["capabilities"] != capabilities:
        raise ValueError("identity")
    if document["limits"] != {"requests_per_action": 8, "options": 32, "copy_characters": COPY_BOUNDS}:
        raise ValueError("limits")
    section = document["catalog"]
    if not isinstance(section, dict) or set(section) != {"summary", "messages"}:
        raise ValueError("catalog")
    catalog = {message["id"]: message for message in section["messages"]}
    _verify_fingerprints(document["fingerprint"])
    _verify_cases(document["request_cases"], "request", catalog)
    _verify_cases(document["transcript_cases"], "transcript", catalog)


def _verify_fingerprints(section: object) -> None:
    if not isinstance(section, dict) or set(section) != {"algorithm", "serialization", "cases"}:
        raise ValueError("fingerprint")
    if section["algorithm"] != "sha256" or section["serialization"] != "utf8-json-sort-keys-compact-no-ascii-escaping":
        raise ValueError("fingerprint")
    cases = section["cases"]
    if not isinstance(cases, list) or len(cases) < 2:
        raise ValueError("fingerprint")
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"name", "request", "sha256"}:
            raise ValueError("fingerprint")
        if fingerprint(case["request"]) != case["sha256"]:
            raise ValueError("fingerprint")


def _verify_cases(cases: object, kind: str, catalog: Catalog) -> None:
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{kind}_cases")
    names: set[str] = set()
    outcomes: set[bool] = set()
    for case in cases:
        valid = case.get("valid") if isinstance(case, dict) else None
        payload_keys = {"request"} if kind == "request" else {"requests", "responses"}
        expected_keys = {"name", "valid"} | payload_keys
        if valid is False:
            expected_keys.add("error")
        if not isinstance(case, dict) or set(case) != expected_keys:
            raise ValueError(f"{kind}_cases")
        name = case["name"]
        if not isinstance(name, str) or not name or name in names or type(valid) is not bool:
            raise ValueError(f"{kind}_cases")
        names.add(name)
        outcomes.add(valid)
        actual = (
            request_error(case["request"], catalog)
            if kind == "request"
            else transcript_error(case["requests"], case["responses"], catalog)
        )
        expected = None if valid else case["error"]
        if actual != expected:
            raise ValueError(f"{kind}_cases:{name}")
    if outcomes != {False, True}:
        raise ValueError(f"{kind}_cases")


def _length_error(request: dict[str, object], limit: int, catalog: Catalog) -> str | None:
    expected = BASE | {"label", "required", "placeholder", "min_length", "max_length"}
    if request.get("kind") == "input:password" and "stored_input" in request:
        expected.add("stored_input")
    if set(request) != expected or type(request["required"]) is not bool:
        return "request_shape"
    if "stored_input" in request and not _identifier(request["stored_input"]):
        return "stored_input"
    copy_error = _copy_error(request, catalog, {"label": False, "placeholder": True})
    if copy_error is not None:
        return copy_error
    minimum = request["min_length"]
    maximum = request["max_length"]
    if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum <= limit:
        return "length_bounds"
    return None


def _choice_error(request: dict[str, object], catalog: Catalog, *, multiple: bool) -> str | None:
    bounds = {"min_selections", "max_selections"} if multiple else set()
    if set(request) != BASE | {"label", "required", "options"} | bounds or type(request["required"]) is not bool:
        return "request_shape"
    options = request["options"]
    if not isinstance(options, list) or not 2 <= len(options) <= 32 or not all(_option(item) for item in options):
        return "options"
    values = [item["value"] for item in options]
    if len(values) != len(set(values)):
        return "options"
    copy_error = _copy_error(request, catalog, {"label": False}) or next(
        (
            error
            for option in options
            if (error := _copy_error(option, catalog, {"option_label": False, "option_description": True}))
        ),
        None,
    )
    if copy_error is not None:
        return copy_error
    if multiple:
        minimum = request["min_selections"]
        maximum = request["max_selections"]
        if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum <= len(options):
            return "selection_bounds"
    return None


def _response_error(request: dict[str, object], response: object) -> str | None:
    if not isinstance(response, dict) or set(response) != {"kind", "ordinal", "fingerprint", "value"}:
        return "response_shape"
    if response["kind"] != request["kind"] or response["ordinal"] != request["ordinal"]:
        return "response_match"
    if response["fingerprint"] != fingerprint(request):
        return "response_match"
    kind = request["kind"]
    value = response["value"]
    if kind == "approval" or kind in AUTH_KINDS:
        error = None if value is True else "response_value"
    elif kind in CHOICE_KINDS:
        allowed = {item["value"] for item in request["options"]}
        error = None if value in allowed or (value == "" and request["required"] is False) else "response_value"
    elif kind == "input:choices":
        error = _choices_response_error(request, value)
    else:
        error = _text_response_error(request, value)
    return error


def _choices_response_error(request: dict[str, object], value: object) -> str | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value) or len(value) != len(set(value)):
        return "response_value"
    allowed = {item["value"] for item in request["options"]}
    within_bounds = request["min_selections"] <= len(value) <= request["max_selections"]
    return None if set(value) <= allowed and within_bounds else "response_value"


def _text_response_error(request: dict[str, object], value: object) -> str | None:
    if not isinstance(value, str):
        return "response_value"
    if request["required"] and not value:
        return "response_value"
    return None if request["min_length"] <= len(value) <= request["max_length"] else "response_value"


def _option(value: object) -> bool:
    return isinstance(value, dict) and set(value) == {"value", "label", "description"} and _text(value["value"], 128)


def _text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and value == value.strip() and 0 < len(value) <= maximum and value.isprintable()


def _identifier(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return False
    return (
        value[0].isascii()
        and value[0].islower()
        and value[-1] != "-"
        and "--" not in value
        and all(
            character.isascii() and (character.islower() or character.isdigit() or character == "-")
            for character in value
        )
    )
