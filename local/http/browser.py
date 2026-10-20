"""The Browser over HTTP: its watched screen, a person's control of it, and a Team's Sign-ins (ADR-0117).

A stream request is one exact WebSocket upgrade. Team admits it only for the view of the very Browser it names, holds
one of the Space's stream slots for the whole stream, and answers 101 only after the display itself answered the same
upgrade exactly; before that, every refusal is a closed JSON answer, and after it the stream just ends. A control
stream is the person's own control of a Browser that offers it (a sign-in session).

A person adds logins to the Team's one Sign-in in a sign-in session and Saves it; Admin reads the Sign-in and deletes
it after the Supervisor's second factor (ADR-0117, 2026-10-20 amendment). Every body and answer has the exact form of
`protocol/http/v1/sign_in.py`.
"""

from http import HTTPStatus

from core.http import strict as strict_http
from local.browser import sign_in as local_sign_in
from local.browser import sign_ins as sign_in_store
from local.browser.runtime import BrowserError
from local.errors import ApiProblemError as ApiProblem
from local.http.audit import RequestAudit
from local.http.dispatch import MAX_BODY_BYTES, team_operation
from local.validation import validate_team_id
from protocol.http.v1 import browser_view as view_protocol
from protocol.http.v1 import sign_in as sign_in_protocol

STREAM_OPERATION = "browser-view-stream"
CONTROL_OPERATION = "browser-view-control"
BODY_LIMITS = {
    "browser-sign-in-start": MAX_BODY_BYTES,
    "browser-sign-in-save": MAX_BODY_BYTES,
    "browser-sign-in-cancel": MAX_BODY_BYTES,
    "browser-sign-in-delete": MAX_BODY_BYTES,
}
# Operations the journal never blocks (ADR-0120): a Cancel and a deletion protect the Team, and a Save's own flushed
# request decides whether it delegates, while its committed answer must still reach the Supervisor.
JOURNAL_TOLERANT = frozenset({"browser-sign-in-save", "browser-sign-in-cancel", "browser-sign-in-delete"})
_SIGN_IN_MESSAGES = {
    sign_in_protocol.NOT_RUNNING: (HTTPStatus.NOT_FOUND, "the sign-in session is not running"),
    sign_in_protocol.URL_INVALID: (HTTPStatus.BAD_REQUEST, "the sign-in URL is not admissible"),
    sign_in_protocol.BUSY: (HTTPStatus.CONFLICT, "the Team's Sign-in is in use or being deleted"),
    sign_in_protocol.CHANGED: (HTTPStatus.CONFLICT, "the Team's Sign-in changed since it was confirmed"),
    sign_in_protocol.ABSENT: (HTTPStatus.NOT_FOUND, "the Team has no Sign-in"),
    sign_in_protocol.WINDOWS: (HTTPStatus.CONFLICT, "close every window but one before saving"),
    sign_in_protocol.IDENTITY_PROVIDER: (
        HTTPStatus.CONFLICT,
        "the profile holds a session at an identity provider; sign out of it before saving",
    ),
    sign_in_protocol.UNSAVED: (HTTPStatus.SERVICE_UNAVAILABLE, "the Sign-in could not be saved"),
    "browser-busy": (HTTPStatus.CONFLICT, "the Space's Browser is busy"),
    "browser-unavailable": (HTTPStatus.SERVICE_UNAVAILABLE, "the Browser is unavailable"),
    local_sign_in.TEAM_NOT_FOUND: (HTTPStatus.NOT_FOUND, "the Team does not exist"),
}
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


def _sign_in_problem(exc: local_sign_in.SignInSessionError) -> ApiProblem:
    code = exc.code if exc.code in _SIGN_IN_MESSAGES else "browser-unavailable"
    status, message = _SIGN_IN_MESSAGES[code]
    return ApiProblem(status, message, code=code)


def _body(handler, admit) -> dict[str, object]:
    body = admit(handler.body())
    if body is None:
        raise ApiProblem(HTTPStatus.BAD_REQUEST, "the request body is not admissible", code="invalid-body")
    return body


def _incarnation(handler, team_id: str) -> local_sign_in.Incarnation:
    """The Team's current incarnation, its network's id, read anew each time; None once it is gone or unknowable."""

    def current() -> str | None:
        try:
            network = handler.server.controller.assistant_lifecycle._network(team_id, required=False)
        except ApiProblem:
            return None
        return None if network is None else network.id

    return current


def _start(handler, team_id: str) -> dict[str, object]:
    body = _body(handler, sign_in_protocol.canonical_start)
    # A Team that does not exist, or a Docker that cannot say, is answered as such before any Browser starts.
    handler.server.controller.assistant_lifecycle._network(team_id)
    try:
        return handler.server.controller.sign_in_sessions.start(
            team_id, _incarnation(handler, team_id), body["url"], body["locale"]
        )
    except local_sign_in.SignInSessionError as exc:
        raise _sign_in_problem(exc) from exc


def _save(handler, team_id: str) -> dict[str, object]:
    body = _body(handler, sign_in_protocol.canonical_session)
    sessions = handler.server.controller.sign_in_sessions
    try:
        return sessions.save(team_id, body["sign_in_session_id"], _incarnation(handler, team_id))
    except local_sign_in.SignInSessionError as exc:
        raise _sign_in_problem(exc) from exc


def _cancel(handler, team_id: str) -> dict[str, object]:
    body = _body(handler, sign_in_protocol.canonical_session)
    try:
        handler.server.controller.sign_in_sessions.cancel(team_id, body["sign_in_session_id"])
    except local_sign_in.SignInSessionError as exc:
        raise _sign_in_problem(exc) from exc
    return {"cancelled": True}


def _current(handler, team_id: str) -> dict[str, object]:
    try:
        return {"sign_in": handler.server.controller.sign_in_sessions.current(team_id)}
    except sign_in_store.SignInError as exc:
        raise ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE, "the Sign-in is unavailable", code="sign-ins-unavailable"
        ) from exc


def _delete(handler, team_id: str) -> dict[str, object]:
    body = _body(handler, sign_in_protocol.canonical_delete)
    try:
        return {"deletion": handler.server.controller.sign_in_sessions.delete(team_id, body)}
    except local_sign_in.SignInSessionError as exc:
        raise _sign_in_problem(exc) from exc


OPERATIONS = {
    "browser-view-open": team_operation(_open),
    "browser-sign-in-start": team_operation(_start),
    "browser-sign-in-save": team_operation(_save),
    "browser-sign-in-cancel": team_operation(_cancel),
    "browser-sign-in": team_operation(_current),
    "browser-sign-in-delete": team_operation(_delete),
}


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
    """Carry one viewer's stream of the watched Browser's screen, or a person's control of it, until it ends."""
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
        request_audit.record(route.operation, result="ok", team_id=team_id, detail="upgraded")

    try:
        with service.view_slot():
            service.carry_view(
                team_id, view_id, handler.connection, key, answer, control=route.operation == CONTROL_OPERATION
            )
    except BrowserError as exc:
        if not upgraded:
            raise _problem(exc) from exc
    finally:
        # The connection carried a stream: nothing else is ever read from it.
        handler.close_connection = True
