"""Hosted Team chat API, continuation, OAuth, and cancellation operations."""

import contextlib
import secrets
from collections.abc import Callable
from http import HTTPStatus

import docker.errors

from action import stored_input as action_stored_input
from assistant import spec as assistant_registry
from chat import turn as chat_turn_engine
from hosted import audit
from hosted import state as runtime_state
from hosted.assistant import lifecycle as assistant_lifecycle
from hosted.assistant import runtime as hosted_assistants
from hosted.chat import human as hosted_chat_human
from hosted.chat import lifecycle as hosted_chat_lifecycle
from hosted.chat import segment as hosted_chat_segment
from hosted.team import resources as hosted_resources
from inference import abort as request_abort
from integrations import challenges as integration_challenges
from integrations import pkce as integration_pkce
from integrations import service as integration_service


@contextlib.contextmanager
def _exclusive_chat_turn(team_id: str, lease: hosted_resources._AuthorizationLease):
    """Hold one Controller-owned agent turn without creating a process in the Team."""
    lock = runtime_state._chat_lock_for(team_id)
    if not lock.acquire(blocking=False):
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, f"team {team_id!r} already has an active chat turn")
    try:
        container = hosted_resources._require_current_authorization(team_id, lease)
        container.reload()
        if container.status != "running":
            raise runtime_state.ApiError(
                HTTPStatus.CONFLICT,
                f"team {team_id!r} is not running (status={container.status})",
            )
    except BaseException:
        lock.release()
        raise
    token = secrets.token_hex(16)
    # Registered before any Brain request of the turn, so Stop can always reach the one in flight (ADR-0079).
    brain_abort = request_abort.RequestAbort()
    with runtime_state._active_chat_guard:
        draining = team_id in runtime_state._draining_chats
        if not draining:
            runtime_state._active_chat_tokens[team_id] = token
            runtime_state._active_chat_container_ids[team_id] = container.id
            runtime_state._brain_aborts[token] = brain_abort
    if draining:
        lock.release()
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, f"team {team_id!r} is being destroyed")
    try:
        with request_abort.abortable(brain_abort):
            yield token, container
    finally:
        with runtime_state._active_chat_guard:
            runtime_state._brain_aborts.pop(token, None)
            runtime_state._active_chat_tokens.pop(team_id, None)
            runtime_state._active_chat_container_ids.pop(team_id, None)
            runtime_state._active_action_container_ids.pop(team_id, None)
            runtime_state._cancelled_chat_tokens.discard(token)
        lock.release()


def _chat(
    team_id: str,
    message: str,
    file_ids: object,
    assistant_ids: tuple[str, ...],
    lease: hosted_resources._AuthorizationLease,
    locale: str | None = None,
) -> dict:
    """Run one bounded Team turn across the explicit Controller-brokered Assistant scope."""
    pending = _authorized_pending(team_id, lease, _pending_hosted_chat)
    if pending is not None:
        return pending
    # The slot comes first. A losing concurrent request must not run even the local credential probe,
    # much less provider status or a second provider CLI.
    with _exclusive_chat_turn(team_id, lease) as (token, container):
        pending = _admit_fresh_turn(team_id, container)
        if pending is not None:
            return pending
        return hosted_chat_segment._chat_in_turn(
            hosted_chat_segment.HostedChatSegmentRequest(
                team_id=team_id,
                file_ids=file_ids,
                assistant_ids=assistant_ids,
                token=token,
                container=container,
                owner=lease.owner,
                message=message,
                locale=locale,
            )
        )


def _admit_fresh_turn(team_id: str, container: object) -> dict[str, object] | None:
    """Admit a fresh turn under its held exclusive slot, before any response byte, for ordinary and streamed chat.

    A pending gate answers instead. Otherwise the generation's settled journal residue ends, including a batch whose
    in-memory challenge or turn a Controller restart lost, so the new turn's batch is not refused as pending.
    """
    pending = _pending_hosted_chat(team_id)
    if pending is not None:
        return pending
    try:
        runtime_state._action_execution_journal().end_settled(container.id)
    except hosted_chat_segment.action_journal.ActionJournalError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Team Action execution state is unavailable",
        ) from exc
    return None


def _authorized_pending[T](team_id: str, lease: hosted_resources._AuthorizationLease, read: Callable[[str], T]) -> T:
    """Read pending gate state only for the exact Team generation the lease authorized.

    A Team destroyed and recreated under the same id, even by another Account, may hold its own pending gate; the
    lifecycle lock keeps the revalidated generation current while it is read.
    """
    with runtime_state._lock_for(team_id):
        hosted_resources._require_current_authorization(team_id, lease, require_isolation=False)
        return read(team_id)


