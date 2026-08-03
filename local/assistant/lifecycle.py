"""Local Assistant container install, update, and uninstall lifecycle."""

import logging
from collections.abc import Callable
from contextlib import suppress
from http import HTTPStatus

from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import LogConfig, Ulimit

from assistant import manifest as assistant_manifest
from install import bindings
from local.assistant import isolation as local_container_policy
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.errors import ApiProblemError as ApiProblem
from local.install.runtime import AssistantSpec
from local.validation import validate_team_id
from power import execution as power_execution

ASSISTANT_MEMORY = local_container_policy.ASSISTANT_MEMORY
ASSISTANT_NANO_CPUS = local_container_policy.ASSISTANT_NANO_CPUS
ASSISTANT_PIDS = local_container_policy.ASSISTANT_PIDS
ASSISTANT_TMPFS = local_container_policy.ASSISTANT_TMPFS
ASSISTANT_ULIMITS = local_container_policy.ASSISTANT_ULIMITS
log = logging.getLogger("shimpz.team.local.assistant.lifecycle")


def _is_replaceable_readiness_failure(problem: ApiProblem) -> bool:
    return problem.code == "assistant-not-ready"


def _serialize_against_local_team_chat(
    operation: Callable[..., dict[str, object]],
) -> Callable[..., dict[str, object]]:
    """Reject Assistant mutation before its first side effect while a Team turn owns the slot."""

    def guarded(controller, team_id: str, *args, **kwargs) -> dict[str, object]:
        team_id = validate_team_id(team_id)
        lock = controller.chat_turn_service._chat_lock(team_id)
        if not lock.acquire(blocking=False):
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Assistant lifecycle cannot change during an active Team chat turn",
                code="chat-active",
            )
        try:
            return operation(controller, team_id, *args, **kwargs)
        finally:
            lock.release()

    return guarded


def _rollback_assistant_install(
    self,
    team_id: str,
    spec: AssistantSpec,
    network,
    container,
    *,
    egress_prepared: bool,
) -> ApiProblem | None:
    incomplete = False
    if container is not None:
        self._assistant_genesis_cache.discard(container.id)
        self._assistant_allowed_hosts_cache.discard(container.id)
        self._assistant_machine_contract_cache.discard(container.id)
        try:
            container.remove(force=True)
        except NotFound:
            pass
        except DockerException:
            incomplete = True
            with suppress(ApiProblem):
                self._fail_stop_power(container)
    if egress_prepared:
        try:
            self._release_assistant_egress(team_id, spec.assistant_id, network)
        except ApiProblem:
            incomplete = True
    if incomplete:
        return ApiProblem(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "Assistant install rollback is incomplete",
            code="assistant-install-rollback-incomplete",
        )
    return None


def _create_assistant_container(
    self,
    team_id: str,
    spec: AssistantSpec,
    network,
    image,
    *,
    authorize_start: Callable[[], None] | None = None,
) -> None:
    container = None
    egress_prepared = False
    egress_store = None
    try:
        proxy_environment: dict[str, str] = {}
        if spec.allowed_hosts:
            token, proxy_environment, egress_store = self._reserve_assistant_egress_environment(
                team_id,
                spec.assistant_id,
            )
            if token is None:
                raise ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "Assistant egress token could not be saved",
                    code="egress-policy-unavailable",
                )
            egress_prepared = True
        container = self.client.containers.create(
            image=spec.image,
            name=self._container_name(team_id, spec.assistant_id),
            command=None,
            detach=True,
            user=power_execution.ASSISTANT_RPC_USER,
            network=network.name,
            labels=self._assistant_labels(team_id, spec),
            environment={
                "SHIMPZ_ASSISTANT_ID": spec.assistant_id,
                "SHIMPZ_TEAM_ID": team_id,
                "PYTHONDONTWRITEBYTECODE": "1",
                **proxy_environment,
            },
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            privileged=False,
            ipc_mode="private",
            cgroupns="private",
            mem_limit=ASSISTANT_MEMORY,
            memswap_limit=ASSISTANT_MEMORY,
            nano_cpus=ASSISTANT_NANO_CPUS,
            cpuset_cpus=self.cpuset_cpus,
            pids_limit=ASSISTANT_PIDS,
            tmpfs=ASSISTANT_TMPFS,
            ulimits=[
                Ulimit(
                    name="nofile",
                    soft=local_container_policy.ASSISTANT_NOFILE_LIMIT,
                    hard=local_container_policy.ASSISTANT_NOFILE_LIMIT,
                )
            ],
            restart_policy={"Name": "no"},
            log_config=LogConfig(type=LogConfig.types.JSON, config={"max-size": "1m", "max-file": "2"}),
        )
        container.reload()
        if container.attrs.get("Image") != image.id:
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Docker resolved an unexpected Assistant image",
                code="image-resolution-mismatch",
            )
        allowed_hosts = self._admit_assistant_allowed_hosts(container, spec)
        if allowed_hosts:
            self._activate_assistant_egress(team_id, spec, network, allowed_hosts, egress_store)
        if authorize_start is not None:
            authorize_start()
        container.start()
        self._validate_container(container, team_id, spec, network.name)
        self._wait_ready(container, spec)
        self._active_assistant_genesis(_ActiveAssistant(spec, container.id, container))
    except ApiProblem as exc:
        cleanup_error = self._rollback_assistant_install(
            team_id,
            spec,
            network,
            container,
            egress_prepared=egress_prepared,
        )
        if cleanup_error is not None:
            raise cleanup_error from exc
        raise
    except DockerException as exc:
        cleanup_error = self._rollback_assistant_install(
            team_id,
            spec,
            network,
            container,
            egress_prepared=egress_prepared,
        )
        if cleanup_error is not None:
            raise cleanup_error from exc
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Docker could not install the Assistant",
            code="docker-install-failed",
        ) from exc


