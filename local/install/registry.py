"""Team-scoped durable publication bindings for the Local profile."""

from __future__ import annotations

from assistant import manifest as assistant_manifest
from assistant import spec as assistant_registry
from install import bindings
from local.install.runtime import AssistantSpec


class PublicationRegistry:
    def __init__(self, store: bindings.DynamicAssistantStore) -> None:
        self._store = store

    def put(self, team_id: str, resolution: dict[str, object]) -> AssistantSpec:
        return _spec(self._store.put(team_id, resolution))

    def get(self, team_id: str, assistant_id: str) -> AssistantSpec | None:
        binding = self._store.get(team_id, assistant_id)
        return None if binding is None else _spec(binding)

    def list(self, team_id: str) -> tuple[AssistantSpec, ...]:
        return tuple(_spec(binding) for binding in self._store.list(team_id))

    def delete(self, team_id: str, assistant_id: str) -> bool:
        return self._store.delete(team_id, assistant_id)

    def identities(self) -> set[tuple[str, str]]:
        return {(binding.team_id, binding.assistant_id) for binding in self._store.snapshot()}

    def all(self) -> tuple[AssistantSpec, ...]:
        unique: dict[tuple[str, str], AssistantSpec] = {}
        for binding in self._store.snapshot():
            spec = _spec(binding)
            unique[(spec.assistant_id, spec.image)] = spec
        return tuple(sorted(unique.values(), key=lambda spec: (spec.assistant_id, spec.image)))


def _spec(binding: bindings.DynamicAssistantBinding) -> AssistantSpec:
    try:
        declarations = tuple(
            assistant_manifest.IntegrationDeclaration(
                id=integration["id"],
                provider=integration["provider"],
                scopes=tuple(integration["scopes"]),
            )
            for integration in binding.resolution["integrations"]
        )
        machine_contract = assistant_manifest.canonical_machine_contract(
            binding.resolution["machine_contract"],
            declarations,
        )
        if machine_contract != binding.resolution["machine_contract"]:
            raise assistant_manifest.ManifestError("machine contract is not canonical")
        integrations = {
            integration.id: assistant_registry.IntegrationSpec(
                provider=integration.provider,
                scopes=integration.scopes,
            )
            for integration in declarations
        }
        reviewed = assistant_manifest.reviewed_manifest_contract(
            allowed_hosts=binding.resolution["allowed_hosts"],
            integrations=integrations,
        )
        powers = {
            power["id"]: assistant_registry.PowerSpec(
                summary=assistant_registry.power_summary(power["id"]),
                input_schema=power["input_schema"],
                output_schema=power["output_schema"],
                integrations=tuple(power["integrations"]),
            )
            for power in machine_contract["powers"]
        }
        return AssistantSpec(
            assistant_id=binding.assistant_id,
            name=str(binding.resolution["name"]),
            summary=str(binding.resolution["name"]),
            image=str(binding.resolution["image_reference"]),
            powers=powers,
            allowed_hosts=reviewed.allowed_hosts,
            required_image_labels=(
                ("org.shimpz.assistant.id", binding.assistant_id),
                ("org.shimpz.source.digest", str(binding.resolution["source_digest"])),
            ),
            integrations=integrations,
            machine_contract=machine_contract,
        )
    except (KeyError, TypeError, assistant_manifest.ManifestError) as exc:
        raise bindings.DynamicAssistantError("publication has no valid Assistant runtime contract") from exc