def _pending_hosted_chat(team_id: str) -> dict[str, object] | None:
    human = hosted_chat_human.pending_chat_human(team_id)
    if human["status"] != "none":
        return human
    integration = runtime_state._integration_challenges.current(team_id)
    if integration is not None:
        return hosted_chat_segment._hosted_integration_challenge_payload(integration)
    return None


def _pending_integration(team_id: str) -> dict[str, object]:
    pending = runtime_state._integration_challenges.current(team_id)
    if pending is None:
        return {"team_id": team_id, "status": "none"}
    return hosted_chat_segment._hosted_integration_challenge_payload(pending)


def _current_integration_declaration(team_id: str, assistant_id: str, integration_id: str) -> object:
    try:
        installed_id, contract, _container = hosted_assistants._installed_assistant(team_id, assistant_id)
        declaration = contract.integrations.get(integration_id)
        if installed_id != assistant_id or declaration is None:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Assistant integration declaration changed")
    except runtime_state.ApiError, assistant_registry.AssistantSpecError:
        # The OAuth service intentionally receives one opaque typed failure so
        # registry, Docker, and manifest details cannot reach the callback response.
        raise integration_service.OAuthIntegrationDeclarationError(
            "installed Assistant integration declaration is unavailable"
        ) from None
    else:
        return declaration


def _start_oauth_integration(
    team_id: str,
    challenge_id: object,
    assistant_id: object,
    integration_id: object,
    session_binding: object,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    # Destruction cancels this Team's OAuth state under the same lock, so none is created for a generation it ended. A
    # turn publishes its Integration challenge before its pause commits, holding the Team chat slot throughout, so a
    # start is refused while the slot is held: it never issues OAuth state from a challenge whose failed commit then
    # withdraws it. The slot is only tried under the Team lock, never awaited, as destruction awaits it under that lock.
    with runtime_state._lock_for(team_id), runtime_state._idle_team_chat(team_id):
        hosted_resources._require_current_authorization(team_id, lease, require_isolation=False)
        try:
            challenge = runtime_state._integration_challenges.get(team_id, challenge_id)
        except integration_challenges.IntegrationChallengeNotFoundError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.CONFLICT,
                "Assistant integration request expired; retry the message",
            ) from exc
        pending = challenge.payload
        if not isinstance(pending, hosted_assistants._PendingHostedChat) or pending.owner != lease.owner:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Team capabilities changed; retry")
        try:
            authorization_url = runtime_state._oauth_integrations.authorization_url(
                challenge,
                session_binding,
                assistant_id=assistant_id,
                integration_id=integration_id,
                resource_binding=(lease.owner, lease.container_id),
            )
        except integration_service.OAuthIntegrationUnavailableError as exc:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Assistant integrations are already configured") from exc
        except integration_service.OAuthIntegrationServiceError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant integration could not be started",
            ) from exc
    return {"authorization_url": authorization_url}


def _callback_binding(body: dict[str, object]) -> tuple[integration_pkce.OAuthCallbackBinding, str, str]:
    try:
        binding = runtime_state._integration_pkce.inspect_callback(
            state=body["state"],
            session_binding=body["session_binding"],
        )
    except integration_pkce.OAuthChallengeNotFoundError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.CONFLICT,
            "Assistant integration request expired; retry",
        ) from exc
    except integration_pkce.OAuthChallengeError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.BAD_GATEWAY,
            "Assistant integration could not be completed",
        ) from exc
    resource = binding.resource_binding
    if not isinstance(resource, tuple) or len(resource) != 2:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "OAuth Team authority is unavailable")
    owner, container_id = resource
    if not isinstance(owner, str) or not owner or not isinstance(container_id, str) or not container_id:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "OAuth Team authority is unavailable")
    return binding, owner, container_id


def _compensate_oauth_completion(completion: integration_service.OAuthIntegrationCompletion, owner: str) -> None:
    try:
        runtime_state._oauth_integrations.disconnect(
            completion.team_id,
            completion.assistant_id,
            completion.integration_id,
        )
    except integration_service.OAuthIntegrationServiceError as exc:
        audit.log(
            "oauth_completion_compensate",
            completion.team_id,
            result="error",
            principal_id="admin",
            principal_class="machine",
            owner_account_id=owner,
            reason=type(exc).__name__,
        )
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant integration cleanup is incomplete",
        ) from exc
    audit.log(
        "oauth_completion_compensate",
        completion.team_id,
        result="ok",
        principal_id="admin",
        principal_class="machine",
        owner_account_id=owner,
    )


