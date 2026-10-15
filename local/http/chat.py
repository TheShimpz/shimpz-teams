"""Local Team chat routes.

A Supervisor session starts a turn, reads and answers its pending human and Integration requests, opens the pending
human challenge in one interface language (ADR-0091), stops the turn, and asks for the Brain's capability plan and
intent route. A turn and its answers stream metadata-only progress before their one terminal record.
"""

from http import HTTPStatus

from chat import progress as chat_progress
from chat import turn as chat_turn_engine
from core.http import strict as strict_http
from local.errors import ApiProblemError as ApiProblem
from local.http import stream as local_http_stream
from local.http.audit import RequestAudit
from local.http.dispatch import Operation, team_operation
from local.validation import validate_team_id
from protocol.http.v1 import turn as http_turn

MAX_BODY_BYTES = 16 * 1024
MAX_CHAT_BODY_BYTES = 128 * 1024
MAX_CAPABILITY_PLAN_BODY_BYTES = http_turn.MAX_CAPABILITY_PLAN_BODY_BYTES
MAX_INTENT_ROUTE_BODY_BYTES = http_turn.MAX_INTENT_ROUTE_BODY_BYTES
MAX_HUMAN_RESPONSE_BODY_BYTES = 128 * 1024
STREAMED_OPERATIONS = frozenset({"chat", "chat-human-submit", "chat-integration-submit"})
BODY_LIMITS = {
    "chat": MAX_CHAT_BODY_BYTES,
    "chat-capability-plan": MAX_CAPABILITY_PLAN_BODY_BYTES,
    "chat-intent-route": MAX_INTENT_ROUTE_BODY_BYTES,
    "chat-integration-submit": MAX_BODY_BYTES,
    "chat-human-submit": MAX_HUMAN_RESPONSE_BODY_BYTES,
    "chat-human-open": MAX_BODY_BYTES,
    "chat-stop": MAX_BODY_BYTES,
}


def _status(payload: dict[str, object]) -> HTTPStatus:
    return (
        HTTPStatus.PRECONDITION_REQUIRED
        if payload.get("status") in chat_turn_engine.CHAT_PAUSED_STATUSES
        else HTTPStatus.OK
    )


def _start(handler, team_id: str, progress: chat_progress.Reporter) -> tuple[HTTPStatus, dict[str, object]]:
    provider, api_key = handler._model_credential_headers()
    body = handler._body(max_bytes=MAX_CHAT_BODY_BYTES)
    payload = handler.server.controller.chat_turn_service.chat(team_id, body, provider, api_key, progress)
    return _status(payload), payload


# Each streamed answer to a paused turn and the chat turn service method that resumes it.
_SUBMISSIONS = {"chat-human-submit": "resume_chat_human", "chat-integration-submit": "resume_chat_integrations"}


def _submit(
    handler, operation: str, team_id: str, progress: chat_progress.Reporter
) -> tuple[HTTPStatus, dict[str, object]]:
    resume = getattr(handler.server.controller.chat_turn_service, _SUBMISSIONS[operation])
    provider, api_key = handler._model_credential_headers()
    payload = resume(team_id, handler._body(max_bytes=BODY_LIMITS[operation]), provider, api_key, progress)
    return _status(payload), payload


def _open(handler, team_id: str) -> dict[str, object]:
    """Open the pending human challenge in the Admin interface language its request copy renders in (ADR-0091)."""
    service = handler.server.controller.chat_turn_service
    return service.open_chat_human(team_id, handler._body(max_bytes=BODY_LIMITS["chat-human-open"]))


def _stop(handler, team_id: str) -> dict[str, object]:
    if handler._body(max_bytes=BODY_LIMITS["chat-stop"]) != {}:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "chat stop requires an empty object", code="invalid-body")
    return handler.server.controller.chat_turn_service.stop_chat(team_id)


def _capability_plan(handler, team_id: str) -> dict[str, object]:
    provider, api_key = handler._model_credential_headers()
    body = handler._body(max_bytes=BODY_LIMITS["chat-capability-plan"])
    return handler.server.controller.chat_turn_service.capability_plan(team_id, body, provider, api_key)


def _intent_route(handler, team_id: str) -> dict[str, object]:
    provider, api_key = handler._model_credential_headers()
    decision_key = handler._decision_key("chat-intent-route")
    body = handler._body(max_bytes=BODY_LIMITS["chat-intent-route"])
    return handler.server.controller.chat_turn_service.intent_route(team_id, body, provider, api_key, decision_key)


# The non-streamed chat operations; a turn and the answers to it stream instead.
OPERATIONS: dict[str, Operation] = {
    "chat-human-pending": team_operation(
        lambda handler, team_id: handler.server.controller.chat_turn_service.pending_chat_human(team_id)
    ),
    "chat-integration-pending": team_operation(
        lambda handler, team_id: handler.server.controller.chat_turn_service.pending_chat_integrations(team_id)
    ),
    "chat-human-open": team_operation(_open),
    "chat-stop": team_operation(_stop),
    "chat-capability-plan": team_operation(_capability_plan),
    "chat-intent-route": team_operation(_intent_route),
}


def stream(handler, route: strict_http.ControllerRouteMatch, request_audit: RequestAudit) -> None:
    """Run a turn or one answer to it, streaming its progress before the terminal record."""
    team_id = validate_team_id(route.params["team_id"])

    def execute(reporter: chat_progress.Reporter) -> tuple[HTTPStatus, dict[str, object]]:
        if route.operation == "chat":
            return _start(handler, team_id, reporter)
        return _submit(handler, route.operation, team_id, reporter)

    local_http_stream.respond(handler, route.operation, team_id, request_audit, execute)
