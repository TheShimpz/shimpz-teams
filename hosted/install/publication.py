"""Convert a verified publication binding into a Hosted App contract."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from typing import Any

from assistant_human import assistant_manifest, assistant_registry, marketplace
from install import bindings


def _build_app_spec(assistant_id: str, resolution: dict[str, Any]) -> marketplace.AppSpec:
    try:
        declarations = tuple(
            assistant_manifest.AccountDeclaration(
                id=account["id"],
                provider=account["provider"],
                scopes=tuple(account["scopes"]),
            )
            for account in resolution["accounts"]
        )
        machine_contract = assistant_manifest.canonical_machine_contract(
            resolution["machine_contract"],
            declarations,
        )
        if machine_contract != resolution["machine_contract"]:
            raise assistant_manifest.ManifestError("machine contract is not canonical")
        accounts = {
            account.id: assistant_registry.AccountSpec(
                provider=account.provider,
                scopes=account.scopes,
            )
            for account in declarations
        }
        reviewed = assistant_manifest.reviewed_manifest_contract(
            allowed_hosts=resolution["allowed_hosts"],
            accounts=accounts,
        )
        powers = {
            power["id"]: assistant_registry.PowerSpec(
                summary=assistant_registry.power_summary(power["id"]),
                input_schema=power["input_schema"],
                output_schema=power["output_schema"],
                accounts=tuple(power["accounts"]),
            )
            for power in machine_contract["powers"]
        }
        platforms = tuple(platform.removeprefix("linux/") for platform in resolution["platforms"])
    except (KeyError, TypeError, assistant_manifest.ManifestError) as exc:
        raise bindings.DynamicAssistantError("the dynamic Assistant runtime contract is invalid") from exc
    return marketplace.AppSpec(
        image=resolution["image_reference"],
        port=1,
        db=False,
        allowed_hosts=reviewed.allowed_hosts,
        first_party=False,
        archs=platforms,
        required_image_labels=(
            ("org.shimpz.assistant.id", assistant_id),
            ("org.shimpz.source.digest", resolution["source_digest"]),
        ),
        assistant=marketplace.AssistantContract(
            powers=powers,
            accounts=accounts,
            machine_contract=machine_contract,
        ),
    )


@lru_cache(maxsize=4096)
def _cached_app_spec(binding_digest: str, encoded_resolution: bytes) -> marketplace.AppSpec:
    try:
        resolution = json.loads(encoded_resolution)
        assistant_id = resolution["assistant_id"]
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise bindings.DynamicAssistantError("the dynamic Assistant runtime contract is invalid") from exc
    if not isinstance(assistant_id, str):
        raise bindings.DynamicAssistantError("the dynamic Assistant runtime contract is invalid")
    return _build_app_spec(assistant_id, resolution)


def app_spec(binding: bindings.DynamicAssistantBinding) -> marketplace.AppSpec:
    expected = bindings.binding_from_resolution(binding.team_id, binding.resolution)
    if expected.binding_digest != binding.binding_digest:
        raise bindings.DynamicAssistantError("the dynamic Assistant registry binding digest is invalid")
    return deepcopy(
        _cached_app_spec(
            binding.binding_digest,
            json.dumps(
                binding.resolution,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
        )
    )
