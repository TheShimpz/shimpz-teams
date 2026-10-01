"""Local Assistant inventory API operations."""

from http import HTTPStatus

from docker.errors import DockerException

from assistant import language as assistant_language
from install import icons
from local.chat.types import ActiveAssistant
from local.errors import ApiProblemError as ApiProblem
from local.labels import ASSISTANT_LABEL
from local.validation import validate_assistant_id, validate_team_id
from protocol.assistant.v1 import message_catalog_validator as catalog_validator
from protocol.http.v1 import payload as http_payload


def assistant_icon(self, team_id: str, assistant_id: str) -> bytes:
    """Return the verified icon bound to one installed Team Assistant."""
    team_id = validate_team_id(team_id)
    with self._lock(team_id):
        binding = self.registry.binding(team_id, assistant_id)
        if binding is None:
            raise ApiProblem(
                HTTPStatus.NOT_FOUND,
                "Assistant is not installed in this Team",
                code="assistant-not-installed",
            )
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
        raise ApiProblem(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "locale must be one interface language",
            code="invalid-locale",
        )
    with self._lock(team_id):
        binding = self.registry.binding(team_id, assistant_id)
        if binding is None:
            raise ApiProblem(
                HTTPStatus.NOT_FOUND,
                "Assistant is not installed in this Team",
                code="assistant-not-installed",
            )
        spec = self.registry.spec(binding)
        if canonical == assistant_language.ENGLISH:
            return {"locale": canonical, "summary": spec.summary}
        container = self.assistant_lifecycle._assistant_container(team_id, assistant_id)
        pack = self.assistant_lifecycle._assistant_language(ActiveAssistant(spec, container.id, container))
        summary = pack.template(catalog_validator.message_id(spec.summary), canonical)
    return {"locale": canonical, "summary": summary}


def list_assistants(self, team_id: str) -> dict[str, list[dict[str, str]]]:
    team_id = validate_team_id(team_id)
    with self._lock(team_id):
        self.assistant_lifecycle._network(team_id)
        output: list[dict[str, str]] = []
        egress_proxy = None

        def current_egress_proxy():
            nonlocal egress_proxy
            if egress_proxy is None:
                egress_proxy = self.assistant_lifecycle._egress_proxy()
            return egress_proxy

        try:
            containers = self.client.containers.list(**self.assistant_lifecycle._assistant_filters(team_id))
        except DockerException as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker is unavailable",
                code="docker-unavailable",
            ) from exc
        bindings_by_id = (
            {binding.assistant_id: binding for binding in self.registry.team_bindings(team_id)} if containers else {}
        )
        for container in containers:
            labels = container.labels or {}
            assistant_id = labels.get(ASSISTANT_LABEL)
            binding = bindings_by_id.get(assistant_id)
            if binding is None:
                raise ApiProblem(
                    HTTPStatus.CONFLICT,
                    "an installed Assistant is no longer allowlisted",
                    code="assistant-registry-drift",
                )
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
        output.sort(key=lambda item: item["assistant"])
        return {"assistants": output}
