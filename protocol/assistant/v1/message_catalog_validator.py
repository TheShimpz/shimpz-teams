"""Reference semantics for Assistant Spec v1 message catalogs, rendering, and language packs."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping

LOCALES = ("ar", "de", "es", "fr", "ja", "pt", "zh")
PACK_FORMAT = "assistant-language-pack-v1"
FIELD_BOUNDS = (80, 120, 160, 500)
SUMMARY_BOUND = 160
MAX_TEMPLATE_CHARS = 500
MAX_MESSAGES = 256
MAX_PARAMS = 8
MAX_CATALOG_BYTES = 131_072
MAX_CATALOG_VALUES = 4_096
MAX_PACK_BYTES = 2_097_152
PARAM_BOUNDS = {"integer": 15, "domain": 253, "dns_name": 253, "identifier": 128}
LIMITS = {
    "messages": MAX_MESSAGES,
    "params": MAX_PARAMS,
    "template_characters": MAX_TEMPLATE_CHARS,
    "catalog_bytes": MAX_CATALOG_BYTES,
    "catalog_values": MAX_CATALOG_VALUES,
    "pack_bytes": MAX_PACK_BYTES,
    "field_bounds": list(FIELD_BOUNDS),
    "summary_bound": SUMMARY_BOUND,
    "param_bounds": PARAM_BOUNDS,
}
MESSAGE_KEYS = frozenset({"id", "msgid", "max_length", "params"})
PACK_KEYS = frozenset({"format", "catalog", "policy", "locales"})
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
PARAM_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
ReferenceCheck = Callable[[object, Mapping[str, dict[str, object]], int], str | None]


def canonical_json(value: object) -> bytes:
    """Return the shared canonical UTF-8 JSON encoding used by fingerprints and digests."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def message_id(msgid: str) -> str:
    """Return the lowercase SHA-256 of an NFC message template's UTF-8 bytes."""
    return hashlib.sha256(msgid.encode()).hexdigest()


def catalog_digest(messages: list[dict[str, object]]) -> str:
    """Return the digest of the canonical sorted catalog."""
    return f"sha256:{hashlib.sha256(canonical_json(messages)).hexdigest()}"


def pack_digest(raw: bytes) -> str:
    """Return the digest of the exact canonical pack bytes."""
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def public_text(value: object, maximum: int) -> bool:
    """Return whether a value is trimmed, printable, NFC, and within its character bound."""
    return (
        isinstance(value, str)
        and value == value.strip()
        and 0 < len(value) <= maximum
        and value.isprintable()
        and unicodedata.is_normalized("NFC", value)
    )


def placeholders(template: str) -> list[str] | None:
    """Return the named fields in order, or None for any other brace syntax."""
    names: list[str] = []
    rest = template
    while "{" in rest or "}" in rest:
        start = rest.find("{")
        end = rest.find("}")
        if start < 0 or end < start or PARAM_NAME.fullmatch(rest[start + 1 : end]) is None:
            return None
        names.append(rest[start + 1 : end])
        rest = rest[end + 1 :]
    return names


def catalog_error(messages: object, summary: object) -> str | None:
    """Return the first catalog error, if any, for a contract whose manifest declares this summary."""
    error = _aggregate_error(messages) or next(
        (error for message in messages if (error := message_error(message)) is not None), None
    )
    if error is not None:
        return error
    ids = [message["id"] for message in messages]
    if ids != sorted(set(ids)):
        return "catalog_order"
    return _summary_error(messages, summary)


def _aggregate_error(messages: object) -> str | None:
    """Return the first whole-catalog shape or aggregate-bound error, if any, before any entry is inspected."""
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        return "catalog_shape"
    if _json_values_exceed(messages, MAX_CATALOG_VALUES):
        return "catalog_bounds"
    try:
        encoded = canonical_json(messages)
    except RecursionError:
        # Within the value bound, only nesting deeper than any admissible message can exhaust the encoder stack.
        return "message_shape"
    except TypeError, ValueError:
        return "catalog_shape"
    return "catalog_bounds" if len(encoded) > MAX_CATALOG_BYTES else None


def message_error(message: object) -> str | None:
    """Return the first error of one catalog entry, if any."""
    if not isinstance(message, dict) or set(message) != MESSAGE_KEYS or not _field_bound(message["max_length"]):
        return "message_shape"
    msgid = message["msgid"]
    if not public_text(msgid, MAX_TEMPLATE_CHARS):
        return "public_text"
    if message["id"] != message_id(msgid):
        return "message_id"
    params = _declarations(message["params"])
    if params is None:
        return "message_params"
    return _template_error(msgid, params, message["max_length"], "message")


def render(reference: Mapping[str, object], template: str) -> str:
    """Insert each already-validated parameter once into the English or translated template."""
    params = reference["params"]
    return PLACEHOLDER.sub(lambda match: str(params[match[1]]), template)


