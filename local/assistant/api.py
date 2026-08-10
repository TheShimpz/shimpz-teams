"""Local Assistant inventory API operations."""

from http import HTTPStatus

from docker.errors import DockerException

from install import icons
from local.errors import ApiProblemError as ApiProblem
from local.labels import ASSISTANT_LABEL
from local.validation import validate_team_id


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
            return self.assistant_icons.read(binding.resolution)
        except icons.AssistantIconError as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant icon is unavailable",
                code="assistant-icon-unavailable",
            ) from exc


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
        for container in containers:
            labels = container.labels
            assistant_id = labels.get(ASSISTANT_LABEL)
            spec = self.registry.get(team_id, assistant_id)
            version = self.registry.version(team_id, assistant_id)
            if spec is None or version is None:
                raise ApiProblem(
                    HTTPStatus.CONFLICT,
                    "an installed Assistant is no longer allowlisted",
                    code="assistant-registry-drift",
                )
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
                }
            )
        output.sort(key=lambda item: item["assistant"])
        return {"assistants": output}
