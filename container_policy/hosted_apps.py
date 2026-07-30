"""Hosted Team Assistant admission, egress, installation, and inventory."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from http import HTTPStatus
from typing import NoReturn

import docker.errors

import manifests
from assistant_human import (
    assistant_genesis,
    assistant_manifest,
    assistant_registry,
    oauth_account_store,
)
from container_policy import hosted_resources
from container_policy import network as network_policy
from egress import policy as egress_policy
from hosted.install import publication
from http_boundary import runtime_state
from install import bindings as dynamic_assistants


class _IncompleteInstallRollback(runtime_state.ApiError):
    """An install failed while retryable resources still require their durable binding."""


def _egress_store() -> egress_policy.EgressPolicyStore:
    return egress_policy.EgressPolicyStore(
        runtime_state.APP_EGRESS_POLICY_DIR,
        runtime_state.APP_EGRESS_POLICY_GID,
        "localhost,127.0.0.1,::1,postgres,.team",
    )


def _raise_egress_error(exc: egress_policy.EgressPolicyError) -> NoReturn:
    status = (
        HTTPStatus.CONFLICT if isinstance(exc, egress_policy.EgressPolicyDriftError) else HTTPStatus.SERVICE_UNAVAILABLE
    )
    raise runtime_state.ApiError(status, "installed Assistant egress policy failed its contract") from exc


def _require_assistant_genesis(container) -> str:
    """Admit only one immutable, bounded Genesis file and hide package details on failure."""
    try:
        return runtime_state._assistant_genesis_cache.get(container)
    except assistant_genesis.GenesisError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            "installed Assistant Genesis failed its contract",
        ) from exc


def _require_assistant_allowed_hosts(
    spec: assistant_registry.AssistantSpec,
    container,
) -> tuple[str, ...]:
    """Admit the complete security manifest and return its reviewed egress set."""
    try:
        reviewed = assistant_manifest.reviewed_manifest_contract(
            allowed_hosts=spec.allowed_hosts,
            accounts=spec.contract.accounts,
        )
        declared = runtime_state._assistant_allowed_hosts_cache.get(container, reviewed)
        runtime_state._assistant_machine_contract_cache.get(
            container,
            declared.accounts,
            spec.contract.machine_contract,
        )
    except assistant_manifest.ManifestError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            "installed Assistant manifest failed its reviewed contract",
        ) from exc
    return declared.allowed_hosts


def _admit_assistant_contract(
    spec: assistant_registry.AssistantSpec,
    container,
) -> tuple[str, ...]:
    allowed_hosts = _require_assistant_allowed_hosts(spec, container)
    _require_assistant_genesis(container)
    return allowed_hosts


def _team_assistant_containers(team_id: str) -> list:
    """Return only exact Assistant-labeled containers owned by one Team."""
    return runtime_state._docker.containers.list(
        all=True,
        filters={"label": ["team.assistant.runtime", f"team.id={team_id}"]},
    )


def _resolve_team_assistant(
    team_id: str,
    assistant_id: object,
    bindings: dict[str, dynamic_assistants.DynamicAssistantBinding] | None = None,
) -> tuple[str, assistant_registry.AssistantSpec]:
    """Resolve one Assistant exclusively through this Team's durable publication binding."""
    try:
        validated_id = assistant_registry.validate_assistant_id(assistant_id)
        if validated_id in network_policy.RESERVED_SERVICE_ALIASES:
            raise assistant_registry.AssistantSpecError("the Assistant id is reserved for Team infrastructure")
        binding = (
            runtime_state._dynamic_assistants.get(team_id, validated_id)
            if bindings is None
            else bindings.get(validated_id)
        )
        if binding is None:
            raise assistant_registry.AssistantSpecError(f"Assistant {validated_id!r} is not installed in this Team")
        return validated_id, publication.assistant_spec(binding)
    except dynamic_assistants.DynamicAssistantError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant metadata is unavailable",
        ) from exc


def _dynamic_binding_snapshot(
    team_id: str,
    assistant_ids: tuple[str, ...],
) -> dict[str, dynamic_assistants.DynamicAssistantBinding]:
    if not assistant_ids:
        return {}
    try:
        requested = frozenset(assistant_ids)
        return {
            binding.assistant_id: binding
            for binding in runtime_state._dynamic_assistants.list(team_id)
            if binding.assistant_id in requested
        }
    except dynamic_assistants.DynamicAssistantError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant metadata is unavailable",
        ) from exc


