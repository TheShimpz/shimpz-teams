"""Local Team resource naming, network validation, and each Assistant's egress policy.

The policy is Team's own route for the Assistant's provider calls: the token Team presents to the Assistant egress
proxy and the reviewed hosts the proxy admits for it. No Assistant workload holds it (ADR-0106).
"""

import os
from http import HTTPStatus
from pathlib import Path
from typing import NoReturn

from docker.errors import DockerException, NotFound

from egress import policy as egress_policy
from local.errors import ApiProblemError as ApiProblem
from local.errors import docker_unavailable, ownership_conflict
from local.install.runtime import AssistantSpec
from local.labels import (
    KIND_LABEL,
    MANAGED_LABEL,
    PROFILE_LABEL,
    SPACE_LABEL,
    TEAM_LABEL,
    TEAM_NAME_LABEL,
)
from local.validation import space_prefix as _space_prefix
from local.validation import validate_team_name

PROFILE = "local-v1"
ASSISTANT_EGRESS_POLICY_GID = 10017
ASSISTANT_EGRESS_POLICY_DIR = Path(
    os.environ.get(
        "SHIMPZ_ASSISTANT_EGRESS_POLICY_DIR",
        "/var/lib/shimpz-local/assistant-egress",
    )
)


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
    return {
        MANAGED_LABEL: "1",
        PROFILE_LABEL: PROFILE,
        SPACE_LABEL: self.space_id,
        KIND_LABEL: kind,
        TEAM_LABEL: team_id,
    }


def _network_name(self, team_id: str) -> str:
    return f"shimpz-local-{_space_prefix(self.space_id)}-team-{team_id}"


def _container_name(self, team_id: str, assistant_id: str) -> str:
    return f"shimpz-local-{_space_prefix(self.space_id)}-{team_id}-assistant-{assistant_id}"


def _egress_policy_identity(self, team_id: str, assistant_id: str) -> str:
    return f"{self.space_id}\0{team_id}\0{assistant_id}"


def _egress_token(
    self,
    team_id: str,
    assistant_id: str,
    *,
    create: bool,
    store: egress_policy.EgressPolicyStore | None = None,
) -> str | None:
    try:
        current_store = store if store is not None else _egress_store()
        return current_store.token(
            self._egress_policy_identity(team_id, assistant_id),
            create=create,
        )
    except egress_policy.EgressPolicyError as exc:
        _raise_egress_problem(exc)


def _write_egress_policy(
    self,
    team_id: str,
    spec: AssistantSpec,
    allowed_hosts: tuple[str, ...],
    store: egress_policy.EgressPolicyStore | None = None,
) -> None:
    try:
        current_store = store if store is not None else _egress_store()
        token = current_store.token(
            self._egress_policy_identity(team_id, spec.assistant_id),
            create=True,
        )
        if token is None:
            raise egress_policy.EgressPolicyUnavailableError("egress token was not created")
        current_store.write(token, allowed_hosts)
    except egress_policy.EgressPolicyError as exc:
        _raise_egress_problem(exc)


def _validate_egress_policy(
    self,
    team_id: str,
    spec: AssistantSpec,
    allowed_hosts: tuple[str, ...],
    store: egress_policy.EgressPolicyStore | None = None,
) -> None:
    try:
        current_store = store if store is not None else _egress_store()
        admitted = self._read_admitted_egress_policy(team_id, spec.assistant_id, current_store)
        current_store.validate_admitted(admitted, allowed_hosts)
    except egress_policy.EgressPolicyError as exc:
        _raise_egress_problem(exc)


def _read_admitted_egress_policy(
    self,
    team_id: str,
    assistant_id: str,
    store: egress_policy.EgressPolicyStore | None = None,
) -> tuple[str, tuple[str, ...]] | None:
    """Read only a canonical policy previously admitted and owned by this controller."""
    try:
        current_store = store if store is not None else _egress_store()
        return current_store.admitted(
            self._egress_policy_identity(team_id, assistant_id),
        )
    except egress_policy.EgressPolicyError as exc:
        _raise_egress_problem(exc)


def _remove_egress_policy(
    self,
    team_id: str,
    assistant_id: str,
    store: egress_policy.EgressPolicyStore | None = None,
) -> None:
    try:
        current_store = store if store is not None else _egress_store()
        current_store.remove(
            self._egress_policy_identity(team_id, assistant_id),
        )
    except egress_policy.EgressPolicyError as exc:
        _raise_egress_problem(exc)


def _managed_team_networks(self) -> list:
    labels = [
        f"{MANAGED_LABEL}=1",
        f"{PROFILE_LABEL}={PROFILE}",
        f"{SPACE_LABEL}={self.space_id}",
        f"{KIND_LABEL}=team",
    ]
    try:
        return self.client.networks.list(filters={"label": labels})
    except DockerException as exc:
        raise docker_unavailable() from exc


def _labels_include(actual: object, expected: dict[str, str]) -> bool:
    return isinstance(actual, dict) and all(actual.get(key) == value for key, value in expected.items())


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