def _replace_unready_assistant(
    self,
    team_id: str,
    spec: AssistantSpec,
    network,
    existing,
    *,
    authorize_start: Callable[[], None] | None = None,
) -> None:
    # The reference Assistant is the only explicitly stateless recovery target. Resolve its trusted image before
    # removing anything, then revalidate ownership to close the pull/remove race.
    image = self._trusted_image(spec)
    self._validate_container(existing, team_id, spec, network.name)
    try:
        self._assistant_genesis_cache.discard(existing.id)
        self._assistant_allowed_hosts_cache.discard(existing.id)
        self._assistant_machine_contract_cache.discard(existing.id)
        existing.remove(force=True)
    except DockerException as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Docker could not replace the Assistant",
            code="docker-remove-failed",
        ) from exc
    if authorize_start is None:
        self._create_assistant_container(team_id, spec, network, image)
    else:
        self._create_assistant_container(team_id, spec, network, image, authorize_start=authorize_start)


def _replace_outdated_assistant(
    self,
    team_id: str,
    spec: AssistantSpec,
    network,
    existing,
    *,
    authorize_start: Callable[[], None] | None = None,
) -> None:
    image = self._trusted_image(spec)
    config = self._validate_container_isolation(existing, team_id, spec, network.name)
    if self._has_current_assistant_artifact(config, spec):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "the installed Assistant changed during update",
            code="assistant-update-conflict",
        )
    self.chat_turn_service._retain_declared_assistant_integration_state(team_id, spec)
    remaining_egress = (
        self._team_has_egress_assistant(team_id, excluding=spec.assistant_id) if spec.allowed_hosts else None
    )
    try:
        self._assistant_genesis_cache.discard(existing.id)
        self._assistant_allowed_hosts_cache.discard(existing.id)
        self._assistant_machine_contract_cache.discard(existing.id)
        existing.remove(force=True)
    except DockerException as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Docker could not replace the Assistant",
            code="docker-remove-failed",
        ) from exc
    if spec.allowed_hosts:
        self._release_assistant_egress(
            team_id,
            spec.assistant_id,
            network,
            remaining_egress=remaining_egress,
        )
    if authorize_start is None:
        self._create_assistant_container(team_id, spec, network, image)
    else:
        self._create_assistant_container(team_id, spec, network, image, authorize_start=authorize_start)


def _restore_previous_assistant(self, team_id: str, spec: AssistantSpec, network, image) -> None:
    try:
        self._create_assistant_container(team_id, spec, network, image)
    except ApiProblem as exc:
        raise ApiProblem(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "Assistant update rollback is incomplete",
            code="assistant-update-rollback-incomplete",
        ) from exc


def _remove_retired_image(self, update) -> bool:
    if any(spec.image == update.previous.resolution["image_reference"] for spec in self.registry.all()):
        return False
    try:
        containers = self.client.containers.list(all=True)
    except DockerException:
        log.warning("Assistant update residue cleanup deferred: Docker inventory is unavailable")
        return False
    if any(container.attrs.get("Image") == update.previous_image_id for container in containers):
        return False
    try:
        self.client.images.remove(image=update.previous_image_id, force=False, noprune=True)
    except ImageNotFound:
        return True
    except DockerException:
        log.warning("Assistant update residue cleanup deferred: retired image is still referenced")
        return False
    return True