def _assistant_egress_token(
    team_id: str,
    assistant_id: str,
    *,
    create: bool = True,
    store: egress_policy.EgressPolicyStore | None = None,
) -> str | None:
    try:
        current_store = store if store is not None else _egress_store()
        return current_store.token(
            manifests.team_assistant_container_name(team_id, assistant_id),
            create=create,
        )
    except egress_policy.EgressPolicyError as exc:
        _raise_egress_error(exc)


def _write_egress_policy(
    token: str,
    allowed_hosts: tuple[str, ...],
    store: egress_policy.EgressPolicyStore | None = None,
) -> None:
    try:
        current_store = store if store is not None else _egress_store()
        current_store.write(token, allowed_hosts)
    except egress_policy.EgressPolicyError as exc:
        _raise_egress_error(exc)


def _validate_egress_policy(
    team_id: str,
    assistant_id: str,
    allowed_hosts: tuple[str, ...],
    store: egress_policy.EgressPolicyStore | None = None,
) -> str:
    try:
        current_store = store if store is not None else _egress_store()
        return current_store.validate(
            manifests.team_assistant_container_name(team_id, assistant_id),
            allowed_hosts,
        )
    except egress_policy.EgressPolicyError as exc:
        _raise_egress_error(exc)


def _validate_admitted_egress(
    team_id: str,
    assistant_id: str,
    allowed_hosts: tuple[str, ...],
    store: egress_policy.EgressPolicyStore | None = None,
) -> str | None:
    if allowed_hosts:
        return _validate_egress_policy(team_id, assistant_id, allowed_hosts, store)
    return None


def _egress_proxy_environment(
    token: str,
    store: egress_policy.EgressPolicyStore | None = None,
) -> dict[str, str]:
    try:
        current_store = store if store is not None else _egress_store()
        return current_store.proxy_environment(token)
    except egress_policy.EgressPolicyError as exc:
        _raise_egress_error(exc)


def _validate_assistant_proxy_environment(
    container,
    token: str | None,
    allowed_hosts: tuple[str, ...],
    store: egress_policy.EgressPolicyStore | None = None,
) -> None:
    config = container.attrs.get("Config")
    raw_environment = config.get("Env") if isinstance(config, dict) else None
    environment = egress_policy.environment_map(raw_environment)
    if environment is None:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            "installed Assistant proxy environment is invalid",
        )
    proxy_environment = {key: value for key, value in environment.items() if key.upper().endswith("_PROXY")}
    if allowed_hosts and token is None:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            "installed Assistant proxy environment failed its contract",
        )
    expected = _egress_proxy_environment(token, store) if token is not None else {}
    if proxy_environment != expected:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            "installed Assistant proxy environment failed its contract",
        )


def _reserve_egress_environment(
    team_id: str,
    assistant_id: str,
    allowed_hosts: tuple[str, ...],
    store: egress_policy.EgressPolicyStore | None = None,
) -> tuple[str | None, dict[str, str]]:
    if not allowed_hosts:
        return None, {}
    current_store = store if store is not None else _egress_store()
    token = _assistant_egress_token(
        team_id,
        assistant_id,
        store=current_store,
    )
    if token is None:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant egress token is unavailable",
        )
    return token, _egress_proxy_environment(token, current_store)


def _activate_admitted_egress(
    network,
    token: str | None,
    allowed_hosts: tuple[str, ...],
    store: egress_policy.EgressPolicyStore | None = None,
) -> None:
    if not allowed_hosts:
        return
    if token is None:
        raise runtime_state.ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "Assistant egress admission failed",
        )
    _write_egress_policy(token, allowed_hosts, store)
    hosted_resources._safe_connect(
        network,
        manifests.APP_EGRESS_CONTAINER,
        aliases=["app-egress-proxy"],
        required=True,
    )


def _remove_egress_policy(team_id: str, assistant_id: str) -> bool:
    try:
        _egress_store().remove(manifests.team_assistant_container_name(team_id, assistant_id))
    except egress_policy.EgressPolicyError:
        return False
    return True


