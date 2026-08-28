"""Team-scoped durable Assistant bindings for the Local profile."""

from __future__ import annotations

from assistant import manifest as assistant_manifest
from assistant import spec as assistant_registry
from install import bindings
from local.install import snapshots
from local.install.runtime import AssistantSpec


def is_successor(
    current: bindings.DynamicAssistantBinding,
    candidate: bindings.DynamicAssistantBinding,
) -> bool:
    if (
        current.provenance != "published"
        or candidate.provenance != "published"
        or current.assistant_id != candidate.assistant_id
    ):
        return False
    return _version(candidate.resolution) > _version(current.resolution)


class AssistantRegistry:
    def __init__(self, store: bindings.DynamicAssistantStore) -> None:
        self._store = store

    def put(self, team_id: str, resolution: dict[str, object]) -> AssistantSpec:
        return _spec(self._store.put(team_id, resolution))

    def put_local(self, team_id: str, record: dict[str, object]) -> AssistantSpec:
        return _spec(self._store.put_local(team_id, record))

    def get(self, team_id: str, assistant_id: str) -> AssistantSpec | None:
        binding = self._store.get(team_id, assistant_id)
        return None if binding is None else _spec(binding)

    def binding(self, team_id: str, assistant_id: str) -> bindings.DynamicAssistantBinding | None:
        return self._store.get(team_id, assistant_id)

    def get_versioned(self, team_id: str, assistant_id: str) -> tuple[AssistantSpec, str] | None:
        binding = self._store.get(team_id, assistant_id)
        if binding is None:
            return None
        value = binding.document.get("assistant_version")
        if not isinstance(value, str):
            raise bindings.DynamicAssistantError("Assistant binding has no valid version")
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
        if current.provenance != "published":
            raise bindings.DynamicAssistantConflictError("the Assistant binding provenance cannot be replaced")
        return binding, _spec(binding)

    def commit_replacement(
        self,
        team_id: str,
        expected_binding_digest: str,
        resolution: dict[str, object],
    ) -> AssistantSpec:
        return _spec(self._store.replace(team_id, expected_binding_digest, resolution))

    def local_replacement(
        self,
        team_id: str,
        expected_binding_digest: str,
        record: dict[str, object],
    ) -> tuple[bindings.DynamicAssistantBinding, AssistantSpec]:
        binding = bindings.binding_from_local_record(team_id, record, snapshots.validate_record)
        current = self._store.get(team_id, binding.assistant_id)
        if current is None or current.binding_digest != expected_binding_digest:
            raise bindings.DynamicAssistantConflictError("the Assistant binding changed before replacement")
        if current.provenance != "local":
            raise bindings.DynamicAssistantConflictError("the Assistant binding provenance cannot be replaced")
        return binding, _spec(binding)

    def commit_local_replacement(
        self,
        team_id: str,
        expected_binding_digest: str,
        record: dict[str, object],
    ) -> AssistantSpec:
        return _spec(self._store.replace_local(team_id, expected_binding_digest, record))

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
    document = binding.document
    try:
        declarations = tuple(
            assistant_manifest.IntegrationDeclaration(
                id=integration["id"],
                provider=integration["provider"],
                scopes=tuple(integration["scopes"]),
            )
            for integration in document["integrations"]
        )
        stored_input_declarations = assistant_manifest.canonical_stored_input_declarations(
            {
                stored_input["id"]: {
                    "kind": stored_input["kind"],
                    "label": stored_input["label"],
                    "description": stored_input["description"],
                }
                for stored_input in document["stored_inputs"]
            }
        )
        machine_contract = assistant_manifest.canonical_machine_contract(
            document["machine_contract"],
            declarations,
            stored_input_declarations,
        )
        if machine_contract != document["machine_contract"]:
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
            allowed_hosts=document["allowed_hosts"],
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
        image, required_labels = _runtime_identity(binding)
        return AssistantSpec(
            assistant_id=binding.assistant_id,
            version=str(document["assistant_version"]),
            name=str(document["name"]),
            summary=str(document["summary"]),
            image=image,
            actions=actions,
            allowed_hosts=reviewed.allowed_hosts,
            required_image_labels=required_labels,
            integrations=integrations,
            stored_inputs=stored_inputs,
            machine_contract=machine_contract,
            provenance=binding.provenance,
            platform=str(document["platform"]) if binding.provenance == "local" else None,
        )
    except (KeyError, TypeError, assistant_manifest.ManifestError) as exc:
        raise bindings.DynamicAssistantError("Assistant binding has no valid runtime contract") from exc


def _runtime_identity(binding: bindings.DynamicAssistantBinding) -> tuple[str, tuple[tuple[str, str], ...]]:
    document = binding.document
    if binding.provenance == "published":
        image = str(document["image_reference"])
        labels = (
            (snapshots.ASSISTANT_LABEL, binding.assistant_id),
            (snapshots.SOURCE_LABEL, str(document["source_digest"])),
        )
    elif binding.provenance == "local":
        snapshots.validate_record(document)
        image = str(document["image_id"])
        labels = (
            (snapshots.LOCAL_STAGE_LABEL, snapshots.LOCAL_STAGE_VALUE),
            (snapshots.ASSISTANT_LABEL, binding.assistant_id),
            (snapshots.SOURCE_LABEL, str(document["source_digest"])),
            (snapshots.VERSION_LABEL, str(document["assistant_version"])),
        )
    else:
        raise bindings.DynamicAssistantError("Assistant binding provenance is invalid")
    return image, labels


def _version(resolution: dict[str, object]) -> tuple[int, int, int]:
    value = resolution.get("assistant_version")
    if not isinstance(value, str):
        raise bindings.DynamicAssistantError("Assistant binding has no valid version")
    try:
        major, minor, patch = value.split(".")
        return int(major), int(minor), int(patch)
    except (ValueError, TypeError) as exc:
        raise bindings.DynamicAssistantError("Assistant binding has no valid version") from exc


def _catalog_order(binding: bindings.DynamicAssistantBinding) -> tuple[tuple[int, int, int], str]:
    return _version(binding.document), binding.binding_digest
