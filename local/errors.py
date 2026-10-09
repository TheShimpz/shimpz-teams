"""Typed public failures shared by the local controller and HTTP adapter.

A failure the controller reports from more than one place is built here once, so it always carries one status,
message, and code; Admin localizes the code.
"""

import functools
from collections.abc import Callable
from http import HTTPStatus

_CONFLICT = HTTPStatus.CONFLICT
_NOT_FOUND = HTTPStatus.NOT_FOUND
_UNAVAILABLE = HTTPStatus.SERVICE_UNAVAILABLE
_INVALID = HTTPStatus.UNPROCESSABLE_ENTITY
_BAD_GATEWAY = HTTPStatus.BAD_GATEWAY


class ApiProblemError(RuntimeError):
    def __init__(self, status: HTTPStatus, message: str, *, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


def _problem(status: HTTPStatus, message: str, code: str) -> Callable[[], ApiProblemError]:
    return functools.partial(ApiProblemError, status, message, code=code)


stored_input_unavailable = _problem(
    _UNAVAILABLE, "Assistant Stored Input state is unavailable", "assistant-stored-input-state-unavailable"
)
team_context_changed = _problem(_CONFLICT, "Team capabilities changed; retry", "team-context-changed")
docker_unavailable = _problem(_UNAVAILABLE, "Docker is unavailable", "docker-unavailable")
ownership_conflict = _problem(_CONFLICT, "Team resource ownership conflict", "ownership-conflict")
team_resources_ownership_conflict = _problem(
    _CONFLICT, "Team resources failed their ownership contract", "ownership-conflict"
)
assistant_registry_drift = _problem(
    _CONFLICT, "an installed Assistant is no longer allowlisted", "assistant-registry-drift"
)
assistant_icon_unavailable = _problem(
    _UNAVAILABLE, "Assistant icon storage is unavailable", "assistant-icon-unavailable"
)
assistant_action_blocked = _problem(
    _UNAVAILABLE,
    "Assistant Action execution is blocked until this Assistant is reinstalled",
    "assistant-action-blocked",
)
conversation_state_unavailable = _problem(
    _UNAVAILABLE, "Team conversation state could not be deleted", "brain-runtime-failed"
)
integration_challenge_expired = _problem(
    _CONFLICT, "Assistant integration request expired; retry the message", "assistant-integration-challenge-expired"
)
chat_stopped = _problem(_CONFLICT, "chat turn stopped", "chat-stopped")
inference_not_configured = _problem(_CONFLICT, "Team model provider is not configured", "inference-not-configured")
inference_provider_mismatch = _problem(
    _CONFLICT, "configured model provider changed; retry", "inference-provider-mismatch"
)
action_state_unavailable = _problem(
    _UNAVAILABLE, "Team Action execution state is unavailable", "action-state-unavailable"
)
action_file_unavailable = _problem(
    _CONFLICT, "the attached file is unavailable for this Action; attach it again", "action-file-unavailable"
)
selected_file_not_found = _problem(_NOT_FOUND, "selected file not found", "file-not-found")
assistant_isolation_drift = _problem(
    _CONFLICT, "the installed Assistant failed its isolation profile", "assistant-isolation-drift"
)
integration_contract_unavailable = _problem(
    _CONFLICT, "Assistant integration contract is unavailable", "assistant-integration-contract-invalid"
)
team_destroy_failed = _problem(_UNAVAILABLE, "Docker could not destroy the Team", "docker-remove-failed")
assistant_replace_failed = _problem(_UNAVAILABLE, "Docker could not replace the Assistant", "docker-remove-failed")
chat_stop_timeout = _problem(_CONFLICT, "active Team chat did not stop in time", "chat-active")
routine_active = _problem(_CONFLICT, "Team is running a Routine", "routine-active")
chat_active = _problem(_CONFLICT, "Team already has an active chat turn", "chat-active")
invalid_locale = _problem(_INVALID, "locale must be one interface language", "invalid-locale")
assistant_not_installed = _problem(_NOT_FOUND, "Assistant is not installed in this Team", "assistant-not-installed")
assistant_manifest_unavailable = _problem(
    _UNAVAILABLE, "installed Assistant manifest could not be verified", "assistant-manifest-unavailable"
)
assistant_manifest_invalid = _problem(
    _CONFLICT, "installed Assistant manifest failed its reviewed contract", "assistant-manifest-invalid"
)
human_request_invalid = _problem(_CONFLICT, "Action human request changed; retry the message", "human-request-invalid")
human_request_expired = _problem(_CONFLICT, "Action human request expired; retry the message", "human-request-expired")
human_response_invalid = _problem(_INVALID, "Action human response is invalid", "invalid-body")
integration_oauth_unavailable = _problem(
    _BAD_GATEWAY,
    "Assistant integration authorization could not be completed",
    "assistant-integration-oauth-unavailable",
)
oauth_authorization_invalid = _problem(_INVALID, "OAuth authorization is invalid", "invalid-body")
space_resource_ownership_conflict = _problem(
    _CONFLICT, "a labeled Space resource failed its ownership contract", "ownership-conflict"
)
space_reset_failed = _problem(_UNAVAILABLE, "Docker could not reset the Space", "docker-reset-failed")
invalid_model_credential = _problem(_INVALID, "private model credential is invalid", "invalid-model-credential")
brain_turn_failed = _problem(_BAD_GATEWAY, "Brain could not complete the Team turn", "brain-runtime-failed")
human_response_mismatch = _problem(
    _INVALID, "Action human response does not match its request", "invalid-human-response"
)
challenge_locale_only = _problem(_INVALID, "opening a challenge requires only locale", "invalid-body")
empty_body_required = _problem(_INVALID, "request requires an empty object", "invalid-body")
routine_not_found = _problem(_NOT_FOUND, "Routine is unavailable", "routine-not-found")
routine_steps_not_found = _problem(_NOT_FOUND, "Routine steps are unavailable", "routine-steps-not-found")
routine_run_not_found = _problem(_NOT_FOUND, "Routine run is unavailable", "routine-run-not-found")
routine_run_steps_not_found = _problem(_NOT_FOUND, "Routine run steps are unavailable", "routine-run-steps-not-found")
routine_lease_invalid = _problem(_CONFLICT, "Routine run lease is not live", "routine-lease-invalid")
routine_incident_unavailable = _problem(_NOT_FOUND, "Routine incident is unavailable", "routine-incident-unavailable")
routine_incident_not_unresolved = _problem(
    _CONFLICT, "Routine incident is not unresolved", "routine-incident-unavailable"
)
routine_card_stale = _problem(_CONFLICT, "the recovery card is stale; open it again", "routine-card-stale")
routine_deliveries_invalid = _problem(_INVALID, "Routine deliveries are invalid", "invalid-body")
routine_diagnostics_unavailable = _problem(
    _UNAVAILABLE, "Routine diagnostics are unavailable", "routine-state-unavailable"
)
routine_state_delete_failed = _problem(
    _UNAVAILABLE, "Team Routine state could not be deleted", "routine-state-unavailable"
)