def _wait_assistant_ready(container) -> tuple[bool, str]:
    for attempt in range(runtime_state.HEALTH_RETRIES):
        container.reload()
        if container.status == "running":
            return True, "running"
        if container.status not in {"created", "restarting"}:
            return False, f"container not running (status={container.status})"
        if attempt < runtime_state.HEALTH_RETRIES - 1:
            time.sleep(runtime_state.HEALTH_DELAY_SECONDS)
    return False, "Assistant container never started"


def _assistant_ready_now(container) -> tuple[bool, str]:
    try:
        container.reload()
    except docker.errors.DockerException:
        return False, "container readiness could not be verified"
    if container.status != "running":
        return False, f"container not running (status={container.status})"
    return True, "running"


def _teardown_assistant(
    team_id: str,
    assistant_id: str,
    *,
    container=None,
) -> hosted_resources._CleanupResult:
    """Remove one exact managed Assistant while preserving retry evidence."""
    if container is None:
        try:
            container = hosted_resources._get_container(manifests.team_assistant_container_name(team_id, assistant_id))
        except docker.errors.DockerException:
            return hosted_resources._CleanupResult(False, True)

    if container is not None:
        try:
            container.reload()
        except docker.errors.DockerException:
            return hosted_resources._CleanupResult(False, True)
        if not network_policy.assistant_identity_valid(
            container.attrs,
            team_id,
            assistant_id,
        ):
            return hosted_resources._CleanupResult(False, True)

    policy_removed = _remove_egress_policy(team_id, assistant_id)
    container_removed = container is None
    if container is not None and policy_removed:
        container_id = getattr(container, "id", None)
        container_removed = hosted_resources._remove_team_container(container)
        if container_removed:
            runtime_state._assistant_genesis_cache.discard(container_id)
            runtime_state._assistant_allowed_hosts_cache.discard(container_id)
            runtime_state._assistant_machine_contract_cache.discard(container_id)
    elif container is not None:
        with contextlib.suppress(runtime_state.ApiError):
            hosted_resources._fail_stop_team(container)
    return hosted_resources._CleanupResult(
        policy_removed and container_removed,
        True,
    )


def _retain_admitted_assistant_accounts(
    team_id: str,
    assistant_id: str,
    spec: assistant_registry.AssistantSpec,
) -> None:
    try:
        pruned = runtime_state._assistant_accounts.retain_declared(
            team_id,
            assistant_id,
            tuple(sorted(spec.contract.accounts)),
        )
    except oauth_account_store.OAuthAccountStoreError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant account state is unavailable",
        ) from exc
    if pruned:
        runtime_state._assistant_account_challenges.cancel_team(team_id)


@runtime_state._serialize_against_team_chat
def _install_assistant(
    team_id: str,
    binding: dynamic_assistants.DynamicAssistantBinding,
    owner: str,
    lease: hosted_resources._AuthorizationLease,
    *,
    authorize_start: Callable[[], None],
) -> dict[str, object]:
    """Install one validated publication and atomically retain its Team binding."""
    if team_id != binding.team_id:
        raise runtime_state.ApiError(
            HTTPStatus.NOT_FOUND,
            f"team {team_id!r} not found",
        )
    assistant_id = binding.assistant_id
    with runtime_state._lock_for(team_id):
        try:
            previous = runtime_state._dynamic_assistants.get(team_id, assistant_id)
            retained = runtime_state._dynamic_assistants.put(
                team_id,
                binding.resolution,
            )
        except dynamic_assistants.DynamicAssistantConflictError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.CONFLICT,
                "this Assistant id is already installed from another publication",
            ) from exc
        except dynamic_assistants.DynamicAssistantError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant metadata is unavailable",
            ) from exc
        try:
            result = _install_assistant_locked(
                team_id,
                retained,
                publication.assistant_spec(retained),
                owner,
                lease,
                authorize_start=authorize_start,
            )
        except Exception as exc:
            if previous is None and not isinstance(exc, _IncompleteInstallRollback):
                with contextlib.suppress(dynamic_assistants.DynamicAssistantError):
                    runtime_state._dynamic_assistants.delete(team_id, assistant_id)
            raise
    return {
        **result,
        "source_digest": retained.resolution["source_digest"],
        "oci_digest": retained.resolution["oci_digest"],
        "binding_digest": retained.binding_digest,
    }


