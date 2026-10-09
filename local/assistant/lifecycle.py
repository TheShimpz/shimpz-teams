"""Local Assistant container install, update, and uninstall lifecycle."""

import logging
from collections.abc import Callable
from contextlib import suppress
from http import HTTPStatus

from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import LogConfig, Ulimit

from action import execution as action_execution
from assistant import manifest as assistant_manifest
from install import bindings, icons
from local.assistant import isolation as local_container_policy
from local.chat.types import ActiveAssistant as _ActiveAssistant
from local.errors import ApiProblemError as ApiProblem
from local.errors import assistant_icon_unavailable, assistant_registry_drift, assistant_replace_failed
from local.install import snapshots as local_snapshots
from local.install.runtime import AssistantSpec
from local.labels import ASSISTANT_LABEL
from local.validation import validate_team_id
from protocol.http.v1 import payload as http_payload

ASSISTANT_MEMORY = local_container_policy.ASSISTANT_MEMORY
ASSISTANT_NANO_CPUS = local_container_policy.ASSISTANT_NANO_CPUS
ASSISTANT_PIDS = local_container_policy.ASSISTANT_PIDS
ASSISTANT_TMPFS = local_container_policy.ASSISTANT_TMPFS
ASSISTANT_ULIMITS = local_container_policy.ASSISTANT_ULIMITS
log = logging.getLogger("shimpz.team.local.assistant.lifecycle")


def _forget_container_review(self, container_id: str) -> None:
    """Drop every review this controller cached for one Assistant container, so its next use is reviewed afresh."""
    self._assistant_genesis_cache.discard(container_id)
    self._assistant_allowed_hosts_cache.discard(container_id)
    self._assistant_machine_contract_cache.discard(container_id)
    self._assistant_language_cache.discard(container_id)


def _is_replaceable_readiness_failure(problem: ApiProblem) -> bool:
    return problem.code == "assistant-not-ready"


def _retired_image_id(container) -> str | None:
    image_id = container.attrs.get("Image")
    if not isinstance(image_id, str) or http_payload.SOURCE_DIGEST_RE.fullmatch(image_id) is None:
        return None
    return image_id


def _serialize_against_local_team_chat(
    operation: Callable[..., dict[str, object]],
) -> Callable[..., dict[str, object]]:
    """Reject Assistant mutation before its first side effect while a Team turn owns the slot."""

    def guarded(controller, team_id: str, *args, **kwargs) -> dict[str, object]:
        return _run_against_local_team_chat(
            controller,
            team_id,
            lambda: operation(controller, team_id, *args, **kwargs),
        )

    return guarded


def _run_against_local_team_chat(
    self,
    team_id: str,
    operation: Callable[[], dict[str, object]],
) -> dict[str, object]:
    team_id = validate_team_id(team_id)
    lock = self.chat_turn_service._chat_lock(team_id)
    if not lock.acquire(blocking=False):
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant lifecycle cannot change during an active Team chat turn",
            code="chat-active",
        )
    try:
        return operation()
    finally:
        lock.release()


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
        _forget_container_review(self, container.id)
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
            self._remove_egress_policy(team_id, spec.assistant_id)
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
    try:
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
            # Team's own route for this Assistant's provider calls; the workload never holds it (ADR-0106).
            egress_prepared = True
            self._write_egress_policy(team_id, spec, allowed_hosts)
        if authorize_start is not None:
            authorize_start()
        container.start()
        self._validate_container(container, team_id, spec, network.name)
        self._wait_ready(container, spec)
        self._active_assistant_genesis(_ActiveAssistant(spec, container.id, container))
    except (ApiProblem, DockerException) as exc:
        cleanup_error = self._rollback_assistant_install(
            team_id,
            spec,
            network,
            container,
            egress_prepared=egress_prepared,
        )
        if cleanup_error is not None:
            raise cleanup_error from exc
        if isinstance(exc, ApiProblem):
            raise
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
        _forget_container_review(self, existing.id)
        existing.remove(force=True)
    except DockerException as exc:
        raise assistant_replace_failed() from exc
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
    try:
        _forget_container_review(self, existing.id)
        existing.remove(force=True)
    except DockerException as exc:
        raise assistant_replace_failed() from exc
    if spec.allowed_hosts:
        self._remove_egress_policy(team_id, spec.assistant_id)
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
        for image in self.registry.images():
            try:
                if self.client.images.get(image).id == image_id:
                    return True
            except ImageNotFound:
                continue
    except bindings.DynamicAssistantError, DockerException:
        log.warning("Assistant update residue cleanup deferred: binding images are unavailable")
        return None
    return False


