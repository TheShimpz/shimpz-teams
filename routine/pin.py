"""The complete pin of one Action a compiled Routine step runs (ADR-0092 section 3).

A step pins everything that decides what its Action may do and how Team handles it: the immutable image and source
identity, the outbound hosts, the Action's whole reviewed contract (input and output schemas, capabilities, effect,
verifier, and idempotency), the verifier Action recovery would run, the Integration and Stored Input declarations
either uses, the English catalog, and the language pack and locale its requests render in. Team recomputes the pin
before every dispatch and resume; any difference is drift, which holds the Routine until an authenticated update.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from protocol.assistant.v1.validators import message_catalog as catalog_validator
from protocol.http.v1 import payload as http_payload

FORMAT = "shimpz-routine-action-pin-v1"
SCOPE_FORMAT = "shimpz-routine-assistant-pin-v1"
# Every locale's copy is in the pinned pack; this fixed locale only completes an Assistant-wide pin.
SCOPE_LOCALE = "en"


class PinError(ValueError):
    """The Assistant does not declare the pinned Action, or the locale is not an interface language."""


def action_pin(spec: object, action_id: str, locale: str) -> str:
    """The ``sha256:`` pin of one Action of one installed Assistant, rendered in ``locale``."""
    if locale not in http_payload.CHAT_LOCALES:
        raise PinError("Routine locale is invalid")
    actions = {action["id"]: action for action in spec.machine_contract["actions"]}
    action = actions.get(action_id)
    if action is None:
        raise PinError("Routine Action is not declared")
    verifier = action.get("verifier")
    pinned = [action] if verifier is None else [action, actions[verifier["action"]]]
    integrations = sorted({name for item in pinned for name in item["integrations"]})
    stored_inputs = sorted({name for item in pinned for name in item["stored_inputs"]})
    document = {
        "format": FORMAT,
        "assistant": spec.assistant_id,
        "version": spec.version,
        "image": spec.image,
        "image_labels": [list(label) for label in spec.required_image_labels],
        "allowed_hosts": list(spec.allowed_hosts),
        "action": action,
        "verifier": None if verifier is None else pinned[1],
        "integrations": {name: _integration(spec.integrations[name]) for name in integrations},
        "stored_inputs": {name: _stored_input(spec.stored_inputs[name]) for name in stored_inputs},
        "catalog": catalog_validator.catalog_digest(spec.machine_contract["messages"]),
        "pack": spec.pack_digest,
        "locale": locale,
    }
    encoded = json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assistant_pin(spec: object, brain_digest: str) -> str:
    """The ``sha256:`` scope pin of one Assistant: its Brain-visible contract and every Action's complete pin.

    A Routine pins the Assistants it may use with it, so a changed image, output schema, effect, verifier,
    idempotency, capability, catalog, or pack is drift exactly as a changed input schema is.
    """
    actions = sorted(action["id"] for action in spec.machine_contract["actions"])
    document = {
        "format": SCOPE_FORMAT,
        "brain": brain_digest,
        "actions": {action_id: action_pin(spec, action_id, SCOPE_LOCALE) for action_id in actions},
    }
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _integration(declaration: object) -> Mapping[str, object]:
    return {"provider": declaration.provider, "scopes": list(declaration.scopes)}


def _stored_input(declaration: object) -> Mapping[str, object]:
    return {
        "kind": declaration.kind,
        "label": declaration.label,
        "description": declaration.description,
        "help_url": declaration.help_url,
    }