@_serialize_against_local_team_chat
def update_assistant(
    self,
    team_id: str,
    previous: AssistantSpec,
    successor: AssistantSpec,
    *,
    previous_binding: bindings.DynamicAssistantBinding,
    resolution: dict[str, object],
    authorize_start: Callable[[], None],
) -> dict[str, object]:
    previous_contract = assistant_manifest.reviewed_manifest_contract(
        allowed_hosts=previous.allowed_hosts,
        integrations=previous.integrations,
    )
    successor_contract = assistant_manifest.reviewed_manifest_contract(
        allowed_hosts=successor.allowed_hosts,
        integrations=successor.integrations,
    )
    if not assistant_manifest.update_preserves_authority(previous_contract, successor_contract):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant update requires approval for expanded authority",
            code="assistant-update-approval-required",
        )
    with self._lock(team_id):
        network = self._network(team_id)
        existing = self._assistant_container(team_id, previous.assistant_id, required=True)
        self._validate_container_security(existing, team_id, previous, network.name)
        successor_image = self._trusted_image(successor)
        try:
            previous_image = self.client.images.get(existing.attrs["Image"])
        except (KeyError, DockerException) as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker could not preserve the current Assistant image",
                code="docker-image-unavailable",
            ) from exc
        transaction = self.updates.begin(previous_binding, resolution, previous_image.id)
        remaining_egress = (
            self._team_has_egress_assistant(team_id, excluding=previous.assistant_id)
            if previous.allowed_hosts
            else None
        )
        try:
            self._assistant_genesis_cache.discard(existing.id)
            self._assistant_allowed_hosts_cache.discard(existing.id)
            self._assistant_machine_contract_cache.discard(existing.id)
            existing.remove(force=True)
            if previous.allowed_hosts:
                self._release_assistant_egress(
                    team_id,
                    previous.assistant_id,
                    network,
                    remaining_egress=remaining_egress,
                )
            self._create_assistant_container(
                team_id,
                successor,
                network,
                successor_image,
                authorize_start=authorize_start,
            )
        except (ApiProblem, DockerException) as exc:
            self._restore_previous_assistant(team_id, previous, network, previous_image)
            self.updates.clear(transaction)
            if isinstance(exc, ApiProblem):
                raise
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker could not replace the Assistant",
                code="docker-remove-failed",
            ) from exc
        try:
            self.registry.commit_replacement(
                team_id,
                previous_binding.binding_digest,
                resolution,
            )
        except bindings.DynamicAssistantError as exc:
            replacement = self._assistant_container(team_id, successor.assistant_id, required=False)
            cleanup_error = self._rollback_assistant_install(
                team_id,
                successor,
                network,
                replacement,
                egress_prepared=bool(successor.allowed_hosts),
            )
            if cleanup_error is not None:
                raise cleanup_error from exc
            self._restore_previous_assistant(team_id, previous, network, previous_image)
            self.updates.clear(transaction)
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Assistant binding changed during update",
                code="assistant-update-conflict",
            ) from exc
        self.chat_turn_service._retain_declared_assistant_integration_state(team_id, successor)
        if self._remove_retired_image(transaction):
            self.updates.clear(transaction)
        return {"assistant": successor.assistant_id, "installed": False, "updated": True}


def _recover_update_target(self, update, target: AssistantSpec) -> None:
    team_id = update.team_id
    network = self._network(team_id)
    existing = self._assistant_container(team_id, target.assistant_id, required=False)
    if existing is None:
        self._create_assistant_container(team_id, target, network, self._trusted_image(target))
        return
    previous = self.registry.spec(update.previous)
    successor = self.registry.spec(update.successor)
    config, _environment = self._validate_container_profile(existing, team_id, target, network.name)
    if self._has_current_assistant_artifact(config, target):
        self._validate_container_security(existing, team_id, target, network.name, refresh=False)
        existing.reload()
        if existing.status != "running":
            try:
                existing.start()
            except DockerException as exc:
                raise ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "Docker could not recover the Assistant",
                    code="docker-start-failed",
                ) from exc
        self._wait_ready(existing, target)
        self._active_assistant_genesis(_ActiveAssistant(target, existing.id, existing))
        return
    actual = previous if self._has_current_assistant_artifact(config, previous) else successor
    if not self._has_current_assistant_artifact(config, actual):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant update recovery found an unknown generation",
            code="assistant-update-conflict",
        )
    self._validate_container_security(existing, team_id, actual, network.name, refresh=False)
    target_image = self._trusted_image(target)
    remaining_egress = (
        self._team_has_egress_assistant(team_id, excluding=target.assistant_id) if actual.allowed_hosts else None
    )
    try:
        existing.remove(force=True)
    except DockerException as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Docker could not recover the Assistant",
            code="docker-remove-failed",
        ) from exc
    if actual.allowed_hosts:
        self._release_assistant_egress(
            team_id,
            target.assistant_id,
            network,
            remaining_egress=remaining_egress,
        )
    self._create_assistant_container(team_id, target, network, target_image)