def _inspect_retired_image(self, image_id: str) -> tuple[bool, bool] | None:
    try:
        image = self.client.images.get(image_id)
    except ImageNotFound:
        return False, False
    except DockerException:
        log.warning("Assistant update residue cleanup deferred: image metadata is unavailable")
        return None
    attributes = image.attrs
    config = attributes.get("Config") if isinstance(attributes, dict) else None
    labels = config.get("Labels") if isinstance(config, dict) else None
    if labels is not None and not isinstance(labels, dict):
        log.warning("Assistant update residue cleanup deferred: image labels are invalid")
        return None
    is_local_snapshot = (
        isinstance(labels, dict) and labels.get(local_snapshots.LOCAL_STAGE_LABEL) == local_snapshots.LOCAL_STAGE_VALUE
    )
    return True, is_local_snapshot


def _delete_retired_image(self, image_id: str) -> bool:
    inspection = _inspect_retired_image(self, image_id)
    if inspection is None:
        return False
    exists, is_local_snapshot = inspection
    if not exists or is_local_snapshot:
        return True
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
        containers = self.client.containers.list(all=True, sparse=True, filters={"ancestor": image_id})
    except DockerException:
        log.warning("Assistant update residue cleanup deferred: Docker inventory is unavailable")
        return False
    if containers:
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


def _queue_published_residue(
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
            "Assistant update requires approval for expanded outbound hosts or moved credentials",
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
        try:
            _forget_container_review(self, existing.id)
            existing.remove(force=True)
            if previous.allowed_hosts:
                self._remove_egress_policy(team_id, previous.assistant_id)
            self._create_assistant_container(
                team_id,
                successor,
                network,
                successor_image,
                authorize_start=authorize_start,
            )
        except (ApiProblem, DockerException) as exc:
            self._restore_previous_assistant(team_id, previous, network, previous_image)
            self._queue_published_residue(previous_binding, successor_image.id)
            self._clear_update(transaction)
            if isinstance(exc, ApiProblem):
                raise
            raise assistant_replace_failed() from exc
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
            self._queue_published_residue(previous_binding, successor_image.id)
            self._clear_update(transaction)
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "Assistant binding changed during update",
                code="assistant-update-conflict",
            ) from exc
        self.chat_turn_service._retain_declared_assistant_integration_state(team_id, successor)
        self.chat_turn_service._retain_declared_assistant_stored_input_state(team_id, successor)
        self._queue_published_residue(previous_binding, transaction.previous_image_id)
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
    try:
        existing.remove(force=True)
    except DockerException as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Docker could not recover the Assistant",
            code="docker-remove-failed",
        ) from exc
    if actual.allowed_hosts:
        self._remove_egress_policy(team_id, target.assistant_id)
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
                    self._queue_published_residue(update.previous, update.previous_image_id)
                self._clear_update(update)
            # A transaction over a binding the current contract refuses stays deferred for that Assistant only.
            except ApiProblem, RuntimeError, DockerException, bindings.DynamicAssistantError:
                log.exception(
                    "Assistant update recovery deferred for %s/%s",
                    update.team_id,
                    update.assistant_id,
                )
    self.sweep_residues()


