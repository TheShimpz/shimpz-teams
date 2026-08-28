"""Local Assistant container install, update, and uninstall lifecycle."""

import logging
import re
from collections.abc import Callable
from contextlib import suppress
from http import HTTPStatus

from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import LogConfig, Ulimit

from action import execution as action_execution
from assistant import manifest as assistant_manifest
from install import bindings
from local.assistant import isolation as local_container_policy
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.errors import ApiProblemError as ApiProblem
from local.install.runtime import AssistantSpec
from local.validation import validate_team_id

ASSISTANT_MEMORY = local_container_policy.ASSISTANT_MEMORY
ASSISTANT_NANO_CPUS = local_container_policy.ASSISTANT_NANO_CPUS
ASSISTANT_PIDS = local_container_policy.ASSISTANT_PIDS
ASSISTANT_TMPFS = local_container_policy.ASSISTANT_TMPFS
ASSISTANT_ULIMITS = local_container_policy.ASSISTANT_ULIMITS
log = logging.getLogger("shimpz.team.local.assistant.lifecycle")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _is_replaceable_readiness_failure(problem: ApiProblem) -> bool:
    return problem.code == "assistant-not-ready"


def _retired_image_id(container) -> str | None:
    image_id = container.attrs.get("Image")
    if not isinstance(image_id, str) or _IMAGE_ID_RE.fullmatch(image_id) is None:
        return None
    return image_id


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
                self._fail_stop_action(container)
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
            user=action_execution.ASSISTANT_RPC_USER,
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
            log_config=LogConfig(type=LogConfig.types.NONE),
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
    image = self._assistant_image(spec)
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
    image = self._assistant_image(spec)
    config = self._validate_container_isolation(existing, team_id, spec, network.name)
    if self._has_current_assistant_artifact(config, spec):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "the installed Assistant changed during update",
            code="assistant-update-conflict",
        )
    self.chat_turn_service._retain_declared_assistant_integration_state(team_id, spec)
    self.chat_turn_service._retain_declared_assistant_stored_input_state(team_id, spec)
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


def _binding_uses_image(self, image_id: str) -> bool | None:
    try:
        for spec in self.registry.all():
            try:
                if self.client.images.get(spec.image).id == image_id:
                    return True
            except ImageNotFound:
                continue
    except bindings.DynamicAssistantError, DockerException:
        log.warning("Assistant update residue cleanup deferred: binding images are unavailable")
        return None
    return False


def _delete_retired_image(self, image_id: str) -> bool:
    try:
        self.client.images.remove(image=image_id, force=False, noprune=True)
    except ImageNotFound:
        return True
    except DockerException:
        log.warning("Assistant update residue cleanup deferred: retired image is still referenced")
        return False
    return True


def _remove_retired_image(self, image_id: str) -> bool:
    binding_use = self._binding_uses_image(image_id)
    if binding_use is None or binding_use:
        return False
    try:
        containers = self.client.containers.list(all=True)
    except DockerException:
        log.warning("Assistant update residue cleanup deferred: Docker inventory is unavailable")
        return False
    if any(image_id in {container.attrs.get("ImageID"), container.attrs.get("Image")} for container in containers):
        return False
    return self._delete_retired_image(image_id)


def _clear_update(self, update) -> None:
    try:
        self.updates.clear(update)
    except bindings.DynamicAssistantError:
        log.warning("Assistant update transaction cleanup deferred")


def sweep_residues(self) -> None:
    try:
        residues = self.residues.list()
    except bindings.DynamicAssistantError:
        log.warning("Assistant update residue queue is unavailable")
        return
    for residue in residues:
        if not self._remove_retired_image(residue.image_id):
            continue
        try:
            self.residues.clear(residue)
        except bindings.DynamicAssistantError:
            log.warning("Assistant update residue record cleanup deferred")


def _queue_residue(self, image_id: str) -> None:
    try:
        self.residues.add(image_id)
    except bindings.DynamicAssistantError, OSError:
        log.exception("Assistant update residue could not be queued")
        if not self._remove_retired_image(image_id):
            log.warning("Assistant update left one unqueued image residue")


def _queue_failed_successor(
    self,
    binding: bindings.DynamicAssistantBinding,
    image_id: str,
) -> None:
    if binding.provenance == "published":
        self._queue_residue(image_id)


def _commit_replacement(
    self,
    team_id: str,
    previous: bindings.DynamicAssistantBinding,
    successor_document: dict[str, object],
) -> None:
    if previous.provenance == "published":
        self.registry.commit_replacement(team_id, previous.binding_digest, successor_document)
        return
    if previous.provenance == "local":
        self.registry.commit_local_replacement(team_id, previous.binding_digest, successor_document)
        return
    raise bindings.DynamicAssistantConflictError("the Assistant binding provenance cannot be replaced")


