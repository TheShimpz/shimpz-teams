"""Convert a verified publication binding into a Hosted Assistant contract."""

import json
from contextlib import AbstractContextManager
from copy import deepcopy
from functools import lru_cache
from typing import Any

from assistant import manifest as assistant_manifest
from assistant import spec as assistant_registry
from install import bindings, icons

# The spec depends only on the canonical resolution, so Teams installing the same release share one entry. The bound
# covers the distinct releases a Space runs at once; a retired release is evicted as the least recently used.
_SPEC_CACHE_ENTRIES = 256


def retained_icon(
    client,
    store: icons.AssistantIconStore,
    bindings_store: bindings.DynamicAssistantStore,
    resolution: dict[str, Any],
) -> AbstractContextManager[None]:
    """Fetch the exact canonical icon declared by resolution and hold it while its binding commits."""
    contents = client.icon(resolution["source_digest"], resolution["icon_digest"])
    return store.retained(resolution, contents, bindings_store.snapshot)


def _build_assistant_spec(assistant_id: str, resolution: dict[str, Any]) -> assistant_registry.AssistantSpec:
    try:
        contract = assistant_registry.runtime_contract(resolution)
        platforms = tuple(platform.removeprefix("linux/") for platform in resolution["platforms"])
    except (KeyError, TypeError, assistant_manifest.ManifestError) as exc:
        raise bindings.DynamicAssistantError("the dynamic Assistant runtime contract is invalid") from exc
    return assistant_registry.AssistantSpec(
        version=resolution["assistant_version"],
        summary=resolution["summary"],
        description=contract.presentation.description,
        image=resolution["image_reference"],
        allowed_hosts=contract.allowed_hosts,
        archs=platforms,
        required_image_labels=(
            ("org.shimpz.assistant.id", assistant_id),
            ("org.shimpz.source.digest", resolution["source_digest"]),
        ),
        contract=assistant_registry.AssistantContract(
            name=resolution["name"],
            actions=contract.actions,
            integrations=contract.integrations,
            stored_inputs=contract.stored_inputs,
            machine_contract=contract.machine_contract,
            pack_digest=resolution["pack_digest"],
        ),
        creators=tuple(resolution["creators"]),
        links=dict(contract.presentation.links),
    )


@lru_cache(maxsize=_SPEC_CACHE_ENTRIES)
def _cached_assistant_spec(encoded_resolution: bytes) -> assistant_registry.AssistantSpec:
    try:
        resolution = json.loads(encoded_resolution)
        assistant_id = resolution["assistant_id"]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise bindings.DynamicAssistantError("the dynamic Assistant runtime contract is invalid") from exc
    if not isinstance(assistant_id, str):
        raise bindings.DynamicAssistantError("the dynamic Assistant runtime contract is invalid")
    return _build_assistant_spec(assistant_id, resolution)


def assistant_spec(binding: bindings.DynamicAssistantBinding) -> assistant_registry.AssistantSpec:
    """The binding's Assistant contract; its Team-bound digest is verified on every call, never cached."""
    expected = bindings.binding_from_resolution(binding.team_id, binding.resolution)
    if expected.binding_digest != binding.binding_digest:
        raise bindings.DynamicAssistantError("the dynamic Assistant registry binding digest is invalid")
    return deepcopy(
        _cached_assistant_spec(
            json.dumps(
                binding.resolution,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
        )
    )
