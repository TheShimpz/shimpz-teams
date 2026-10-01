"""Catalog-referenced Action human requests, their copy, and their packs for Team tests (ADR-0091).

Tests write request copy as plain English; ``refs`` turns every copy string into a reference to a catalog message
with exactly that template, registering the message so ``CATALOG`` admits it.
"""

from __future__ import annotations

import copy as _copy

from action import challenges as action_challenges
from action import human as action_human
from assistant import language as assistant_language
from protocol.assistant.v1 import message_catalog_validator as catalog_validator
from tests import catalog_fixtures

COPY_FIELDS = ("title", "description", "label", "placeholder")
OPTION_FIELDS = ("label", "description")
TITLE = "Continue safely"
DESCRIPTION = "Provide the reviewed value before the Action continues."
# Every message a test referenced, by id; validate_request reads only the ids a request references.
CATALOG: dict[str, dict[str, object]] = catalog_fixtures.by_id(catalog_fixtures.messages())


# Copy that Controller-driven tests use through the Local and Hosted harnesses, whose reviewed binding carries a fixed
# catalog and pack: a harness request may reference only these messages and the default templates.
HARNESS_COPY = (
    "Allow listing the zones.",
    "Allow this Action to list the reviewed Cloudflare zones.",
    "API key",
    "Apply all.",
    "Change DNS",
    "Confirm",
    "Confirm action",
    "Confirm current identity before continuing.",
    "Confirm identity",
    "Confirm identity.",
    "Confirm protected action",
    "Confirm this reviewed action.",
    "Continue",
    "Continue the reviewed Action operation.",
    "Continue the reviewed operation.",
    "Continue this Action.",
    "Enter the password.",
    "Enter the reviewed zone.",
    "Enter the third-party provider secret.",
    "Exa API key",
    "List zones",
    "Paste the Exa API key once.",
    "Prepare",
    "Prepare the reviewed action.",
    "Provide the key once.",
    "Provider secret",
    "Publish this reviewed DNS zone.",
    "Publish zone",
    "Replace the record.",
    "Secret",
    "Sign in",
    "Token",
    "Use an enrolled second factor.",
    "Value",
    "Zone",
    "example.com",
)


def message_for(text: str) -> dict[str, object]:
    """One parameterless message bounded by the smallest field bound its text fits, so any wider field may use it."""
    return catalog_fixtures.message(text, next(bound for bound in catalog_validator.FIELD_BOUNDS if len(text) <= bound))


def _reference(value: object) -> object:
    if not isinstance(value, str):
        return value
    entry = message_for(value)
    CATALOG.setdefault(entry["id"], entry)
    return {"message": entry["id"], "params": {}}


def harness_messages(summary: str) -> list[dict[str, object]]:
    """The fixed catalog of a harness binding: its summary, the default templates, and every harness copy."""
    return catalog_fixtures.messages(
        summary, message_for(TITLE), message_for(DESCRIPTION), *(message_for(text) for text in HARNESS_COPY)
    )


def refs(descriptor: dict[str, object]) -> dict[str, object]:
    """A copy of the request whose string copy fields are references to registered catalog messages."""
    value = _copy.deepcopy(descriptor)
    for field in COPY_FIELDS:
        if field in value:
            value[field] = _reference(value[field])
    for option in value.get("options", ()) if isinstance(value.get("options"), list) else ():
        if isinstance(option, dict):
            for field in OPTION_FIELDS:
                if field in option:
                    option[field] = _reference(option[field])
    return value


def descriptor(kind: str, ordinal: int = 0, **fields: object) -> dict[str, object]:
    """One fingerprinted canonical request with default English title and description copy."""
    value = refs({"kind": kind, "ordinal": ordinal, "title": TITLE, "description": DESCRIPTION, **fields})
    value["fingerprint"] = action_human._fingerprint(value)
    return value


def fingerprinted(value: dict[str, object]) -> dict[str, object]:
    """Reference the copy of a request written without a fingerprint and add its fingerprint."""
    referenced = refs({key: item for key, item in value.items() if key != "fingerprint"})
    referenced["fingerprint"] = action_human._fingerprint(referenced)
    return referenced


def admit(
    value: dict[str, object],
    capabilities: tuple[str, ...] | None = None,
    stored_inputs: tuple[str, ...] = (),
) -> action_human.HumanRequest:
    return action_human.validate_request(
        value,
        capabilities if capabilities is not None else (str(value["kind"]),),
        stored_inputs,
        catalog=CATALOG,
    )


def request(kind: str, ordinal: int = 0, *, stored_inputs: tuple[str, ...] = (), **fields: object):
    """One admitted request of this kind with default copy."""
    return admit(descriptor(kind, ordinal, **fields), stored_inputs=stored_inputs)


def pack_for(messages: list[dict[str, object]]) -> assistant_language.LanguagePack:
    """The verified pack of exactly these catalog entries."""
    raw = catalog_fixtures.pack_bytes(messages)
    return assistant_language.admit_pack(raw, messages, catalog_validator.pack_digest(raw))


def harness_pack(summary: str) -> assistant_language.LanguagePack:
    return pack_for(harness_messages(summary))


def copy(request_value: action_human.HumanRequest, locale: str = "en") -> action_challenges.RequestCopy:
    """The request's copy rendered in one interface language from a pack of its own messages."""
    return action_challenges.render_copy(request_value, pack_for(request_value.messages()), locale)


IDENTITY = {
    "assistant_id": "shimpz-cloudflare",
    "assistant_name": "Shimpz Cloudflare",
    "action_id": "list-zones",
    "action_summary": "List zones",
    "interrupt_id": "interrupt-1",
    "assistant_version": "1.0.0",
}


def requirement(
    request_value: action_human.HumanRequest,
    *,
    locale: str = "en",
    **fields: object,
) -> action_challenges.HumanRequirement:
    """A requirement rendered in one locale; ``fields`` override the identity or add presentation."""
    identity = {key: fields.pop(key, value) for key, value in IDENTITY.items()}
    return action_challenges.HumanRequirement(
        request=request_value,
        copy=copy(request_value, locale),
        **identity,
        **fields,
    )
