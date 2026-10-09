"""Local Assistant inventory API operations."""

from http import HTTPStatus

from docker.errors import DockerException

from assistant import details as assistant_page
from assistant import language as assistant_language
from assistant import manifest as assistant_manifest
from install import icons
from local.chat.types import ActiveAssistant
from local.errors import ApiProblemError as ApiProblem
from local.errors import (
    assistant_manifest_invalid,
    assistant_not_installed,
    assistant_registry_drift,
    docker_unavailable,
    invalid_locale,
)
from local.labels import ASSISTANT_LABEL
from local.validation import validate_assistant_id, validate_team_id
from protocol.assistant.v1.validators import message_catalog as catalog_validator
from protocol.http.v1 import payload as http_payload


def assistant_icon(self, team_id: str, assistant_id: str) -> bytes:
    """Return the verified icon bound to one installed Team Assistant."""
    team_id = validate_team_id(team_id)
    with self._lock(team_id):
        binding = self.registry.binding(team_id, assistant_id)
        if binding is None:
            raise assistant_not_installed()
        try:
            return self.assistant_icons.read_binding(binding)
        except icons.AssistantIconError as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant icon is unavailable",
                code="assistant-icon-unavailable",
            ) from exc


def assistant_summary(self, team_id: str, assistant_id: str, locale: object) -> dict[str, object]:
    """One installed Assistant's summary in one closed interface language, read from its binding's pack (ADR-0091).

    English is the binding's catalog summary itself; any other language is only that message's translation from the
    pack verified against the binding's digest, so a missing or mismatched pack fails closed instead of answering in
    English.
    """
    team_id = validate_team_id(team_id)
    assistant_id = validate_assistant_id(assistant_id)
    canonical = http_payload.canonical_locale(locale)
    if canonical is None:
        raise invalid_locale()
    with self._lock(team_id):
        binding = self.registry.binding(team_id, assistant_id)
        if binding is None:
            raise assistant_not_installed()
        if not binding.admissible:
            raise assistant_manifest_invalid()
        spec = self.registry.spec(binding)
        if canonical == assistant_language.ENGLISH:
            return {"locale": canonical, "summary": spec.summary}
        container = self.assistant_lifecycle._assistant_container(team_id, assistant_id)
        pack = self.assistant_lifecycle._assistant_language(ActiveAssistant(spec, container.id, container))
        summary = pack.template(catalog_validator.message_id(spec.summary), canonical)
    return {"locale": canonical, "summary": summary}


def assistant_details(self, team_id: str, assistant_id: str, locale: object) -> dict[str, object]:
    """One installed Assistant's page in one closed interface language, from its exact current binding.

    Like the summary, English is the binding's own catalog copy and any other language only its translations from the
    pack verified against the binding's digest; Team validates the closed answer before it leaves.
    """
    team_id = validate_team_id(team_id)
    assistant_id = validate_assistant_id(assistant_id)
    canonical = http_payload.canonical_locale(locale)
    if canonical is None:
        raise invalid_locale()
    with self._lock(team_id):
        binding = self.registry.binding(team_id, assistant_id)
        if binding is None:
            raise assistant_not_installed()
        if not binding.admissible:
            raise assistant_manifest_invalid()
        spec = self.registry.spec(binding)
        pack = None
        if canonical != assistant_language.ENGLISH:
            container = self.assistant_lifecycle._assistant_container(team_id, assistant_id)
            pack = self.assistant_lifecycle._assistant_language(ActiveAssistant(spec, container.id, container))
        document = binding.document
        page = assistant_page.AssistantPage(
            assistant_id=spec.assistant_id,
            version=spec.version,
            name=spec.name,
            # A Local record keeps its snapshot's declared Creators; a resolution its published ones.
            creators=tuple(document["declared_creators" if spec.provenance == "local" else "creators"]),
            summary=spec.summary,
            description=spec.description,
            links=document["links"],
            machine_contract=spec.machine_contract,
            integrations={identifier: value.provider for identifier, value in spec.integrations.items()},
            labels={identifier: value.label for identifier, value in spec.stored_inputs.items()},
        )
        try:
            return page.localized(canonical, pack)
        except assistant_manifest.ManifestError as exc:
            raise assistant_manifest_invalid() from exc


def list_assistants(self, team_id: str) -> dict[str, list[dict[str, str]]]:
    team_id = validate_team_id(team_id)
    with self._lock(team_id):
        self.assistant_lifecycle._network(team_id)
        output: list[dict[str, str]] = []
        egress_proxy = None

        def current_egress_proxy():
            nonlocal egress_proxy
            if egress_proxy is None:
                egress_proxy = self.assistant_lifecycle._egress_proxy(self.assistant_lifecycle._network_name(team_id))
            return egress_proxy

        try:
            containers = self.client.containers.list(**self.assistant_lifecycle._assistant_filters(team_id))
        except DockerException as exc:
            raise docker_unavailable() from exc
        # Read even without containers: a binding needing replacement has no runtime but must stay visible.
        admitted, refused = self.registry.installed(team_id)
        bindings_by_id = {binding.assistant_id: binding for binding in admitted}
        for container in containers:
            labels = container.labels or {}
            assistant_id = labels.get(ASSISTANT_LABEL)
            binding = bindings_by_id.get(assistant_id)
            if binding is None and any(item.assistant_id == assistant_id for item in refused):
                # Listed below as needing replacement; its runtime is never validated against a refused contract.
                continue
            if binding is None:
                raise assistant_registry_drift()
            spec, version = self.registry.versioned(binding)
            config, environment = self.assistant_lifecycle._validate_container_profile(
                container,
                team_id,
                spec,
                self.assistant_lifecycle._network_name(team_id),
            )
            invalid = False
            try:
                self.assistant_lifecycle._validate_container_egress(
                    team_id,
                    spec,
                    self.assistant_lifecycle._network_name(team_id),
                    environment,
                    current_egress_proxy,
                )
            except ApiProblem as exc:
                if exc.code != "egress-policy-drift":
                    raise
                invalid = True
            if invalid:
                status = "invalid"
            elif self.assistant_lifecycle._has_current_assistant_artifact(config, spec):
                try:
                    self.assistant_lifecycle._admit_assistant_allowed_hosts(container, spec)
                except ApiProblem as exc:
                    if exc.code != "assistant-manifest-invalid":
                        raise
                    status = "invalid"
                else:
                    status = container.status
            else:
                status = "outdated"
            output.append(
                {
                    "assistant": assistant_id,
                    "assistant_version": version,
                    "status": status,
                    "provenance": spec.provenance,
                }
            )
        output.extend(_needing_replacement(refused))
        output.sort(key=lambda item: item["assistant"])
        return {"assistants": output}


def _needing_replacement(refused) -> list[dict[str, str]]:
    """Every binding the current contract refuses, shown as invalid so its Supervisor replaces or uninstalls it."""
    listed: list[dict[str, str]] = []
    for binding in refused:
        version = binding.document.get("assistant_version")
        if not isinstance(version, str) or assistant_manifest.VERSION_RE.fullmatch(version) is None:
            raise assistant_registry_drift()
        listed.append(
            {
                "assistant": binding.assistant_id,
                "assistant_version": version,
                "status": "invalid",
                "provenance": binding.provenance,
            }
        )
    return listed
