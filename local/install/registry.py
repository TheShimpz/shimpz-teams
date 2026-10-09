"""Team-scoped durable Assistant bindings for the Local profile."""

from dataclasses import replace

from assistant import manifest as assistant_manifest
from assistant import spec as assistant_registry
from install import bindings
from install.bindings import DynamicAssistantBinding, DynamicAssistantStore
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
    def __init__(self, store: DynamicAssistantStore) -> None:
        self._store = store

    def put(self, team_id: str, resolution: dict[str, object]) -> AssistantSpec:
        return _spec(self._store.put(team_id, resolution))

    def put_with_status(
        self,
        team_id: str,
        resolution: dict[str, object],
    ) -> tuple[AssistantSpec, DynamicAssistantBinding, bool]:
        binding, created = self._store.put_with_status(team_id, resolution)
        return _spec(binding), binding, created

    def put_local(self, team_id: str, record: dict[str, object]) -> AssistantSpec:
        return _spec(self._store.put_local(team_id, record))

    def put_local_with_status(
        self,
        team_id: str,
        record: dict[str, object],
    ) -> tuple[AssistantSpec, DynamicAssistantBinding, bool]:
        binding, created = self._store.put_local_with_status(team_id, record)
        return _spec(binding), binding, created

    def get(self, team_id: str, assistant_id: str) -> AssistantSpec | None:
        binding = self._store.get(team_id, assistant_id)
        return None if binding is None else _spec(binding)

    def binding(self, team_id: str, assistant_id: str) -> DynamicAssistantBinding | None:
        binding = self._store.get(team_id, assistant_id)
        return None if binding is None else _admitted(binding)

    @staticmethod
    def versioned(binding: DynamicAssistantBinding) -> tuple[AssistantSpec, str]:
        value = binding.document.get("assistant_version")
        if not isinstance(value, str):
            raise bindings.DynamicAssistantError("Assistant binding has no valid version")
        return _spec(binding), value

    def replacement(
        self,
        team_id: str,
        expected_binding_digest: str,
        resolution: dict[str, object],
    ) -> tuple[DynamicAssistantBinding, AssistantSpec]:
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
    ) -> tuple[DynamicAssistantBinding, AssistantSpec]:
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
    def spec(binding: DynamicAssistantBinding) -> AssistantSpec:
        return _spec(binding)

    def team_bindings(self, team_id: str) -> tuple[DynamicAssistantBinding, ...]:
        """The Team's bindings that pass current store admission, for callers that convert each to its spec.

        Conversion completes admission: a binding whose runtime contract is refused raises there, never runs.
        """
        return tuple(binding for binding in self._store.list(team_id) if binding.admissible)

    def installed(
        self, team_id: str
    ) -> tuple[tuple[DynamicAssistantBinding, ...], tuple[DynamicAssistantBinding, ...]]:
        """One snapshot of the Team's bindings, split into admitted ones and those needing replacement.

        A binding the current contract refuses is intact but never backs a running Assistant (ADR-0033's 2026-10-08
        amendment); its Supervisor sees it so it can be replaced or uninstalled.
        """
        snapshot = tuple(map(_admitted, self._store.list(team_id)))
        return (
            tuple(binding for binding in snapshot if binding.admissible),
            tuple(binding for binding in snapshot if not binding.admissible),
        )

    def delete(self, team_id: str, assistant_id: str) -> bool:
        return self._store.delete(team_id, assistant_id)

    def delete_if_matches(self, team_id: str, assistant_id: str, expected_binding_digest: str) -> bool:
        return self._store.delete_if_matches(team_id, assistant_id, expected_binding_digest)

    def identities(self) -> set[tuple[str, str]]:
        return {(binding.team_id, binding.assistant_id) for binding in self._store.snapshot()}

    def bindings(self) -> tuple[DynamicAssistantBinding, ...]:
        return tuple(map(_admitted, self._store.snapshot()))

    def inadmissible(self) -> tuple[DynamicAssistantBinding, ...]:
        """Every installed binding the current contract refuses; each needs replacement (ADR-0033, 2026-10-08)."""
        return tuple(binding for binding in self.bindings() if not binding.admissible)

    def images(self) -> tuple[str, ...]:
        """Every image reference an installed binding holds, so no bound image is ever collected as unused.

        A binding needing replacement still holds its image until its Supervisor replaces or uninstalls it.
        """
        return tuple(sorted({_bound_image(binding) for binding in self.bindings()}))

    def catalog(self) -> tuple[AssistantSpec, ...]:
        unique: dict[str, bindings.DynamicAssistantBinding] = {}
        for binding in self.bindings():
            if not binding.admissible:
                continue
            current = unique.get(binding.assistant_id)
            if current is None or _catalog_order(binding) > _catalog_order(current):
                unique[binding.assistant_id] = binding
        return tuple(_spec(unique[assistant_id]) for assistant_id in sorted(unique))


def _admitted(binding: bindings.DynamicAssistantBinding) -> bindings.DynamicAssistantBinding:
    """One stored binding as current Local admission sees it: the store's verdict, then its runtime contract.

    A binding whose runtime contract the current Team refuses is refused like any other binding needing replacement,
    so no runtime path ever meets it as admitted (ADR-0033's 2026-10-08 amendment).
    """
    if binding.admissible:
        try:
            _spec(binding)
        except bindings.DynamicAssistantError:
            return replace(binding, admissible=False)
    return binding


def _spec(binding: bindings.DynamicAssistantBinding) -> AssistantSpec:
    binding.require_admissible()
    document = binding.document
    try:
        contract = assistant_registry.runtime_contract(document)
        image, required_labels = _runtime_identity(binding)
        return AssistantSpec(
            assistant_id=binding.assistant_id,
            version=str(document["assistant_version"]),
            name=str(document["name"]),
            summary=str(document["summary"]),
            image=image,
            actions=contract.actions,
            allowed_hosts=contract.allowed_hosts,
            required_image_labels=required_labels,
            integrations=contract.integrations,
            stored_inputs=contract.stored_inputs,
            machine_contract=contract.machine_contract,
            pack_digest=str(document["pack_digest"]),
            provenance=binding.provenance,
            platform=str(document["platform"]) if binding.provenance == "local" else None,
        )
    except (KeyError, TypeError, assistant_manifest.ManifestError) as exc:
        raise bindings.InadmissibleAssistantBindingError("Assistant binding has no valid runtime contract") from exc


def _bound_image(binding: bindings.DynamicAssistantBinding) -> str:
    if binding.admissible:
        return _spec(binding).image
    # The digest-verified document Team wrote names the image; nothing else in it is interpreted.
    image = binding.document.get("image_reference" if binding.provenance == "published" else "image_id")
    if not isinstance(image, str) or not image:
        raise bindings.DynamicAssistantError("Assistant binding holds no image reference")
    return image


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