def _completion_matches(
    binding: integration_pkce.OAuthCallbackBinding,
    completion: integration_service.OAuthIntegrationCompletion,
) -> bool:
    return (
        completion.team_id == binding.team_id
        and completion.assistant_id == binding.assistant_id
        and completion.integration_id == binding.integration_id
        and completion.resource_binding == binding.resource_binding
    )


def _complete_integration_callback(
    body: object,
) -> tuple[dict[str, object], str]:
    if not isinstance(body, dict) or set(body) != {"state", "code", "session_binding"}:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "OAuth callback is invalid")
    binding, owner, container_id = _callback_binding(body)
    with runtime_state._lock_for(binding.team_id):
        if hosted_resources._cleanup_record(binding.team_id) is not None:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "OAuth Team teardown is pending")
        lease = hosted_resources._authorize(binding.team_id, ("account", owner))
        if lease.owner != owner or lease.container_id != container_id:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "OAuth Team authority changed")
        try:
            completion = runtime_state._oauth_integrations.complete(
                body["state"],
                body["code"],
                body["session_binding"],
                _current_integration_declaration,
            )
        except integration_service.OAuthIntegrationServiceError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.BAD_GATEWAY,
                "Assistant integration could not be completed",
            ) from exc
        if not _completion_matches(binding, completion):
            _compensate_oauth_completion(completion, owner)
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "OAuth Team authority changed")
        pending = runtime_state._integration_challenges.current(completion.team_id)
    response = {
        "connected": True,
        "team_id": completion.team_id,
        "assistant_id": completion.assistant_id,
        "integration_id": completion.integration_id,
        "provider": completion.provider,
        "scopes": list(completion.scopes),
        "challenge_id": pending.id if pending is not None else None,
    }
    return response, owner


def _disconnect_oauth_integration(
    team_id: str,
    assistant_id: str,
    integration_id: str,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    with runtime_state._lock_for(team_id), runtime_state._idle_team_chat(team_id):
        hosted_resources._require_current_authorization(team_id, lease, require_isolation=False)
        _current_integration_declaration(team_id, assistant_id, integration_id)
        hosted_chat_human.cancel_pending(team_id)
        hosted_chat_lifecycle.cancel_paused_integration(team_id)
        try:
            disconnected = runtime_state._oauth_integrations.disconnect(team_id, assistant_id, integration_id)
        except integration_service.OAuthIntegrationServiceError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Assistant integration could not be disconnected"
            ) from exc
    return {"disconnected": disconnected}


def _clear_assistant_stored_input(
    team_id: str,
    assistant_id: str,
    stored_input_id: str,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    with runtime_state._lock_for(team_id), runtime_state._idle_team_chat(team_id):
        hosted_resources._require_current_authorization(team_id, lease, require_isolation=False)
        try:
            current_id, spec = assistant_lifecycle._resolve_team_assistant(team_id, assistant_id)
        except assistant_registry.AssistantSpecError as exc:
            raise runtime_state.ApiError(HTTPStatus.NOT_FOUND, "Assistant is not installed") from exc
        if stored_input_id not in spec.contract.stored_inputs:
            raise runtime_state.ApiError(HTTPStatus.NOT_FOUND, "Assistant Stored Input is not declared")
        try:
            cleared = runtime_state._assistant_stored_inputs.delete(team_id, current_id, stored_input_id)
        except action_stored_input.StoredInputStoreError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant Stored Input state is unavailable",
            ) from exc
    return {
        "team_id": team_id,
        "assistant_id": current_id,
        "stored_input_id": stored_input_id,
        "cleared": cleared,
    }