def pack_error(raw: object, messages: list[dict[str, object]]) -> str | None:
    """Return the first error of exact pack bytes against an already-valid catalog, if any."""
    if not isinstance(raw, bytes) or len(raw) > MAX_PACK_BYTES:
        return "pack_bytes"
    pack = _canonical_document(raw)
    if pack is None:
        return "pack_encoding"
    if not _pack_shape(pack):
        return "pack_shape"
    if pack["catalog"] != catalog_digest(messages):
        return "pack_catalog"
    return _translations_error(pack["locales"], messages)


def _canonical_document(raw: bytes) -> object:
    try:
        value = json.loads(raw.decode("utf-8"))
        encoded = canonical_json(value)
    except UnicodeDecodeError, ValueError, TypeError, RecursionError:
        return None
    return value if encoded == raw else None


def _pack_shape(pack: object) -> bool:
    return (
        isinstance(pack, dict)
        and set(pack) == PACK_KEYS
        and pack["format"] == PACK_FORMAT
        and all(isinstance(pack[key], str) and DIGEST.fullmatch(pack[key]) for key in ("catalog", "policy"))
        and isinstance(pack["locales"], dict)
        and set(pack["locales"]) == set(LOCALES)
        and all(isinstance(entries, dict) for entries in pack["locales"].values())
    )


def _translations_error(locales: dict[str, dict[str, object]], messages: list[dict[str, object]]) -> str | None:
    catalog = {message["id"]: message for message in messages}
    for locale in LOCALES:
        entries = locales[locale]
        if set(entries) != set(catalog):
            return "pack_incomplete"
        for identifier, text in entries.items():
            message = catalog[identifier]
            if not public_text(text, MAX_TEMPLATE_CHARS):
                return "public_text"
            params = {item["name"]: item for item in message["params"]}
            error = _template_error(text, params, message["max_length"], "translation")
            if error is not None:
                return error
    return None


def _template_error(template: str, params: Mapping[str, Mapping[str, object]], bound: int, role: str) -> str | None:
    names = placeholders(template)
    if names is None or len(names) != len(set(names)) or set(names) != set(params) or _mark_follows_field(template):
        return f"{role}_placeholders"
    literal = len(template) - sum(len(name) + 2 for name in names)
    return None if literal + sum(params[name]["max_length"] for name in names) <= bound else f"{role}_budget"


def _mark_follows_field(template: str) -> bool:
    """Return whether a combining mark (general category M) directly follows a placeholder.

    Every parameter kind is ASCII-only, and under NFC an ASCII character composes only with a following combining
    mark, so refusing that adjacency keeps every rendering of an NFC template NFC (``{x}`` + U+0301 with ``x="e"``
    would otherwise render a decomposed ``e``). The rule relies on those ASCII-only kinds; a non-ASCII kind needs a
    new rule. Renderings are still checked for NFC after insertion, and parameter values are never normalized.
    """
    following = (template[match.end() : match.end() + 1] for match in PLACEHOLDER.finditer(template))
    return any(character and unicodedata.category(character).startswith("M") for character in following)


def _declarations(value: object) -> dict[str, dict[str, object]] | None:
    if not isinstance(value, list) or len(value) > MAX_PARAMS or not all(_declaration(item) for item in value):
        return None
    names = [item["name"] for item in value]
    return {item["name"]: item for item in value} if names == sorted(set(names)) else None


def _declaration(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"name", "kind", "max_length"}
        and isinstance(value["name"], str)
        and PARAM_NAME.fullmatch(value["name"]) is not None
        and isinstance(value["kind"], str)
        and value["kind"] in PARAM_BOUNDS
        and type(value["max_length"]) is int
        and 1 <= value["max_length"] <= PARAM_BOUNDS[value["kind"]]
    )


def _field_bound(value: object) -> bool:
    return type(value) is int and value in FIELD_BOUNDS


def _summary_error(messages: list[dict[str, object]], summary: object) -> str | None:
    target = next((message for message in messages if message["msgid"] == summary), None)
    if target is None or target["params"] or target["max_length"] > SUMMARY_BOUND:
        return "catalog_summary"
    return None


def _json_values_exceed(value: object, limit: int) -> bool:
    """Return whether a JSON value holds more than ``limit`` values, counted iteratively so depth cannot recurse.

    The value itself, every array element, and every object member value count once. The walk stops as soon as the
    visited and pending values exceed the limit, so it never holds or visits more than ``limit`` values.
    """
    pending = [value]
    visited = 0
    while pending:
        item = pending.pop()
        visited += 1
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        if visited + len(pending) + len(children) > limit:
            return True
        pending.extend(children)
    return False