@_serialize_against_local_team_chat
def update_assistant(
    self,
    team_id: str,
    previous: AssistantSpec,
    successor: AssistantSpec,
    *,
    previous_binding: bindings.DynamicAssistantBinding,
    successor_document: dict[str, object],
    authorize_start: Callable[[], None],
) -> dict[str, object]:
    previous_contract = assistant_manifest.reviewed_manifest_contract(
        allowed_hosts=previous.allowed_hosts,
        integrations=previous.integrations,
        stored_inputs=previous.stored_inputs,
    )
    successor_contract = assistant_manifest.reviewed_manifest_contract(
        allowed_hosts=successor.allowed_hosts,
        integrations=successor.integrations,
        stored_inputs=successor.stored_inputs,
    )
    if not assistant_manifest.automatic_update_preserves_egress(previous_contract, successor_contract):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant update requires approval for expanded outbound hosts",
            code="assistant-update-approval-required",
        )
    with self._lock(team_id):
        network = self._network(team_id)
        existing = self._assistant_container(team_id, previous.assistant_id, required=True)
        self._validate_container_security(existing, team_id, previous, network.name)
        successor_image = self._assistant_image(successor)
        try:
            previous_image = self.client.images.get(existing.attrs["Image"])
        except (KeyError, DockerException) as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker could not preserve the current Assistant image",
                code="docker-image-unavailable",
            ) from exc
        transaction = self.updates.begin(previous_binding, successor_document, previous_image.id)
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
            self._queue_failed_successor(previous_binding, successor_image.id)
            self._clear_update(transaction)
            if isinstance(exc, ApiProblem):
                raise
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker could not replace the Assistant",
                code="docker-remove-failed",
            ) from exc
        try:
            self._commit_replacement(
                team_id,
                previous_binding,
                successor_document,
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
            self._queue_failed_successor(previous_binding, successor_image.id)
            self._clear_update(transaction)
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Assistant binding changed during update",
                code="assistant-update-conflict",
            ) from exc
        self.chat_turn_service._retain_declared_assistant_integration_state(team_id, successor)
        self.chat_turn_service._retain_declared_assistant_stored_input_state(team_id, successor)
        self._queue_residue(transaction.previous_image_id)
        self._clear_update(transaction)
        self.sweep_residues()
        return {"assistant": successor.assistant_id, "installed": False, "updated": True}


def _recover_update_target(self, update, target: AssistantSpec) -> None:
    team_id = update.team_id
    network = self._network(team_id)
    existing = self._assistant_container(team_id, target.assistant_id, required=False)
    if existing is None:
        self._create_assistant_container(team_id, target, network, self._assistant_image(target))
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
    target_image = self._assistant_image(target)
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
    try:
        updates = self.updates.list()
    except bindings.DynamicAssistantError:
        log.exception("Assistant update recovery deferred: transaction store is unavailable")
        return
    for update in updates:
        with self._lock(update.team_id):
            try:
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
                    self.chat_turn_service._retain_declared_assistant_stored_input_state(update.team_id, target)
                    self._queue_residue(update.previous_image_id)
                self._clear_update(update)
            except ApiProblem, RuntimeError, DockerException:
                log.exception(
                    "Assistant update recovery deferred for %s/%s",
                    update.team_id,
                    update.assistant_id,
                )
    self.sweep_residues()


def resume_assistants(self) -> None:
    try:
        identities = sorted(self.registry.identities())
    except bindings.DynamicAssistantError:
        log.exception("Assistant startup recovery deferred: binding store is unavailable")
        return
    for team_id, assistant_id in identities:
        try:
            self.install_assistant(team_id, assistant_id)
        except ApiProblem, bindings.DynamicAssistantError, RuntimeError, DockerException:
            log.exception(
                "Assistant startup recovery deferred for %s/%s",
                team_id,
                assistant_id,
            )


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

        image = self._assistant_image(spec)
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
    binding = self.registry.binding(team_id, assistant_id)
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
            self.chat_turn_service._delete_assistant_stored_input_state(team_id, assistant_id)
            self.registry.delete(team_id, assistant_id)
            if binding is not None:
                self.icons.discard_binding(binding, self.registry.bindings())
            self.sweep_residues()
            return {
                "assistant": assistant_id,
                "uninstalled": False,
                **_local_stage_retention(binding),
            }
        self._validate_container_profile(container, team_id, spec, network.name)
        retired_image_id = _retired_image_id(container)
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
        self._blocked_action_workloads.discard(container.id)
        self._assistant_genesis_cache.discard(container.id)
        self._assistant_allowed_hosts_cache.discard(container.id)
        self._assistant_machine_contract_cache.discard(container.id)
        if retired_image_id is not None and (binding is None or binding.provenance == "published"):
            self._queue_residue(retired_image_id)
        if spec.allowed_hosts:
            self._release_assistant_egress(
                team_id,
                assistant_id,
                network,
                remaining_egress=remaining_egress,
            )
        self.chat_turn_service._delete_assistant_integration_state(team_id, assistant_id)
        self.chat_turn_service._delete_assistant_stored_input_state(team_id, assistant_id)
        self.registry.delete(team_id, assistant_id)
        if binding is not None:
            self.icons.discard_binding(binding, self.registry.bindings())
        self.sweep_residues()
        return {
            "assistant": assistant_id,
            "uninstalled": True,
            **_local_stage_retention(binding),
        }


def _local_stage_retention(binding: bindings.DynamicAssistantBinding | None) -> dict[str, object]:
    if binding is None or binding.provenance != "local":
        return {}
    image_id = binding.local_record.get("image_id")
    if not isinstance(image_id, str) or _IMAGE_ID_RE.fullmatch(image_id) is None:
        raise bindings.DynamicAssistantError("the Local Assistant image id is invalid")
    return {
        "staged_image_retained": image_id,
        "remove_command": f"docker image rm {image_id}",
    }