def quarantine_inadmissible(self) -> None:
    """At startup, take every binding the current contract refuses out of service, one Assistant at a time.

    Its admitted egress policy is revoked first and independently, so Team holds no route for a binding it refuses;
    then its owned runtime container is removed, so nothing runs that current admission cannot validate. The binding,
    its Team-custodied state, and its image stay until its Supervisor replaces or uninstalls it, and a published image
    is queued as residue that is collected only once no binding holds it. A failure is logged for that Assistant only
    and never stops the Team (ADR-0033's 2026-10-08 amendment).
    """
    try:
        refused = self.registry.inadmissible()
    except bindings.DynamicAssistantError:
        log.exception("Assistant quarantine deferred: binding store is unavailable")
        return
    for binding in refused:
        team_id, assistant_id = binding.team_id, binding.assistant_id
        log.warning("Installed Assistant %s/%s needs replacement under the current contract", team_id, assistant_id)
        with self._lock(team_id):
            try:
                self._remove_egress_policy(team_id, assistant_id)
            except ApiProblem:
                log.exception("Assistant egress revocation deferred for %s/%s", team_id, assistant_id)
            try:
                _remove_inadmissible_runtime(self, binding)
            except ApiProblem, DockerException:
                log.exception("Assistant quarantine deferred for %s/%s", team_id, assistant_id)


def _remove_inadmissible_runtime(self, binding: bindings.DynamicAssistantBinding) -> None:
    team_id, assistant_id = binding.team_id, binding.assistant_id
    container = self._assistant_container(team_id, assistant_id, required=False)
    if container is not None:
        expected = self._base_labels(team_id, "assistant")
        expected[ASSISTANT_LABEL] = assistant_id
        if not self._labels_include(container.labels, expected):
            # A container with this name that is not provably this Assistant's is never removed as if it were.
            raise assistant_registry_drift()
        retired_image_id = _retired_image_id(container)
        container.remove(force=True)
        self._blocked_action_workloads.discard(container.id)
        _forget_container_review(self, container.id)
        if retired_image_id is not None:
            self._queue_published_residue(binding, retired_image_id)


def _retire_refused_update(self, team_id: str, assistant_id: str) -> None:
    """Retire the interrupted update of a refused binding, which recovery can never complete, with its Assistant.

    Its previous published image stays queued as residue; a transaction that cannot be retired keeps the Assistant
    installed, so it is never left blocking every later update of the same Assistant.
    """
    try:
        update = self.updates.get(team_id, assistant_id)
        if update is None:
            return
        # The previous image must be durably queued before the transaction that records it goes.
        if update.previous.provenance == "published":
            self.residues.add(update.previous_image_id)
        self.updates.clear(update)
    except (bindings.DynamicAssistantError, OSError) as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant update state could not be retired",
            code="assistant-update-unavailable",
        ) from exc


def resume_assistants(self) -> None:
    try:
        identities = sorted(self.registry.identities())
        refused = {(binding.team_id, binding.assistant_id) for binding in self.registry.inadmissible()}
    except bindings.DynamicAssistantError:
        log.exception("Assistant startup recovery deferred: binding store is unavailable")
        return
    for team_id, assistant_id in identities:
        if (team_id, assistant_id) in refused:
            continue
        try:
            self.install_assistant(team_id, assistant_id)
        except ApiProblem, bindings.DynamicAssistantError, RuntimeError, DockerException:
            log.exception(
                "Assistant startup recovery deferred for %s/%s",
                team_id,
                assistant_id,
            )


