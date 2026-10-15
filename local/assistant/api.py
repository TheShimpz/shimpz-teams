"""Local Assistant inventory API operations."""

from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus

from assistant import details as assistant_page
from assistant import language as assistant_language
from assistant import manifest as assistant_manifest
from install import icons
from local.assistant import resources as local_assistant_resources
from local.chat.types import ActiveAssistant
from local.errors import ApiProblemError as ApiProblem
from local.errors import (
    assistant_manifest_invalid,
    assistant_not_installed,
    invalid_locale,
)
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


@contextmanager
def _installed_copy(
    self, team_id: str, assistant_id: str, locale: object
) -> Iterator[tuple[str, object, object, object]]:
    """Hold one Team's lock over one installed Assistant's copy in one closed interface language.

    It yields the canonical language, the binding, its spec, and, for any language but English, the language pack read
    from the binding's container and verified against its digest; English is the binding's own catalog copy.
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
        spec = self.registry.spec(binding)
        pack = None
        if canonical != assistant_language.ENGLISH:
            container = self.assistant_lifecycle._assistant_container(team_id, assistant_id)
            pack = self.assistant_lifecycle._assistant_language(ActiveAssistant(spec, container.id, container))
        yield canonical, binding, spec, pack


def assistant_summary(self, team_id: str, assistant_id: str, locale: object) -> dict[str, object]:
    """One installed Assistant's summary in one closed interface language, read from its binding's pack (ADR-0091).

    English is the binding's catalog summary itself; any other language is only that message's translation from the
    pack verified against the binding's digest, so a missing or mismatched pack fails closed instead of answering in
    English.
    """
    with _installed_copy(self, team_id, assistant_id, locale) as (canonical, _binding, spec, pack):
        if pack is None:
            return {"locale": canonical, "summary": spec.summary}
        summary = pack.template(catalog_validator.message_id(spec.summary), canonical)
    return {"locale": canonical, "summary": summary}


def assistant_details(self, team_id: str, assistant_id: str, locale: object) -> dict[str, object]:
    """One installed Assistant's page in one closed interface language, from its exact current binding.

    Like the summary, English is the binding's own catalog copy and any other language only its translations from the
    pack verified against the binding's digest; Team validates the closed answer before it leaves.
    """
    with _installed_copy(self, team_id, assistant_id, locale) as (canonical, binding, spec, pack):
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
            stored_inputs=dict(spec.stored_inputs),
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

        filters = self.assistant_lifecycle._assistant_filters(team_id)
        for container, binding in local_assistant_resources.bound_containers(
            self.client, self.registry, filters, team_id
        ):
            spec, version = self.registry.versioned(binding)
            config, environment = self.assistant_lifecycle._validate_container_profile(
                container,
                team_id,
                spec,
                self.assistant_lifecycle._network_name(team_id),
            )
            invalid = False
            try:
                self.assistant_lifecycle._validate_container_egress_environment(team_id, spec, environment)
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
                    "assistant": binding.assistant_id,
                    "assistant_version": version,
                    "status": status,
                    "provenance": spec.provenance,
                }
            )
        output.sort(key=lambda item: item["assistant"])
        return {"assistants": output}
