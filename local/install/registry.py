"""Team-scoped durable publication bindings for the Local profile."""

from __future__ import annotations

from assistant import manifest as assistant_manifest
from assistant import spec as assistant_registry
from install import bindings
from local.install.runtime import AssistantSpec


def is_successor(
    current: bindings.DynamicAssistantBinding,
    candidate: bindings.DynamicAssistantBinding,
) -> bool:
    if current.assistant_id != candidate.assistant_id:
        return False
    return _version(candidate.resolution) > _version(current.resolution)


class PublicationRegistry:
    def __init__(self, store: bindings.DynamicAssistantStore) -> None:
        self._store = store

    def put(self, team_id: str, resolution: dict[str, object]) -> AssistantSpec:
        return _spec(self._store.put(team_id, resolution))

    def get(self, team_id: str, assistant_id: str) -> AssistantSpec | None:
        binding = self._store.get(team_id, assistant_id)
        return None if binding is None else _spec(binding)

    def binding(self, team_id: str, assistant_id: str) -> bindings.DynamicAssistantBinding | None:
        return self._store.get(team_id, assistant_id)

    def get_versioned(self, team_id: str, assistant_id: str) -> tuple[AssistantSpec, str] | None:
        binding = self._store.get(team_id, assistant_id)
        if binding is None:
            return None
        value = binding.resolution.get("assistant_version")
        if not isinstance(value, str):
            raise bindings.DynamicAssistantError("publication has no valid Assistant version")
        return _spec(binding), value

    def replacement(
        self,
        team_id: str,
        expected_binding_digest: str,
        resolution: dict[str, object],
    ) -> tuple[bindings.DynamicAssistantBinding, AssistantSpec]:
        binding = bindings.binding_from_resolution(team_id, resolution)
        current = self._store.get(team_id, binding.assistant_id)
        if current is None or current.binding_digest != expected_binding_digest:
            raise bindings.DynamicAssistantConflictError("the Assistant binding changed before replacement")
        return binding, _spec(binding)

    def commit_replacement(
        self,
        team_id: str,
        expected_binding_digest: str,
        resolution: dict[str, object],
    ) -> AssistantSpec:
        return _spec(self._store.replace(team_id, expected_binding_digest, resolution))

    @staticmethod
    def spec(binding: bindings.DynamicAssistantBinding) -> AssistantSpec:
        return _spec(binding)

    def list(self, team_id: str) -> tuple[AssistantSpec, ...]:
        return tuple(_spec(binding) for binding in self._store.list(team_id))

    def delete(self, team_id: str, assistant_id: str) -> bool:
        return self._store.delete(team_id, assistant_id)

    def identities(self) -> set[tuple[str, str]]:
        return {(binding.team_id, binding.assistant_id) for binding in self._store.snapshot()}

    def bindings(self) -> tuple[bindings.DynamicAssistantBinding, ...]:
        return self._store.snapshot()

    def all(self) -> tuple[AssistantSpec, ...]:
        unique: dict[tuple[str, str], AssistantSpec] = {}
        for binding in self._store.snapshot():
            spec = _spec(binding)
            unique[(spec.assistant_id, spec.image)] = spec
        return tuple(sorted(unique.values(), key=lambda spec: (spec.assistant_id, spec.image)))

    def catalog(self) -> tuple[AssistantSpec, ...]:
        unique: dict[str, bindings.DynamicAssistantBinding] = {}
        for binding in self._store.snapshot():
            current = unique.get(binding.assistant_id)
            if current is None or _catalog_order(binding) > _catalog_order(current):
                unique[binding.assistant_id] = binding
        return tuple(_spec(unique[assistant_id]) for assistant_id in sorted(unique))


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
        stored_input_declarations = assistant_manifest.canonical_stored_input_declarations(
            {
                stored_input["id"]: {
                    "kind": stored_input["kind"],
                    "label": stored_input["label"],
                    "description": stored_input["description"],
                }
                for stored_input in binding.resolution["stored_inputs"]
            }
        )
        machine_contract = assistant_manifest.canonical_machine_contract(
            binding.resolution["machine_contract"],
            declarations,
            stored_input_declarations,
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
        stored_inputs = {
            stored_input.id: assistant_registry.StoredInputSpec(
                kind=stored_input.kind,
                label=stored_input.label,
                description=stored_input.description,
            )
            for stored_input in stored_input_declarations
        }
        reviewed = assistant_manifest.reviewed_manifest_contract(
            allowed_hosts=binding.resolution["allowed_hosts"],
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
        return AssistantSpec(
            assistant_id=binding.assistant_id,
            version=str(binding.resolution["assistant_version"]),
            name=str(binding.resolution["name"]),
            summary=str(binding.resolution["summary"]),
            image=str(binding.resolution["image_reference"]),
            actions=actions,
            allowed_hosts=reviewed.allowed_hosts,
            required_image_labels=(
                ("org.shimpz.assistant.id", binding.assistant_id),
                ("org.shimpz.source.digest", str(binding.resolution["source_digest"])),
            ),
            integrations=integrations,
            stored_inputs=stored_inputs,
            machine_contract=machine_contract,
        )
    except (KeyError, TypeError, assistant_manifest.ManifestError) as exc:
        raise bindings.DynamicAssistantError("publication has no valid Assistant runtime contract") from exc


def _version(resolution: dict[str, object]) -> tuple[int, int, int]:
    value = resolution.get("assistant_version")
    if not isinstance(value, str):
        raise bindings.DynamicAssistantError("publication has no valid Assistant version")
    try:
        major, minor, patch = value.split(".")
        return int(major), int(minor), int(patch)
    except (ValueError, TypeError) as exc:
        raise bindings.DynamicAssistantError("publication has no valid Assistant version") from exc


def _catalog_order(binding: bindings.DynamicAssistantBinding) -> tuple[tuple[int, int, int], str]:
    return _version(binding.resolution), binding.binding_digest
