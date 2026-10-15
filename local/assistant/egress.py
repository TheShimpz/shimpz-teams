"""Local Team resource naming, network validation, and each Assistant's egress policy.

The policy is Team's own route for the Assistant's provider calls: the token Team presents to the Assistant egress
proxy and the reviewed hosts the proxy admits for it. No Assistant workload holds it (ADR-0106).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from typing import NoReturn

from docker.errors import DockerException, NotFound

from egress import policy as egress_policy
from local import labels as local_labels
from local.errors import ApiProblemError as ApiProblem
from local.errors import docker_unavailable, ownership_conflict
from local.install.runtime import AssistantSpec
from local.labels import TEAM_NAME_LABEL
from local.validation import space_prefix as _space_prefix
from local.validation import validate_team_name

ASSISTANT_EGRESS_POLICY_GID = 10017
ASSISTANT_EGRESS_POLICY_DIR = Path("/var/lib/shimpz-local/assistant-egress")


def _egress_store() -> egress_policy.EgressPolicyStore:
    return egress_policy.EgressPolicyStore(ASSISTANT_EGRESS_POLICY_DIR, ASSISTANT_EGRESS_POLICY_GID)


def _raise_egress_problem(exc: egress_policy.EgressPolicyError) -> NoReturn:
    if isinstance(exc, egress_policy.EgressPolicyDriftError):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant egress policy failed its ownership contract",
            code="egress-policy-drift",
        ) from exc
    raise ApiProblem(
        HTTPStatus.SERVICE_UNAVAILABLE,
        "Assistant egress policy storage is unavailable",
        code="egress-policy-unavailable",
    ) from exc


def _base_labels(self, team_id: str, kind: str) -> dict[str, str]:
    return local_labels.ownership_labels(self.space_id, kind, team_id)


def _network_name(self, team_id: str) -> str:
    return f"shimpz-local-{_space_prefix(self.space_id)}-team-{team_id}"


def _container_name(self, team_id: str, assistant_id: str) -> str:
    return f"shimpz-local-{_space_prefix(self.space_id)}-{team_id}-assistant-{assistant_id}"


def _egress_policy_identity(self, team_id: str, assistant_id: str) -> str:
    return f"{self.space_id}\0{team_id}\0{assistant_id}"


@contextmanager
def _egress_problems() -> Iterator[None]:
    """Report an egress policy store failure as its public problem."""
    try:
        yield
    except egress_policy.EgressPolicyError as exc:
        _raise_egress_problem(exc)


def _egress_token(self, team_id: str, assistant_id: str, *, create: bool) -> str | None:
    with _egress_problems():
        return _egress_store().token(self._egress_policy_identity(team_id, assistant_id), create=create)


def _write_egress_policy(self, team_id: str, spec: AssistantSpec, allowed_hosts: tuple[str, ...]) -> None:
    with _egress_problems():
        store = _egress_store()
        token = store.token(self._egress_policy_identity(team_id, spec.assistant_id), create=True)
        if token is None:
            raise egress_policy.EgressPolicyUnavailableError("egress token was not created")
        store.write(token, allowed_hosts)


def _validate_egress_policy(self, team_id: str, spec: AssistantSpec, allowed_hosts: tuple[str, ...]) -> None:
    admitted = self._read_admitted_egress_policy(team_id, spec.assistant_id)
    with _egress_problems():
        _egress_store().validate_admitted(admitted, allowed_hosts)


def _read_admitted_egress_policy(self, team_id: str, assistant_id: str) -> tuple[str, tuple[str, ...]] | None:
    """Read only a canonical policy previously admitted and owned by this controller."""
    with _egress_problems():
        return _egress_store().admitted(self._egress_policy_identity(team_id, assistant_id))


def _remove_egress_policy(self, team_id: str, assistant_id: str) -> None:
    with _egress_problems():
        _egress_store().remove(self._egress_policy_identity(team_id, assistant_id))


def _managed_team_networks(self) -> list:
    labels = local_labels.label_filter(local_labels.ownership_labels(self.space_id, "team"))
    try:
        return self.client.networks.list(filters={"label": labels})
    except DockerException as exc:
        raise docker_unavailable() from exc


def _validate_network(self, network, team_id: str, *, refresh: bool = True) -> str:
    if refresh:
        network.reload()
    attrs = network.attrs
    expected = self._base_labels(team_id, "team")
    labels = attrs.get("Labels") or {}
    if (
        not self._labels_include(labels, expected)
        or attrs.get("Name") != self._network_name(team_id)
        or attrs.get("Driver") != "bridge"
        or attrs.get("Internal") is not True
        or attrs.get("Attachable") is not False
    ):
        raise ownership_conflict()
    try:
        return validate_team_name(labels.get(TEAM_NAME_LABEL))
    except ApiProblem as exc:
        raise ownership_conflict() from exc


def _network(self, team_id: str, *, required: bool = True):
    try:
        network = self.client.networks.get(self._network_name(team_id))
    except NotFound:
        if required:
            raise ApiProblem(HTTPStatus.NOT_FOUND, "Team not found", code="team-not-found") from None
        return None
    self._validate_network(network, team_id, refresh=False)
    return network