def verify_vectors(document: object, reference_error: ReferenceCheck) -> None:
    """Fail when a catalog, reference rendering, or pack vector no longer proves its stated outcome.

    ``reference_error`` is the human-request reference check (``human_request_validator.reference_error``), so a
    rendering vector proves only a reference that Team admits for a field with an admitted bound.
    """
    if not isinstance(document, dict) or set(document) != {
        "version",
        "locales",
        "pack_format",
        "limits",
        "catalog",
        "pack",
        "catalog_cases",
        "render_cases",
        "pack_cases",
    }:
        raise ValueError("root")
    if (
        document["version"] != 1
        or document["locales"] != list(LOCALES)
        or document["pack_format"] != PACK_FORMAT
        or document["limits"] != LIMITS
    ):
        raise ValueError("identity")
    messages = _verify_catalog(document["catalog"])
    _verify_pack(document["pack"], messages)
    _verify_outcomes(
        document["catalog_cases"], "catalog", lambda case: catalog_error(_case_messages(case), case["summary"])
    )
    _verify_renders(document["render_cases"], messages, document["pack"]["value"], reference_error)
    _verify_outcomes(document["pack_cases"], "pack", lambda case: pack_error(_case_bytes(case), messages))


def _verify_catalog(section: object) -> list[dict[str, object]]:
    if not isinstance(section, dict) or set(section) != {"summary", "messages", "digest"}:
        raise ValueError("catalog")
    if catalog_error(section["messages"], section["summary"]) is not None:
        raise ValueError("catalog")
    if catalog_digest(section["messages"]) != section["digest"]:
        raise ValueError("catalog_digest")
    return section["messages"]


def _verify_pack(section: object, messages: list[dict[str, object]]) -> None:
    if not isinstance(section, dict) or set(section) != {"value", "digest"}:
        raise ValueError("pack")
    raw = canonical_json(section["value"])
    if pack_error(raw, messages) is not None or pack_digest(raw) != section["digest"]:
        raise ValueError("pack")


def _verify_renders(
    cases: object, messages: list[dict[str, object]], pack: dict[str, object], reference_error: ReferenceCheck
) -> None:
    catalog = {message["id"]: message for message in messages}
    if not isinstance(cases, list) or not cases:
        raise ValueError("render_cases")
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"name", "reference", "bound", "locale", "rendered"}:
            raise ValueError("render_cases")
        reference = case["reference"]
        bound = case["bound"]
        if not _field_bound(bound) or reference_error(reference, catalog, bound) is not None:
            raise ValueError(f"render_cases:{case['name']}")
        identifier = reference["message"]
        locale = case["locale"]
        template = catalog[identifier]["msgid"] if locale == "en" else pack["locales"][locale][identifier]
        rendered = render(reference, template)
        if rendered != case["rendered"] or not public_text(rendered, bound):
            raise ValueError(f"render_cases:{case['name']}")


def generated_catalog(summary: str, generated: Mapping[str, int]) -> list[dict[str, object]]:
    """Expand a bounded generated catalog case: the summary plus ``count - 1`` padded parameterized messages."""
    params = [{"name": f"p{index}", "kind": "integer", "max_length": 1} for index in range(generated["params"])]
    fields = "".join(f" {{p{index}}}" for index in range(generated["params"]))
    padding = f" {'x' * generated['padding']}" if generated["padding"] else ""
    templates = [f"{index:04d}{fields}{padding}" for index in range(generated["count"] - 1)]
    messages = [{"id": message_id(summary), "msgid": summary, "max_length": SUMMARY_BOUND, "params": []}]
    messages += [{"id": message_id(text), "msgid": text, "max_length": 500, "params": params} for text in templates]
    return sorted(messages, key=lambda message: message["id"])


def nested_catalog(summary: str, depth: int) -> list[object]:
    """Expand a nesting catalog case: the summary message plus one entry made of ``depth`` nested arrays."""
    nested: list[object] = []
    for _ in range(depth - 1):
        nested = [nested]
    return [{"id": message_id(summary), "msgid": summary, "max_length": SUMMARY_BOUND, "params": []}, nested]


def _case_messages(case: dict[str, object]) -> object:
    if "messages" in case:
        return case["messages"]
    if "nested" in case:
        return nested_catalog(case["summary"], case["nested"])
    return generated_catalog(case["summary"], case["generated"])


def _case_bytes(case: dict[str, object]) -> bytes:
    return canonical_json(case["pack"]) if "pack" in case else str(case["text"]).encode()


def _verify_outcomes(cases: object, kind: str, evaluate) -> None:
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{kind}_cases")
    names: set[str] = set()
    outcomes: set[bool] = set()
    payloads = {
        "catalog": ({"summary", "messages"}, {"summary", "generated"}, {"summary", "nested"}),
        "pack": ({"pack"}, {"text"}),
    }[kind]
    for case in cases:
        valid = case.get("valid") if isinstance(case, dict) else None
        base = {"name", "valid"} | ({"error"} if valid is False else set())
        if not isinstance(case, dict) or not any(set(case) == base | payload for payload in payloads):
            raise ValueError(f"{kind}_cases")
        name = case["name"]
        if not isinstance(name, str) or not name or name in names or type(valid) is not bool:
            raise ValueError(f"{kind}_cases")
        names.add(name)
        outcomes.add(valid)
        if evaluate(case) != (None if valid else case["error"]):
            raise ValueError(f"{kind}_cases:{name}")
    if outcomes != {False, True}:
        raise ValueError(f"{kind}_cases")
