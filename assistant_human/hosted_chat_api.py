"""Hosted Team chat API, continuation, OAuth, and cancellation operations."""

from __future__ import annotations

import contextlib
import secrets
from http import HTTPStatus

import docker.errors

import audit
from assistant import spec as assistant_registry
from assistant_human import hosted_assistants, hosted_chat_segment
from chat import turn as chat_turn_engine
from container_policy import hosted_resources
from http_boundary import runtime_state
from integrations import challenges as integration_challenges
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
    with runtime_state._active_chat_guard:
        runtime_state._active_chat_tokens[team_id] = token
        runtime_state._active_chat_container_ids[team_id] = container.id
    try:
        yield token, container
    finally:
        with runtime_state._active_chat_guard:
            runtime_state._active_chat_tokens.pop(team_id, None)
            runtime_state._active_chat_container_ids.pop(team_id, None)
            runtime_state._active_power_container_ids.pop(team_id, None)
            runtime_state._cancelled_chat_tokens.discard(token)
        lock.release()


def _chat(
    team_id: str,
    message: str,
    file_ids: object,
    assistant_ids: tuple[str, ...],
    lease: hosted_resources._AuthorizationLease,
) -> dict:
    """Run one bounded Team turn across the explicit Controller-brokered Assistant scope."""
    pending = _pending_hosted_chat(team_id)
    if pending is not None:
        return pending
    # The slot comes first. A losing concurrent request must not run even the local credential probe,
    # much less provider status or a second provider CLI.
    with _exclusive_chat_turn(team_id, lease) as (token, container):
        pending = _pending_hosted_chat(team_id)
        if pending is not None:
            return pending
        return hosted_chat_segment._chat_in_turn(
            team_id,
            message,
            file_ids,
            assistant_ids,
            token,
            container,
            lease.owner,
        )


def _pending_hosted_chat(team_id: str) -> dict[str, object] | None:
    integration = runtime_state._integration_challenges.current(team_id)
    if integration is not None:
        return hosted_chat_segment._hosted_integration_challenge_payload(integration)
    return None


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
    session_binding: object,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
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
        authorization_url = runtime_state._oauth_integrations.authorization_url(challenge, session_binding)
    except integration_service.OAuthIntegrationUnavailableError as exc:
        raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Assistant integrations are already configured") from exc
    except integration_service.OAuthIntegrationServiceError as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Assistant integration could not be started",
        ) from exc
    return {"authorization_url": authorization_url}


def _complete_oauth_integration(
    body: object,
    principal: tuple[str, str | None],
) -> dict[str, object]:
    if not isinstance(body, dict) or set(body) != {"state", "code", "session_binding"}:
        raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "OAuth callback is invalid")
    try:
        completion = runtime_state._oauth_integrations.complete(
            body["state"],
            body["code"],
            body["session_binding"],
            _current_integration_declaration,
        )
    except integration_service.OAuthIntegrationServiceError as exc:
        raise runtime_state.ApiError(HTTPStatus.BAD_GATEWAY, "Assistant integration could not be completed") from exc
    try:
        hosted_resources._authorize(completion.team_id, principal)
    except Exception:
        with contextlib.suppress(integration_service.OAuthIntegrationServiceError):
            runtime_state._oauth_integrations.disconnect(
                completion.team_id,
                completion.assistant_id,
                completion.integration_id,
            )
        raise
    pending = runtime_state._integration_challenges.current(completion.team_id)
    return {
        "connected": True,
        "team_id": completion.team_id,
        "assistant_id": completion.assistant_id,
        "integration_id": completion.integration_id,
        "provider": completion.provider,
        "scopes": list(completion.scopes),
        "challenge_id": pending.id if pending is not None else None,
    }


@runtime_state._serialize_against_team_chat
def _disconnect_oauth_integration(
    team_id: str,
    assistant_id: str,
    integration_id: str,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    with runtime_state._lock_for(team_id):
        hosted_resources._require_current_authorization(team_id, lease, require_isolation=False)
        _current_integration_declaration(team_id, assistant_id, integration_id)
        runtime_state._integration_challenges.cancel_team(team_id)
        try:
            disconnected = runtime_state._oauth_integrations.disconnect(team_id, assistant_id, integration_id)
        except integration_service.OAuthIntegrationServiceError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE, "Assistant integration could not be disconnected"
            ) from exc
    return {"disconnected": disconnected}


def _resume_chat_integrations(
    team_id: str,
    challenge_id: object,
    lease: hosted_resources._AuthorizationLease,
) -> dict[str, object]:
    with _exclusive_chat_turn(team_id, lease) as (token, container):

        def inspect(pending: object) -> chat_turn_engine.IntegrationResumeContext:
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
                pending.continuation.turn.powers,
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
            )
        )
        if admission.response is not None:
            return admission.response
        pending = admission.pending
        if not isinstance(pending, hosted_assistants._PendingHostedChat):
            raise AssertionError("shared integration resume returned invalid state")

        segment = hosted_chat_segment._run_hosted_chat_segment(
            hosted_chat_segment.HostedChatSegmentRequest(
                team_id=team_id,
                file_ids=list(pending.file_ids),
                assistant_ids=pending.assistant_ids,
                token=token,
                container=container,
                owner=lease.owner,
                continuation=pending.continuation,
                expected_identity=pending.identity,
            )
        )
        return hosted_chat_segment._hosted_segment_response(
            team_id,
            token,
            segment,
            pending.assistant_ids,
            pending.file_ids,
            pending.owner,
        )


def _stop_active_power(team_id: str, token: str | None) -> bool:
    if token is None:
        return False
    with runtime_state._active_chat_guard:
        active = runtime_state._active_power_container_ids.get(team_id)
    if active is None or active[0] != token:
        return False
    try:
        assistant_container = runtime_state._docker.containers.get(active[1])
    except docker.errors.NotFound:
        return True
    except docker.errors.DockerException as exc:
        raise runtime_state.ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE, "active Assistant Power could not be inspected"
        ) from exc
    hosted_assistants._fail_stop_power(team_id, assistant_container)
    return True


def _stop_chat(team_id: str, lease: hosted_resources._AuthorizationLease) -> dict:
    """Cancel one Controller-owned turn and fail-stop a Power already executing."""
    integration_cancelled = runtime_state._integration_challenges.cancel_team(team_id)
    with runtime_state._lock_for(team_id):
        container = hosted_resources._require_current_authorization(team_id, lease)
        container.reload()
        if container.status != "running":
            raise runtime_state.ApiError(
                HTTPStatus.CONFLICT, f"team {team_id!r} is not running (status={container.status})"
            )
        with runtime_state._active_chat_guard:
            token = runtime_state._active_chat_tokens.get(team_id)
            if token is not None and runtime_state._active_chat_container_ids.get(team_id) != container.id:
                raise runtime_state.ApiError(HTTPStatus.NOT_FOUND, f"team {team_id!r} not found")
            if token is not None:
                runtime_state._cancelled_chat_tokens.add(token)
        power_stopped = _stop_active_power(team_id, token)
    accepted = token is not None or integration_cancelled
    audit.log("chat_stop", team_id, result="ok" if accepted else "denied")
    return {
        "team_id": team_id,
        "requested": accepted,
        "accepted": accepted,
        # An executing Power is synchronously terminated. A provider HTTP request is only marked
        # cancelled; its result is discarded before any subsequent Power or terminal reply.
        "confirmed": power_stopped,
        "forced_restart": False,
    }
