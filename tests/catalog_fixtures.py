"""Message catalogs, references, and language packs for Team tests (ADR-0091)."""

import copy
import hashlib

from protocol.assistant.v1.validators import message_catalog as catalog_validator

SUMMARY = "Exercise one reviewed Assistant."
POLICY = f"sha256:{'7' * 64}"
LOCALES = catalog_validator.LOCALES
# Request copy every human-request fixture may reference: one message per copy field bound, plus parameters.
TITLE = "Approve this Action"
DESCRIPTION = "Review exactly what this Action does before it continues."
LABEL = "Your answer"
PLACEHOLDER = "Type the value"
OPTION = "First option"
OPTION_DESCRIPTION = "Choose this option to continue."
ZONE_TITLE = "Delete the record {record} from {zone}"
REQUEST_TEMPLATES = {
    TITLE: 80,
    DESCRIPTION: 500,
    LABEL: 80,
    PLACEHOLDER: 120,
    OPTION: 80,
    OPTION_DESCRIPTION: 160,
}
ZONE_PARAMS = [
    {"name": "record", "kind": "identifier", "max_length": 16},
    {"name": "zone", "kind": "domain", "max_length": 30},
]


def message(msgid: str, max_length: int = 160, params: list[dict[str, object]] | None = None) -> dict[str, object]:
    """One catalog entry whose id is the SHA-256 of its exact template."""
    return {
        "id": catalog_validator.message_id(msgid),
        "msgid": msgid,
        "max_length": max_length,
        "params": copy.deepcopy(params or []),
    }


def messages(summary: str = SUMMARY, *extra: dict[str, object]) -> list[dict[str, object]]:
    """The summary message, every request template, and any extra messages, sorted by id."""
    entries = {item["id"]: item for item in (message(summary, catalog_validator.SUMMARY_BOUND), *extra)}
    for msgid, bound in REQUEST_TEMPLATES.items():
        entries.setdefault(catalog_validator.message_id(msgid), message(msgid, bound))
    zone = message(ZONE_TITLE, 80, ZONE_PARAMS)
    entries.setdefault(zone["id"], zone)
    return [entries[identifier] for identifier in sorted(entries)]


def with_messages(contract: dict[str, object], summary: str = SUMMARY) -> dict[str, object]:
    """Return a copy of a machine contract that carries the catalog for this summary."""
    return {**copy.deepcopy(contract), "messages": messages(summary)}


def ref(msgid: str, **params: object) -> dict[str, object]:
    """One copy reference to a declared message."""
    return {"message": catalog_validator.message_id(msgid), "params": params}


def by_id(entries: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {item["id"]: item for item in entries}


def translation(locale: str, msgid: str, max_length: int, params: list[dict[str, object]]) -> str:
    """A visibly localized template that keeps every placeholder and still fits its budget."""
    literal = len(msgid) - sum(len(item["name"]) + 2 for item in params)
    prefixed = f"{locale.upper()} {msgid}"
    fits = literal + 3 + sum(int(item["max_length"]) for item in params) <= max_length
    return prefixed if fits else msgid


def pack(entries: list[dict[str, object]], policy: str = POLICY) -> dict[str, object]:
    """A complete canonical pack value for exactly this catalog."""
    return {
        "format": catalog_validator.PACK_FORMAT,
        "catalog": catalog_validator.catalog_digest(entries),
        "policy": policy,
        "locales": {
            locale: {
                item["id"]: translation(locale, item["msgid"], item["max_length"], item["params"]) for item in entries
            }
            for locale in LOCALES
        },
    }


def pack_bytes(entries: list[dict[str, object]], policy: str = POLICY) -> bytes:
    return catalog_validator.canonical_json(pack(entries, policy))


def pack_digest(entries: list[dict[str, object]], policy: str = POLICY) -> str:
    return f"sha256:{hashlib.sha256(pack_bytes(entries, policy)).hexdigest()}"
