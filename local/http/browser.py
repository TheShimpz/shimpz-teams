"""The Browser's watched screen over HTTP: opening the running task's view and carrying its stream (ADR-0117).

A stream request is one exact WebSocket upgrade. Team admits it only for the view of the very task it names, holds one
of the Space's stream slots for the whole stream, and answers 101 only after the display itself answered the same
upgrade exactly; before that, every refusal is a closed JSON answer, and after it the stream just ends.
"""

from http import HTTPStatus

from core.http import strict as strict_http
from local.browser.runtime import BrowserError
from local.errors import ApiProblemError as ApiProblem
from local.http.audit import RequestAudit
from local.http.dispatch import team_operation
from local.validation import validate_team_id
from protocol.http.v1 import browser_view as view_protocol

STREAM_OPERATION = "browser-view-stream"
_MESSAGES = {
    view_protocol.NOT_RUNNING: (HTTPStatus.NOT_FOUND, "the Browser task is not running"),
    view_protocol.BUSY: (HTTPStatus.SERVICE_UNAVAILABLE, "the Browser view is busy"),
    view_protocol.UNAVAILABLE: (HTTPStatus.SERVICE_UNAVAILABLE, "the Browser view is unavailable"),
}


def _problem(exc: BrowserError) -> ApiProblem:
    code = exc.code if exc.code in _MESSAGES else view_protocol.UNAVAILABLE
    status, message = _MESSAGES[code]
    return ApiProblem(status, message, code=code)


def _open(handler, team_id: str) -> dict[str, object]:
    try:
        return {"view_id": handler.server.controller.browser_service.open_view(team_id)}
    except BrowserError as exc:
        raise _problem(exc) from exc


OPERATIONS = {"browser-view-open": team_operation(_open)}


def upgrade_key(headers) -> str | None:
    """The request's WebSocket key when it is exactly one admitted upgrade, else None."""

    def one(name: str) -> str | None:
        values = headers.get_all(name, failobj=[])
        return values[0].strip() if len(values) == 1 else None

    connection = one("Connection")
    tokens = {token.strip().lower() for token in (connection or "").split(",")}
    if (
        (one("Upgrade") or "").lower() != "websocket"
        or "upgrade" not in tokens
        or one("Sec-WebSocket-Version") != view_protocol.WEBSOCKET_VERSION
        or one("Sec-WebSocket-Protocol") != view_protocol.SUBPROTOCOL
        or headers.get_all("Sec-WebSocket-Extensions", failobj=[])
    ):
        return None
    return view_protocol.canonical_key(one("Sec-WebSocket-Key"))


def send_stream(handler, route: strict_http.ControllerRouteMatch, request_audit: RequestAudit) -> None:
    """Carry one viewer's stream of the watched task's screen, holding a stream slot until it ends."""
    team_id = validate_team_id(route.params["team_id"])
    view_id = route.params["view_id"]
    if view_protocol.VIEW_ID_RE.fullmatch(view_id) is None:
        raise _problem(BrowserError(view_protocol.NOT_RUNNING))
    key = upgrade_key(handler.headers)
    if key is None:
        raise ApiProblem(HTTPStatus.BAD_REQUEST, "the stream upgrade is invalid", code=view_protocol.INVALID_UPGRADE)
    service = handler.server.controller.browser_service
    upgraded = False

    def answer(first: bytes) -> None:
        nonlocal upgraded
        handler.wfile.write(first)
        handler.wfile.flush()
        upgraded = True
        request_audit.record(STREAM_OPERATION, result="ok", team_id=team_id, detail="upgraded")

    try:
        with service.view_slot():
            service.carry_view(team_id, view_id, handler.connection, key, answer)
    except BrowserError as exc:
        if not upgraded:
            raise _problem(exc) from exc
    finally:
        # The connection carried a stream: nothing else is ever read from it.
        handler.close_connection = True