def recover_updates(self) -> None:
    for update in self.updates.list():
        with self._lock(update.team_id):
            binding = self.registry.binding(update.team_id, update.assistant_id)
            if binding == update.previous:
                target = self.registry.spec(update.previous)
            elif binding == update.successor:
                target = self.registry.spec(update.successor)
            else:
                raise RuntimeError("Assistant update transaction does not match its binding")
            self._recover_update_target(update, target)
            if binding == update.successor:
                self.chat_turn_service._retain_declared_assistant_integration_state(update.team_id, target)
                if self._remove_retired_image(update):
                    self.updates.clear(update)
            else:
                self.updates.clear(update)


@_serialize_against_local_team_chat
def install_assistant(
    self,
    team_id: str,
    assistant_id: str,
    *,
    authorize_start: Callable[[], None] | None = None,
) -> dict[str, object]:
    spec = self._resolve(team_id, assistant_id)
    with self._lock(team_id):
        network = self._network(team_id)
        existing = self._assistant_container(team_id, assistant_id, required=False)
        if existing is not None:
            config = self._validate_container_isolation(existing, team_id, spec, network.name)
            if not self._has_current_assistant_artifact(config, spec):
                self._replace_outdated_assistant(
                    team_id,
                    spec,
                    network,
                    existing,
                    authorize_start=authorize_start,
                )
                return {"assistant": assistant_id, "installed": False}
            self._validate_container_security(existing, team_id, spec, network.name, refresh=False)
            existing.reload()
            if existing.status != "running":
                try:
                    if authorize_start is not None:
                        authorize_start()
                    existing.start()
                except DockerException as exc:
                    raise ApiProblem(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "Docker could not start the Assistant",
                        code="docker-start-failed",
                    ) from exc
            try:
                self._wait_ready(existing, spec)
            except ApiProblem as exc:
                if not _is_replaceable_readiness_failure(exc):
                    raise
                self._replace_unready_assistant(
                    team_id,
                    spec,
                    network,
                    existing,
                    authorize_start=authorize_start,
                )
            else:
                self._active_assistant_genesis(_ActiveAssistant(spec, existing.id, existing))
            return {"assistant": assistant_id, "installed": False}

        image = self._trusted_image(spec)
        if authorize_start is None:
            self._create_assistant_container(team_id, spec, network, image)
        else:
            self._create_assistant_container(
                team_id,
                spec,
                network,
                image,
                authorize_start=authorize_start,
            )
        return {"assistant": assistant_id, "installed": True}


@_serialize_against_local_team_chat
def uninstall_assistant(self, team_id: str, assistant_id: str) -> dict[str, object]:
    spec = self._resolve(team_id, assistant_id)
    self.chat_turn_service._delete_chat_continuation(team_id)
    with self._lock(team_id):
        network = self._network(team_id)
        container = self._assistant_container(team_id, assistant_id, required=False)
        if container is None:
            if self._egress_token(team_id, assistant_id, create=False) is not None:
                remaining_egress = self._team_has_egress_assistant(team_id, excluding=assistant_id)
                self._release_assistant_egress(
                    team_id,
                    assistant_id,
                    network,
                    remaining_egress=remaining_egress,
                )
            self.chat_turn_service._delete_assistant_integration_state(team_id, assistant_id)
            self.registry.delete(team_id, assistant_id)
            return {"assistant": assistant_id, "uninstalled": False}
        self._validate_container_isolation(container, team_id, spec, network.name)
        remaining_egress = (
            self._team_has_egress_assistant(team_id, excluding=assistant_id) if spec.allowed_hosts else None
        )
        try:
            container.remove(force=True)
        except DockerException as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker could not uninstall the Assistant",
                code="docker-remove-failed",
            ) from exc
        self._blocked_power_workloads.discard(container.id)
        self._assistant_genesis_cache.discard(container.id)
        self._assistant_allowed_hosts_cache.discard(container.id)
        self._assistant_machine_contract_cache.discard(container.id)
        if spec.allowed_hosts:
            self._release_assistant_egress(
                team_id,
                assistant_id,
                network,
                remaining_egress=remaining_egress,
            )
        self.chat_turn_service._delete_assistant_integration_state(team_id, assistant_id)
        self.registry.delete(team_id, assistant_id)
        return {"assistant": assistant_id, "uninstalled": True}
