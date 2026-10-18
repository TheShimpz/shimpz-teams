"""The Browser screen view over HTTP: opening the running task's view and sending its frames (ADR-0117, M2 Watch)."""

from http import HTTPStatus

from core.http import strict as strict_http
from local.browser.runtime import BrowserError
from local.errors import ApiProblemError as ApiProblem
from local.http.audit import RequestAudit
from local.http.dispatch import team_operation
from local.validation import validate_team_id
from protocol.http.v1 import browser_view as view_protocol

FRAME_OPERATION = "browser-view-frame"
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


def send_frame(handler, route: strict_http.ControllerRouteMatch, request_audit: RequestAudit) -> None:
    """Send the watched task's next frame, a PNG Team built, numbered in its header; 204 when none came in time.

    The request holds one of the Space's frame slots until its answer is sent.
    """
    team_id = validate_team_id(route.params["team_id"])
    view_id = route.params["view_id"]
    after = view_protocol.canonical_cursor(route.params["after"])
    if view_protocol.VIEW_ID_RE.fullmatch(view_id) is None or after is None:
        raise _problem(BrowserError(view_protocol.NOT_RUNNING))
    service = handler.server.controller.browser_service
    try:
        with service.view_slot():
            frame = service.view_frame(team_id, view_id, after)
            if frame is None:
                request_audit.record(FRAME_OPERATION, result="ok", team_id=team_id, detail="unchanged")
                handler.send_response(HTTPStatus.NO_CONTENT)
                handler.send_private_headers()
                handler.end_headers()
                return
            framing = view_protocol.canonical_frame({"frame": frame.number, "length": len(frame.png)})
            if framing is None or not frame.png.startswith(view_protocol.PNG_SIGNATURE):
                raise _problem(BrowserError(view_protocol.UNAVAILABLE))
            request_audit.record(FRAME_OPERATION, result="ok", team_id=team_id, detail=f"frame={frame.number}")
            handler._send_image(frame.png, "image/png", {view_protocol.FRAME_HEADER: str(frame.number)})
    except BrowserError as exc:
        raise _problem(exc) from exc