def _install_assistant_locked(
    team_id: str,
    binding: dynamic_assistants.DynamicAssistantBinding,
    spec: assistant_registry.AssistantSpec,
    owner: str,
    lease: hosted_resources._AuthorizationLease,
    *,
    authorize_start: Callable[[], None],
) -> dict[str, object]:
    hosted_resources._require_current_authorization(team_id, lease)
    if owner != lease.owner:
        raise runtime_state.ApiError(
            HTTPStatus.NOT_FOUND,
            f"team {team_id!r} not found",
        )
    hosted_resources._prepare_assistant_image(spec)
    existing = hosted_resources._get_container(manifests.team_assistant_container_name(team_id, binding.assistant_id))
    if existing is not None:
        return _admit_existing_assistant(
            team_id,
            binding,
            spec,
            owner,
            existing,
        )
    return _provision_assistant(
        team_id,
        binding,
        spec,
        owner,
        authorize_start=authorize_start,
    )


def _admit_existing_assistant(
    team_id: str,
    binding: dynamic_assistants.DynamicAssistantBinding,
    spec: assistant_registry.AssistantSpec,
    owner: str,
    existing,
) -> dict[str, object]:
    assistant_id = binding.assistant_id
    egress_store = _egress_store()
    try:
        existing.reload()
    except docker.errors.DockerException as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            f"cannot verify installed Assistant {assistant_id!r}",
        ) from exc
    expected_labels = {
        "team.assistant.runtime": "1",
        "team.assistant.dynamic": "1",
        "team.id": team_id,
        "team.assistant": assistant_id,
        "team.owner": owner,
        "team.assistant.source": binding.resolution["source_digest"],
        "team.assistant.image": spec.image,
    }
    if any(str(existing.labels.get(key, "")) != value for key, value in expected_labels.items()):
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            f"existing container for Assistant {assistant_id!r} has invalid ownership metadata",
        )
    hosted_resources._require_team_isolation(existing, workload_spec=spec)
    configured_image = str(existing.attrs.get("Config", {}).get("Image", ""))
    if configured_image != spec.image:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            f"installed Assistant {assistant_id!r} uses a different image",
        )
    admitted_hosts = _admit_assistant_contract(spec, existing)
    token = _validate_admitted_egress(
        team_id,
        assistant_id,
        admitted_hosts,
        egress_store,
    )
    _validate_assistant_proxy_environment(
        existing,
        token,
        admitted_hosts,
        egress_store,
    )
    ready, status = _assistant_ready_now(existing)
    if not ready:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            f"installed Assistant {assistant_id!r} is not ready ({status})",
        )
    _retain_admitted_assistant_accounts(team_id, assistant_id, spec)
    return {
        "team_id": team_id,
        "assistant": assistant_id,
        "status": status,
        "installed": False,
    }


def _provision_assistant(
    team_id: str,
    binding: dynamic_assistants.DynamicAssistantBinding,
    spec: assistant_registry.AssistantSpec,
    owner: str,
    *,
    authorize_start: Callable[[], None],
) -> dict[str, object]:
    if len(_team_assistant_containers(team_id)) >= runtime_state.MAX_ASSISTANTS_PER_TEAM:
        raise runtime_state.ApiError(
            HTTPStatus.TOO_MANY_REQUESTS,
            f"Assistant limit reached for {team_id!r} ({runtime_state.MAX_ASSISTANTS_PER_TEAM})",
        )
    assistant_id = binding.assistant_id
    key = f"assistant:{team_id}:{assistant_id}"
    with hosted_resources._reserve_capacity(
        key,
        owner,
        manifests.ASSISTANT_MEM_LIMIT_BYTES,
        team_slot=False,
    ):
        committed_status = _provision_assistant_transaction(
            team_id,
            binding,
            spec,
            owner,
            authorize_start=authorize_start,
        )
    return {
        "team_id": team_id,
        "assistant": assistant_id,
        "status": committed_status,
        "installed": True,
    }


