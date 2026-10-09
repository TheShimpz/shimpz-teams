"""Shared reviewed Assistant contract primitives for both Controllers."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from assistant import manifest as assistant_manifest
from protocol.http.v1 import payload as http_payload

ALL_ZERO_SHA256 = "0" * 64
DIGEST_IMAGE_RE = re.compile(r"^[a-z0-9.-]+(?::[0-9]{1,5})?/[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}$")


class AssistantSpecError(RuntimeError):
    """A published Assistant contract is malformed or unavailable."""


@dataclass(frozen=True, slots=True)
class ActionSpec:
    summary: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    integrations: tuple[str, ...] = ()
    stored_inputs: tuple[str, ...] = ()
    human_requests: tuple[str, ...] = ()
    # The input properties that carry one Team file each (ADR-0093); empty for an ordinary Action.
    input_files: tuple[str, ...] = ()
    # The reviewed effect class and optional verifier and idempotency declarations (ADR-0092); absent means mutating.
    effect: str = "mutating"
    verifier: Mapping[str, object] | None = None
    idempotency: Mapping[str, object] | None = None


def action_spec(action: Mapping[str, Any]) -> ActionSpec:
    """One admitted machine-contract Action as both Controllers hold it."""
    return ActionSpec(
        summary=action_summary(action["id"]),
        input_schema=action["input_schema"],
        output_schema=action["output_schema"],
        integrations=tuple(action["integrations"]),
        stored_inputs=tuple(action["stored_inputs"]),
        human_requests=tuple(action["human_requests"]),
        input_files=tuple(action["input_files"]),
        effect=action["effect"],
        verifier=action.get("verifier"),
        idempotency=action.get("idempotency"),
    )


@dataclass(frozen=True, slots=True)
class IntegrationSpec:
    provider: str
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StoredInputSpec:
    kind: str
    label: str
    description: str
    help_url: str | None = None


@dataclass(frozen=True, slots=True)
class AssistantContract:
    name: str
    actions: dict[str, ActionSpec]
    integrations: dict[str, IntegrationSpec] = field(default_factory=dict)
    stored_inputs: dict[str, StoredInputSpec] = field(default_factory=dict)
    machine_contract: dict[str, Any] = field(default_factory=dict)
    # The reviewed binding's language-pack digest; the pack itself is admitted from the verified image (ADR-0091).
    pack_digest: str = ""


@dataclass(frozen=True, slots=True)
class AssistantSpec:
    version: str
    summary: str
    image: str
    allowed_hosts: tuple[str, ...]
    archs: tuple[str, ...]
    required_image_labels: tuple[tuple[str, str], ...]
    contract: AssistantContract


@dataclass(frozen=True, slots=True)
class RuntimeContract:
    """The admitted runtime contract of one Assistant binding document."""

    actions: dict[str, ActionSpec]
    allowed_hosts: tuple[str, ...]
    integrations: dict[str, IntegrationSpec]
    stored_inputs: dict[str, StoredInputSpec]
    machine_contract: dict[str, Any]


def runtime_contract(document: Mapping[str, Any]) -> RuntimeContract:
    """Admit a binding document's declarations and its exactly canonical machine contract.

    Raises KeyError, TypeError, or ManifestError; each profile maps them to its own binding error.
    """
    declarations = tuple(
        assistant_manifest.IntegrationDeclaration(
            id=integration["id"],
            provider=integration["provider"],
            scopes=tuple(integration["scopes"]),
        )
        for integration in document["integrations"]
    )
    stored_input_declarations = assistant_manifest.stored_input_declarations_from_documents(document["stored_inputs"])
    machine_contract = assistant_manifest.canonical_machine_contract(
        document["machine_contract"],
        declarations,
        stored_input_declarations,
        summary=document["summary"],
        allowed_hosts=assistant_manifest.canonical_allowed_hosts(document["allowed_hosts"]),
    )
    if machine_contract != document["machine_contract"]:
        raise assistant_manifest.ManifestError("machine contract is not canonical")
    integrations = {
        integration.id: IntegrationSpec(provider=integration.provider, scopes=integration.scopes)
        for integration in declarations
    }
    stored_inputs = {
        stored_input.id: StoredInputSpec(**stored_input.metadata()) for stored_input in stored_input_declarations
    }
    reviewed = assistant_manifest.reviewed_manifest_contract(
        allowed_hosts=document["allowed_hosts"],
        integrations=integrations,
        stored_inputs=stored_inputs,
    )
    return RuntimeContract(
        actions={action["id"]: action_spec(action) for action in machine_contract["actions"]},
        allowed_hosts=reviewed.allowed_hosts,
        integrations=integrations,
        stored_inputs=stored_inputs,
        machine_contract=machine_contract,
    )


def validate_assistant_id(value: object) -> str:
    assistant_id = http_payload.canonical_assistant_id(value)
    if assistant_id is None:
        raise AssistantSpecError("the Assistant id is invalid")
    return assistant_id


def action_summary(action_id: str) -> str:
    return action_id.replace("-", " ").capitalize()


def validate_action_payload(
    action: ActionSpec,
    direction: str,
    payload: object,
) -> dict[str, object]:
    if direction == "input":
        schema = action.input_schema
    elif direction == "output":
        schema = action.output_schema
    else:
        raise ValueError("unknown Action payload direction")
    return assistant_manifest.validate_schema_payload(
        assistant_manifest.action_schema_validator(dict(schema)),
        payload,
    )


def digest_is_bound(ref: object) -> bool:
    return isinstance(ref, str) and not ref.endswith(f"sha256:{ALL_ZERO_SHA256}")


def is_digest_image(image: object) -> bool:
    """Accept only a complete, non-placeholder registry digest reference."""
    return isinstance(image, str) and DIGEST_IMAGE_RE.fullmatch(image) is not None and digest_is_bound(image)
