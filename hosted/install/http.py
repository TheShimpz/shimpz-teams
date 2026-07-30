"""Signed Developers-to-Team HTTP boundary for publication installation."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

import docker

from core.http import stdlib
from core.http import strict as strict_http
from hosted import audit
from hosted import state as runtime_state
from hosted.assistant import lifecycle as hosted_apps
from hosted.install import developers_client, developers_delegation, publication
from hosted.team import lifecycle as hosted_lifecycle
from hosted.team import resources as hosted_resources
from install import artifact_trust
from install import bindings as dynamic_assistants
from install import contract as install_contract

TEAMS_PATH = "/internal/v1/developers/teams"
INSTALL_PATH = "/internal/v1/developers/install"
_INSTALL_AUTHORIZATION_CLOCK_SKEW_SECONDS = 5
_CONTRACTS = install_contract.ContractValidator()


@dataclass(slots=True)
class RequestIO:
    """Narrow adapter from the stdlib handler into the Developers boundary."""

    headers: object
    path: str
    capture_body: Callable[[str], dict[str, object]]
    read_team_body: Callable[[set[str]], dict[str, object]]
    send_json: Callable[..., None]
    account_id: str | None = None


def is_path(path: str) -> bool:
    return path in {TEAMS_PATH, INSTALL_PATH}


def _install_authorization_matches(receipt: dict, expected: dict, now: int) -> bool:
    return (
        all(receipt.get(key) == value for key, value in expected.items())
        and receipt["issued_at"] <= now + _INSTALL_AUTHORIZATION_CLOCK_SKEW_SECONDS
        and now <= receipt["expires_at"]
    )


def _dependencies():
    dependencies = (
        runtime_state._developers_delegation,
        runtime_state._developers_client,
        runtime_state._artifact_trust,
    )
    if any(dependency is None for dependency in dependencies):
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Developers integration is unavailable",
        )
    return dependencies


def _route_teams(request: RequestIO) -> None:
    try:
        strict_http.reject_body(request.headers)
    except strict_http.HttpContractError as exc:
        raise runtime_state.ApiError(exc.status, exc.message) from exc
    delegation, _client, _trust = _dependencies()
    claims = delegation.verify(request.headers, action="teams:list")
    request.account_id = claims["account_id"]
    listing = hosted_lifecycle._list(owner=request.account_id)
    response = {
        "version": 1,
        "teams": [{"id": team["team_id"], "name": team["team_name"]} for team in listing["teams"]],
    }
    _CONTRACTS.validate("team-list-response.schema.json", response)
    audit.log(
        "developers_teams",
        "teams",
        result="ok",
        principal_id="developers",
        principal_class="machine",
        subject_account_id=request.account_id,
    )
    request.send_json(HTTPStatus.OK, response, no_store=True)


def _install(request: RequestIO) -> None:
    body = request.read_team_body({"version", "team_id", "source_digest", "request_id", "idempotency_key"})
    _CONTRACTS.validate("install-request.schema.json", body)
    delegation, client, trust = _dependencies()
    claims = delegation.verify(
        request.headers,
        action="assistant:install",
        request=body,
    )
    request.account_id = claims["account_id"]
    principal = ("account", request.account_id)
    runtime_state._enforce_rate("install", principal)
    lease = hosted_resources._authorize(body["team_id"], principal)
    resolution = client.resolve(body["source_digest"])
    trust.verify(resolution)
    binding = dynamic_assistants.binding_from_resolution(body["team_id"], resolution)
    # Pull outside the Team lifecycle lock; the install path resolves this same digest
    # again immediately before create.
    hosted_resources._prepare_assistant_image(publication.assistant_spec(binding))

    def authorize_start() -> None:
        authorization = {
            "version": 1,
            "account_id": request.account_id,
            "team_id": body["team_id"],
            "source_digest": body["source_digest"],
            "oci_digest": resolution["oci_digest"],
            "delegation_jti": claims["jti"],
            "request_id": body["request_id"],
            "idempotency_key": body["idempotency_key"],
        }
        receipt = client.authorize_install(authorization)
        expected = {key: value for key, value in authorization.items() if key != "version"}
        if not _install_authorization_matches(receipt, expected, int(time.time())):
            raise developers_client.InstallAuthorizationDeniedError("installation authorization does not match")

    installed = hosted_apps._install_assistant(
        body["team_id"],
        binding,
        request.account_id,
        lease,
        authorize_start=authorize_start,
    )
    response = {
        "version": 1,
        "status": "installed",
        "team_id": body["team_id"],
        "assistant_id": binding.assistant_id,
        "source_digest": installed["source_digest"],
        "oci_digest": installed["oci_digest"],
        "binding_digest": installed["binding_digest"],
    }
    _CONTRACTS.validate("install-response.schema.json", response)
    audit.log(
        "developers_install",
        body["team_id"],
        result="ok",
        principal_id="developers",
        principal_class="machine",
        subject_account_id=request.account_id,
        assistant=binding.assistant_id,
        source_digest=body["source_digest"],
    )
    request.send_json(HTTPStatus.OK, response, no_store=True)


def _dispatch(request: RequestIO, method: str) -> None:
    if method == "GET" and request.path == TEAMS_PATH:
        _route_teams(request)
        return
    if method == "POST" and request.path == INSTALL_PATH:
        request.capture_body("assistant-install")
        _install(request)
        return
    raise runtime_state.ApiError(HTTPStatus.NOT_FOUND, "operation not found")


def _publication_failure(exc: Exception) -> stdlib.HttpFailure | None:
    if isinstance(exc, developers_delegation.DevelopersDelegationError):
        return stdlib.HttpFailure(HTTPStatus.FORBIDDEN, "invalid Developers delegation", type(exc).__name__, "denied")
    if isinstance(exc, developers_client.AssistantNotInstallableError):
        return stdlib.HttpFailure(HTTPStatus.NOT_FOUND, "Assistant is not installable", type(exc).__name__, "denied")
    if isinstance(exc, developers_client.InstallAuthorizationDeniedError):
        return stdlib.HttpFailure(
            HTTPStatus.CONFLICT,
            "Assistant installation is no longer authorized",
            type(exc).__name__,
            "denied",
        )
    if isinstance(exc, developers_client.DevelopersClientError):
        return stdlib.HttpFailure(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Developers is unavailable",
            type(exc).__name__,
            "error",
        )
    if isinstance(exc, artifact_trust.ArtifactTrustError):
        return stdlib.HttpFailure(
            HTTPStatus.CONFLICT,
            "Assistant artifact trust failed",
            type(exc).__name__,
            "denied",
        )
    return None


def _runtime_failure(exc: Exception) -> stdlib.HttpFailure | None:
    if isinstance(exc, runtime_state.ApiError):
        result = "error" if exc.status >= 500 else "denied"
        return stdlib.HttpFailure(exc.status, exc.message, type(exc).__name__, result)
    if isinstance(exc, (docker.errors.DockerException, OSError)):
        return stdlib.HttpFailure(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Controller dependency is unavailable",
            type(exc).__name__,
            "error",
        )
    if isinstance(exc, (install_contract.ContractValidationError, dynamic_assistants.DynamicAssistantError)):
        return stdlib.HttpFailure(
            HTTPStatus.BAD_REQUEST,
            "installation request is invalid",
            type(exc).__name__,
            "denied",
        )
    return None


def _classify(exc: Exception) -> stdlib.HttpFailure | None:
    return _publication_failure(exc) or _runtime_failure(exc)


def _emit_failure(request: RequestIO, failure: stdlib.HttpFailure) -> None:
    principal = (
        {
            "principal_id": "developers",
            "principal_class": "machine",
            "subject_account_id": request.account_id,
        }
        if request.account_id is not None
        else {"principal_id": None, "principal_class": "absent"}
    )
    audit.log(
        "developers_request",
        request.path,
        result=failure.result,
        reason=failure.audit_reason,
        **principal,
    )
    request.send_json(failure.status, {"error": failure.public_message}, no_store=True)


def dispatch(request: RequestIO, method: str) -> None:
    stdlib.dispatch(
        lambda: _dispatch(request, method),
        classify=_classify,
        emit=lambda failure: _emit_failure(request, failure),
        unexpected_message="internal Developers boundary error",
    )