def _resume_chat_integrations(
    team_id: str,
    challenge_id: object,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    with _exclusive_chat_turn(team_id, lease) as (token, container):

        def inspect(challenge: object) -> chat_turn_engine.IntegrationResumeContext:
            pending = getattr(challenge, "payload", None)
            if not isinstance(pending, hosted_assistants._PendingHostedChat):
                raise AssertionError("invalid hosted integration continuation")
            _, assistants, _files, _config, _key, _generation, current_identity = (
                hosted_chat_segment._hosted_chat_setup(
                    team_id,
                    list(pending.file_ids),
                    pending.assistant_ids,
                    container,
                    lease.owner,
                )
            )
            bindings = {active.assistant_id: active for active in assistants}
            return chat_turn_engine.IntegrationResumeContext(
                current_identity,
                hosted_assistants._integration_bindings(bindings),
                pending.continuation.turn.actions,
            )

        admission = chat_turn_engine.admit_integration_resume(
            chat_turn_engine.IntegrationResumeStrategy(
                store=runtime_state._integration_challenges,
                team_id=team_id,
                challenge_id=challenge_id,
                pending_valid=lambda pending: (
                    isinstance(pending, hosted_assistants._PendingHostedChat) and pending.owner == lease.owner
                ),
                pending_identity=lambda pending: pending.identity,
                inspect=inspect,
                integration_store=runtime_state._assistant_integrations,
                challenge_response=hosted_chat_segment._hosted_integration_challenge_payload,
                expired_error=lambda: runtime_state.ApiError(
                    HTTPStatus.CONFLICT,
                    "Assistant integration request expired; retry the message",
                ),
                context_error=lambda: runtime_state.ApiError(
                    HTTPStatus.CONFLICT,
                    "Team capabilities changed; retry",
                ),
                contract_error=lambda: runtime_state.ApiError(
                    HTTPStatus.CONFLICT, "Assistant integration contract is unavailable"
                ),
                # The turn holds the Team's only execution slot, so no other turn can have paused since, and no OAuth
                # start can issue state meanwhile; the drifted turn ends with the OAuth state started for it.
                end_drifted=lambda _challenge: hosted_chat_lifecycle.cancel_paused_integration(team_id),
            )
        )
        if admission.response is not None:
            return admission.response
        pending = admission.pending
        if not isinstance(pending, hosted_assistants._PendingHostedChat):
            raise AssertionError("shared integration resume returned invalid state")

        return hosted_chat_segment.continue_paused(team_id, token, container, lease.owner, pending, pending)


def _resume_chat_human(
    team_id: str,
    body: object,
    assurance: dict[str, str] | None,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    return hosted_chat_human.resume_chat_human(
        team_id,
        body,
        assurance,
        lease,
        _exclusive_chat_turn,
    )


def _stop_active_action(team_id: str, token: str | None) -> bool:
    if token is None:
        return False
    with runtime_state._active_chat_guard:
        active = runtime_state._active_action_container_ids.get(team_id)
    if active is None or active[0] != token:
        return False
    try:
        assistant_container = runtime_state._docker.containers.get(active[1])
    except docker.errors.NotFound:
        return True
    except docker.errors.DockerException as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE, "active Assistant Action could not be inspected"
        ) from exc
    hosted_assistants._fail_stop_action(team_id, assistant_container)
    return True


def _interrupt_turn(team_id: str, token: str | None, brain_abort: object | None) -> bool:
    """Abort the cancelled turn's Brain request and fail-stop its executing Action; report whether one was stopped."""
    # The token is already cancelled, so the aborted Brain request resolves as a stopped turn, never a failure.
    if brain_abort is not None:
        brain_abort.abort()
    return _stop_active_action(team_id, token)


def _stop_chat(team_id: str, lease: hosted_resources._AuthorizationLease) -> dict:
    """Cancel one Controller-owned turn and fail-stop an Action already executing."""
    with runtime_state._lock_for(team_id):
        container = hosted_resources._require_current_authorization(team_id, lease)
        # The token is cancelled before any challenge is withdrawn: a pause commits only while its token is current, so
        # it either committed already and its challenge is withdrawn below, or its commit fails and rolls it back.
        with runtime_state._active_chat_guard:
            token = runtime_state._active_chat_tokens.get(team_id)
            if token is not None and runtime_state._active_chat_container_ids.get(team_id) != container.id:
                raise runtime_state.ApiError(HTTPStatus.NOT_FOUND, f"team {team_id!r} not found")
            if token is not None:
                runtime_state._cancelled_chat_tokens.add(token)
            brain_abort = runtime_state._brain_aborts.get(token) if token is not None else None
        # Only after the lease proves this exact generation may its pending continuations end. A failed cleanup, or a
        # Team runtime that cannot be inspected or is not running, is still reported, but only after the cancelled turn
        # is interrupted: it must never keep an Action running.
        try:
            integration_cancelled = hosted_chat_lifecycle.cancel_paused_integration(team_id)
            human_cancelled = hosted_chat_human.cancel_pending(team_id)
        finally:
            action_stopped = _interrupt_turn(team_id, token, brain_abort)
        container.reload()
        if container.status != "running":
            raise runtime_state.ApiError(
                HTTPStatus.CONFLICT, f"team {team_id!r} is not running (status={container.status})"
            )
    accepted = token is not None or integration_cancelled or human_cancelled
    return {
        "team_id": team_id,
        "requested": accepted,
        "accepted": accepted,
        # An executing Action is synchronously terminated. An in-flight Brain request is aborted, and any
        # late result is discarded before any subsequent Action or terminal reply.
        "confirmed": action_stopped,
        "forced_restart": False,
    }