def _provision_assistant_transaction(
    team_id: str,
    binding: dynamic_assistants.DynamicAssistantBinding,
    spec: assistant_registry.AssistantSpec,
    owner: str,
    *,
    authorize_start: Callable[[], None],
) -> str:
    assistant_id = binding.assistant_id
    egress_store = _egress_store()
    try:
        network = hosted_resources._ensure_team_network(team_id)
        token, proxy_env = _reserve_egress_environment(
            team_id,
            assistant_id,
            spec.allowed_hosts,
            egress_store,
        )
        kwargs = manifests.build_assistant_kwargs(
            team_id,
            assistant_id,
            spec,
            proxy_env=proxy_env,
            owner=owner,
            source_digest=binding.resolution["source_digest"],
        )
        hosted_resources._require_team_runtime()
        container = runtime_state._docker.containers.create(**kwargs)
        network.disconnect(container)
        network.connect(
            container,
            aliases=[assistant_id, f"{assistant_id}.team"],
        )
        admitted_hosts = _admit_assistant_contract(spec, container)
        _validate_assistant_proxy_environment(
            container,
            token,
            admitted_hosts,
            egress_store,
        )
        _activate_admitted_egress(
            network,
            token,
            admitted_hosts,
            egress_store,
        )
        authorize_start()
        hosted_resources._start_team_with_isolation(
            container,
            workload_spec=spec,
        )
        healthy, reason = _wait_assistant_ready(container)
        if not healthy:
            raise runtime_state.ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Assistant {assistant_id!r} failed readiness ({reason}; rolled back)",
            )
        hosted_resources._require_team_isolation(
            container,
            workload_spec=spec,
        )
        ready, committed_status = _assistant_ready_now(container)
        if not ready:
            raise runtime_state.ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                f"Assistant {assistant_id!r} lost readiness before install commit ({committed_status}; rolled back)",
            )
        _retain_admitted_assistant_accounts(team_id, assistant_id, spec)
    except Exception as exc:
        cleanup = _teardown_assistant(team_id, assistant_id)
        if not cleanup.complete:
            raise _IncompleteInstallRollback(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Assistant install failed and rollback is incomplete",
            ) from exc
        if isinstance(exc, runtime_state.ApiError):
            raise
        raise runtime_state.ApiError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "Assistant install failed and was rolled back",
        ) from exc
    return committed_status


@runtime_state._serialize_against_team_chat
def _uninstall_assistant(
    team_id: str,
    assistant_id: str,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    with runtime_state._lock_for(team_id):
        hosted_resources._require_current_authorization(
            team_id,
            lease,
            require_isolation=False,
        )
        runtime_state._assistant_account_challenges.cancel_team(team_id)
        cleanup = _teardown_assistant(team_id, assistant_id)
        if not cleanup.complete:
            raise runtime_state.ApiError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Assistant teardown is incomplete",
            )
        try:
            runtime_state._assistant_accounts.delete_assistant(
                team_id,
                assistant_id,
            )
            runtime_state._dynamic_assistants.delete(team_id, assistant_id)
        except oauth_account_store.OAuthAccountStoreError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant account state is unavailable",
            ) from exc
        except dynamic_assistants.DynamicAssistantError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant metadata could not be removed",
            ) from exc
        return {
            "team_id": team_id,
            "assistant": assistant_id,
            "uninstalled": True,
        }


def _list_assistants(
    team_id: str,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    with runtime_state._lock_for(team_id):
        hosted_resources._require_current_authorization(
            team_id,
            lease,
            require_isolation=False,
        )
        containers = _team_assistant_containers(team_id)
        ids = tuple(
            assistant_id
            for container in containers
            if isinstance(
                assistant_id := (container.labels or {}).get("team.assistant"),
                str,
            )
        )
        bindings = _dynamic_binding_snapshot(team_id, ids)
        assistants = [
            {
                "assistant": assistant_id,
                "status": container.status,
                "container": container.name,
                "powers": sorted(spec.contract.powers),
            }
            for container in containers
            if isinstance(
                assistant_id := (container.labels or {}).get("team.assistant"),
                str,
            )
            for _resolved_id, spec in [_resolve_team_assistant(team_id, assistant_id, bindings)]
        ]
        return {"team_id": team_id, "assistants": assistants}
