"""Local Team-controller HTTP dispatch and stable failure projection."""

from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from core.http import stdlib
from core.http import strict as strict_http
from local.validation import validate_team_id

# The bound of an ordinary JSON request body; an operation that needs more names its own limit.
MAX_BODY_BYTES = 16 * 1024
# The bound of one answer to a pending human request, from a chat turn or a Routine run alike.
MAX_HUMAN_RESPONSE_BODY_BYTES = 128 * 1024
# One answered operation: its status, payload, and the operation, Team, and Assistant it is audited under.
type Result = tuple[HTTPStatus, dict[str, object], str, str | None, str | None]
# Every resolved operation is dispatched through one table from its operation name to one of these.
type Operation = Callable[[Any, strict_http.ControllerRouteMatch], Result]


def team_operation(call: Callable[[Any, str], dict[str, object]]) -> Operation:
    """An operation answering ``call(handler, team_id)`` for the route's validated Team, audited under that Team."""

    def operation(handler, route: strict_http.ControllerRouteMatch) -> Result:
        team_id = validate_team_id(route.params["team_id"])
        return HTTPStatus.OK, call(handler, team_id), route.operation, team_id, None

    return operation


def failure_payload(failure: stdlib.HttpFailure, trace_id: str) -> dict[str, object]:
    """The public body of one failed operation: its safe message, its trace, and its stable code when it has one."""
    payload: dict[str, object] = {"error": failure.public_message, "trace_id": trace_id}
    if failure.public_code is not None:
        payload["code"] = failure.public_code
    return payload


def classify_failure(
    exc: Exception,
    problem_type: type[Exception],
    docker_error_type: type[Exception] | tuple[type[Exception], ...],
) -> stdlib.HttpFailure:
    """Project one controller exception without exposing its payload."""
    if isinstance(exc, problem_type):
        return stdlib.HttpFailure(
            exc.status,
            exc.message,
            exc.code,
            "denied" if exc.status < 500 else "error",
            exc.code,
        )
    if isinstance(exc, docker_error_type):
        return stdlib.HttpFailure(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Docker is unavailable",
            "docker-error",
            "error",
        )
    return stdlib.HttpFailure(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        "internal error",
        "internal-error",
        "error",
    )


def dispatch_route(
    route,
    record,
    send,
    problem_type: type[Exception],
    docker_error_type: type[Exception] | tuple[type[Exception], ...],
) -> None:
    operation = "request"
    team_id = None
    assistant_id = None

    def action() -> None:
        nonlocal operation, team_id, assistant_id
        result = route()
        if result is None:
            return
        status, payload, operation, team_id, assistant_id = result
        trace_id = record(operation, result="ok", team_id=team_id, assistant=assistant_id)
        payload["trace_id"] = trace_id
        send(status, payload)

    def classify(exc: Exception) -> stdlib.HttpFailure:
        return classify_failure(exc, problem_type, docker_error_type)

    def emit(failure: stdlib.HttpFailure) -> None:
        trace_id = record(
            operation,
            result=failure.result,
            team_id=team_id,
            assistant=assistant_id,
            detail=failure.audit_reason,
        )
        send(failure.status, failure_payload(failure, trace_id))

    stdlib.dispatch(
        action,
        classify=classify,
        emit=emit,
        unexpected_message="internal error",
    )
