"""Convert a verified publication binding into a Hosted Assistant contract."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from typing import Any

from assistant import manifest as assistant_manifest
from assistant import spec as assistant_registry
from install import bindings, icons


def retain_icon(client, store: icons.AssistantIconStore, resolution: dict[str, Any]) -> None:
    """Fetch and retain the exact canonical icon declared by resolution."""
    contents = client.icon(resolution["source_digest"], resolution["icon_digest"])
    store.put(resolution, contents)


def discard_icon(
    store: icons.AssistantIconStore,
    bindings_store: bindings.DynamicAssistantStore,
    source_digest: str,
) -> None:
    """Remove an icon once no installed binding references its publication."""
    store.discard_unreferenced(source_digest, bindings_store.snapshot())


def _build_assistant_spec(assistant_id: str, resolution: dict[str, Any]) -> assistant_registry.AssistantSpec:
    try:
        declarations = tuple(
            assistant_manifest.IntegrationDeclaration(
                id=integration["id"],
                provider=integration["provider"],
                scopes=tuple(integration["scopes"]),
            )
            for integration in resolution["integrations"]
        )
        stored_input_declarations = assistant_manifest.canonical_stored_input_declarations(
            {
                stored_input["id"]: {
                    "kind": stored_input["kind"],
                    "label": stored_input["label"],
                    "description": stored_input["description"],
                }
                for stored_input in resolution["stored_inputs"]
            }
        )
        machine_contract = assistant_manifest.canonical_machine_contract(
            resolution["machine_contract"],
            declarations,
            stored_input_declarations,
        )
        if machine_contract != resolution["machine_contract"]:
            raise assistant_manifest.ManifestError("machine contract is not canonical")
        integrations = {
            integration.id: assistant_registry.IntegrationSpec(
                provider=integration.provider,
                scopes=integration.scopes,
            )
            for integration in declarations
        }
        stored_inputs = {
            stored_input.id: assistant_registry.StoredInputSpec(
                kind=stored_input.kind,
                label=stored_input.label,
                description=stored_input.description,
            )
            for stored_input in stored_input_declarations
        }
        reviewed = assistant_manifest.reviewed_manifest_contract(
            allowed_hosts=resolution["allowed_hosts"],
            integrations=integrations,
            stored_inputs=stored_inputs,
        )
        actions = {
            action["id"]: assistant_registry.ActionSpec(
                summary=assistant_registry.action_summary(action["id"]),
                input_schema=action["input_schema"],
                output_schema=action["output_schema"],
                integrations=tuple(action["integrations"]),
                stored_inputs=tuple(action["stored_inputs"]),
                human_requests=tuple(action["human_requests"]),
            )
            for action in machine_contract["actions"]
        }
        platforms = tuple(platform.removeprefix("linux/") for platform in resolution["platforms"])
    except (KeyError, TypeError, assistant_manifest.ManifestError) as exc:
        raise bindings.DynamicAssistantError("the dynamic Assistant runtime contract is invalid") from exc
    return assistant_registry.AssistantSpec(
        version=resolution["assistant_version"],
        summary=resolution["summary"],
        image=resolution["image_reference"],
        allowed_hosts=reviewed.allowed_hosts,
        archs=platforms,
        required_image_labels=(
            ("org.shimpz.assistant.id", assistant_id),
            ("org.shimpz.source.digest", resolution["source_digest"]),
        ),
        contract=assistant_registry.AssistantContract(
            name=resolution["name"],
            actions=actions,
            integrations=integrations,
            stored_inputs=stored_inputs,
            machine_contract=machine_contract,
        ),
    )


@lru_cache(maxsize=4096)
def _cached_assistant_spec(binding_digest: str, encoded_resolution: bytes) -> assistant_registry.AssistantSpec:
    try:
        resolution = json.loads(encoded_resolution)
        assistant_id = resolution["assistant_id"]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise bindings.DynamicAssistantError("the dynamic Assistant runtime contract is invalid") from exc
    if not isinstance(assistant_id, str):
        raise bindings.DynamicAssistantError("the dynamic Assistant runtime contract is invalid")
    return _build_assistant_spec(assistant_id, resolution)


def assistant_spec(binding: bindings.DynamicAssistantBinding) -> assistant_registry.AssistantSpec:
    expected = bindings.binding_from_resolution(binding.team_id, binding.resolution)
    if expected.binding_digest != binding.binding_digest:
        raise bindings.DynamicAssistantError("the dynamic Assistant registry binding digest is invalid")
    return deepcopy(
        _cached_assistant_spec(
            binding.binding_digest,
            json.dumps(
                binding.resolution,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
        )
    )