def _install_assistant_unguarded(
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
        self._create_assistant_container(team_id, spec, network, image, authorize_start=authorize_start)
        return {"assistant": assistant_id, "installed": True}


@_serialize_against_local_team_chat
def install_assistant(
    self,
    team_id: str,
    assistant_id: str,
    *,
    authorize_start: Callable[[], None] | None = None,
) -> dict[str, object]:
    return _install_assistant_unguarded(
        self,
        team_id,
        assistant_id,
        authorize_start=authorize_start,
    )


def _uninstall_assistant_unguarded(self, team_id: str, assistant_id: str) -> dict[str, object]:
    """Confirm the Assistant is absent from the Team; an Assistant already absent is success, not an error."""
    binding = self.registry.binding(team_id, assistant_id)
    self.chat_turn_service._delete_chat_continuation(team_id)
    with self._lock(team_id):
        network = self._network(team_id)
        if binding is not None and not binding.admissible:
            # No admitted spec can validate this runtime: retire its unrecoverable update, remove it by its ownership
            # labels, then release the rest exactly as for an Assistant whose container is already gone.
            _retire_refused_update(self, team_id, assistant_id)
            try:
                _remove_inadmissible_runtime(self, binding)
            except DockerException as exc:
                raise ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "Docker could not uninstall the Assistant",
                    code="docker-remove-failed",
                ) from exc
        container = self._assistant_container(team_id, assistant_id, required=False)
        if container is None:
            if self._egress_token(team_id, assistant_id, create=False) is not None:
                self._remove_egress_policy(team_id, assistant_id)
            self.chat_turn_service._delete_assistant_integration_state(team_id, assistant_id)
            self.chat_turn_service._delete_assistant_stored_input_state(team_id, assistant_id)
            _retire_binding(self, team_id, assistant_id, binding)
            self.sweep_residues()
            return {"assistant": assistant_id, "uninstalled": False}
        spec = self.registry.get(team_id, assistant_id)
        if spec is None:
            # A container without its binding cannot be validated, so it is never removed as if it were owned.
            raise assistant_registry_drift()
        self._validate_container_profile(container, team_id, spec, network.name)
        retired_image_id = _retired_image_id(container)
        try:
            container.remove(force=True)
        except DockerException as exc:
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Docker could not uninstall the Assistant",
                code="docker-remove-failed",
            ) from exc
        self._blocked_action_workloads.discard(container.id)
        _forget_container_review(self, container.id)
        if retired_image_id is not None and (binding is None or binding.provenance == "published"):
            self._queue_residue(retired_image_id)
        if spec.allowed_hosts:
            self._remove_egress_policy(team_id, assistant_id)
        self.chat_turn_service._delete_assistant_integration_state(team_id, assistant_id)
        self.chat_turn_service._delete_assistant_stored_input_state(team_id, assistant_id)
        _retire_binding(self, team_id, assistant_id, binding)
        self.sweep_residues()
        return {"assistant": assistant_id, "uninstalled": True}


def _retire_binding(self, team_id: str, assistant_id: str, binding) -> None:
    """Retire the binding and its unreferenced icon together; a failed icon removal keeps the binding to retry."""
    if binding is None:
        self.registry.delete(team_id, assistant_id)
        return
    try:
        self.icons.retire(binding, self.registry.bindings, lambda: self.registry.delete(team_id, assistant_id))
    except icons.AssistantIconError as exc:
        raise assistant_icon_unavailable() from exc


@_serialize_against_local_team_chat
def uninstall_assistant(self, team_id: str, assistant_id: str) -> dict[str, object]:
    return _uninstall_assistant_unguarded(self, team_id, assistant_id)


@_serialize_against_local_team_chat
def install_fresh_local(
    self,
    team_id: str,
    assistant_id: str,
    install_successor: Callable[[Callable[..., dict[str, object]]], dict[str, object]],
) -> dict[str, object]:
    if self.registry.binding(team_id, assistant_id) is not None:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant binding changed before Local installation",
            code="assistant-binding-conflict",
        )
    return install_successor(self._install_assistant_unguarded)


@_serialize_against_local_team_chat
def replace_inadmissible(
    self,
    team_id: str,
    previous_binding: bindings.DynamicAssistantBinding,
    install_successor: Callable[[Callable[..., dict[str, object]]], dict[str, object]],
) -> dict[str, object]:
    """Replace a binding the current contract refuses by uninstalling it and installing its successor fresh.

    Its declarations cannot be admitted, so nothing proves the successor compatible with them: its Team-custodied
    Integration and Stored Input state is deleted with it, and the person provides it again.
    """
    current = self.registry.binding(team_id, previous_binding.assistant_id)
    if current != previous_binding or current.admissible:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant binding changed before replacement",
            code="assistant-binding-conflict",
        )
    self._uninstall_assistant_unguarded(team_id, previous_binding.assistant_id)
    return install_successor(self._install_assistant_unguarded)


@_serialize_against_local_team_chat
def replace_published_with_local(
    self,
    team_id: str,
    previous_binding: bindings.DynamicAssistantBinding,
    install_successor: Callable[[Callable[..., dict[str, object]]], dict[str, object]],
) -> dict[str, object]:
    current = self.registry.binding(team_id, previous_binding.assistant_id)
    if current != previous_binding or current.provenance != "published":
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Assistant binding changed before Local replacement",
            code="assistant-binding-conflict",
        )
    self._uninstall_assistant_unguarded(team_id, previous_binding.assistant_id)
    return install_successor(self._install_assistant_unguarded)
