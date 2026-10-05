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
egress_proxy_unavailable = _problem(_UNAVAILABLE, "Assistant egress proxy is unavailable", "egress-proxy-unavailable")
egress_proxy_drift = _problem(
    _CONFLICT, "Assistant egress proxy failed its Team attachment contract", "egress-proxy-drift"
)
team_destroy_failed = _problem(_UNAVAILABLE, "Docker could not destroy the Team", "docker-remove-failed")
assistant_replace_failed = _problem(_UNAVAILABLE, "Docker could not replace the Assistant", "docker-remove-failed")
