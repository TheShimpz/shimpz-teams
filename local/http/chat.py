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
from local.validation import validate_team_id
from protocol.http.v1 import supervisor as supervisor_contract

MAX_BODY_BYTES = 16 * 1024
MAX_CHAT_BODY_BYTES = supervisor_contract.MAX_JSON_BODY_BYTES
MAX_CAPABILITY_PLAN_BODY_BYTES = 32 * 1024
MAX_INTENT_ROUTE_BODY_BYTES = 8 * 1024
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

type Result = tuple[HTTPStatus, dict[str, object], str, str | None, str | None]


def _status(payload: dict[str, object]) -> HTTPStatus:
    return (
        HTTPStatus.PRECONDITION_REQUIRED
        if payload.get("status") in chat_turn_engine.CHAT_PAUSED_STATUSES
        else HTTPStatus.OK
    )


def _start(handler, team_id: str, progress: chat_progress.Reporter | None = None) -> Result:
    provider, api_key = handler._model_credential_headers()
    body = handler._body(max_bytes=MAX_CHAT_BODY_BYTES)
    payload = handler.server.controller.chat_turn_service.chat(team_id, body, provider, api_key, progress)
    return _status(payload), payload, "chat", team_id, None


def _pending(handler, team_id: str, segment: str) -> Result | None:
    pending = {
        "human": ("pending_chat_human", "chat-human-pending"),
        "integrations": ("pending_chat_integrations", "chat-integration-pending"),
    }.get(segment)
    if pending is None:
        return None
    method_name, operation_name = pending
    operation = getattr(handler.server.controller.chat_turn_service, method_name)
    return HTTPStatus.OK, operation(team_id), operation_name, team_id, None


def _open(handler, team_id: str) -> Result:
    """Open the pending human challenge in the Admin interface language its request copy renders in (ADR-0091)."""
    service = handler.server.controller.chat_turn_service
    payload = service.open_chat_human(team_id, handler._body(max_bytes=BODY_LIMITS["chat-human-open"]))
    return HTTPStatus.OK, payload, "chat-human-open", team_id, None


def _submit(handler, team_id: str, segment: str, progress: chat_progress.Reporter | None = None) -> Result | None:
    submission = {
        "human": ("resume_chat_human", "chat-human-submit"),
        "integrations": ("resume_chat_integrations", "chat-integration-submit"),
    }.get(segment)
    if submission is None:
        return None
    method_name, operation_name = submission
    operation = getattr(handler.server.controller.chat_turn_service, method_name)
    provider, api_key = handler._model_credential_headers()
    payload = operation(team_id, handler._body(max_bytes=BODY_LIMITS[operation_name]), provider, api_key, progress)
    return _status(payload), payload, operation_name, team_id, None


def _stop(handler, team_id: str) -> Result:
    if handler._body(max_bytes=BODY_LIMITS["chat-stop"]) != {}:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "chat stop requires an empty object", code="invalid-body")
    return HTTPStatus.OK, handler.server.controller.chat_turn_service.stop_chat(team_id), "chat-stop", team_id, None


def _decision(handler, team_id: str, segment: str) -> Result | None:
    decision = {
        "capability-plan": ("capability_plan", "chat-capability-plan"),
        "intent-route": ("intent_route", "chat-intent-route"),
    }.get(segment)
    if decision is None:
        return None
    method_name, operation_name = decision
    operation = getattr(handler.server.controller.chat_turn_service, method_name)
    provider, api_key = handler._model_credential_headers()
    credentials = (
        (provider, api_key, handler._decision_key(operation_name)) if segment == "intent-route" else (provider, api_key)
    )
    body = handler._body(max_bytes=BODY_LIMITS[operation_name])
    return HTTPStatus.OK, operation(team_id, body, *credentials), operation_name, team_id, None


def _control(handler, team_id: str, segment: str) -> Result | None:
    """The non-streamed chat controls: Stop, and opening the pending human challenge in one language."""
    control = {"stop": _stop, "human/challenge": _open}.get(segment)
    return control(handler, team_id) if control is not None else None


def route(handler, parts: list[str]) -> Result | None:
    """Dispatch one non-streamed chat request, or None when the path or method names no chat operation."""
    if len(parts) not in {4, 5, 6} or parts[:2] != ["v1", "teams"] or parts[3] != "chat":
        return None
    team_id = validate_team_id(parts[2])
    if len(parts) == 4:
        return _start(handler, team_id) if handler.command == "POST" else None
    segment = "/".join(parts[4:])
    if handler.command == "GET":
        return _pending(handler, team_id, segment)
    if handler.command != "POST":
        return None
    return (
        _control(handler, team_id, segment)
        or _decision(handler, team_id, segment)
        or _submit(handler, team_id, segment)
    )


def stream(
    handler,
    parts: list[str],
    route: strict_http.ControllerRouteMatch,
    request_audit: RequestAudit,
) -> None:
    """Run a turn or one answer to it, streaming its progress before the terminal record."""
    team_id = validate_team_id(route.params["team_id"])

    def execute(reporter: chat_progress.Reporter) -> tuple[HTTPStatus, dict[str, object]]:
        if route.operation == "chat":
            status, payload, *_audit = _start(handler, team_id, reporter)
        else:
            status, payload, *_audit = _submit(handler, team_id, parts[4], reporter)
        return status, payload

    local_http_stream.respond(handler, route.operation, team_id, request_audit, execute)
