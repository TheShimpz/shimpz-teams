"""Bounded HTTP adapter for the local Team controller."""

import base64
import contextlib
import json
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from typing import override

from docker.errors import DockerException

from action import human as action_human
from core.http import strict as strict_http
from inference import usage as brain_usage
from integrations import broker as integration_broker
from local import activity as local_activity
from local import audit as local_audit
from local import authority as local_authority
from local.errors import ApiProblemError as ApiProblem
from local.errors import oauth_authorization_invalid, supervisor_unavailable
from local.http import chat as local_http_chat
from local.http import deadline as http_deadline
from local.http import dispatch as local
from local.http import inference as local_http_inference
from local.http import routine as local_http_routine
from local.http.audit import RequestAudit
from local.http.dispatch import Operation, Result, team_operation
from local.validation import (
    MODEL_BOUND_OPERATIONS,
    credential_binding,
    decision_binding,
    validate_decision_credential_header,
    validate_model_credential_headers,
    validate_team_id,
)
from protocol.http.v1 import content as http_content
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import preview as http_preview

MAX_BODY_BYTES = 16 * 1024
MAX_API_RESPONSE_BYTES = 128 * 1024
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FILE_BODY_BYTES = MAX_UPLOAD_BYTES
REQUEST_TIMEOUT_SECONDS = 10
_FILE_UPLOAD_SLOTS = threading.BoundedSemaphore(1)
_MACHINE_ONLY_OPERATIONS = frozenset(
    {"health", "activity", "space-bootstrap-reset", "assistant-integration-complete"}
    | local_http_routine.MACHINE_OPERATIONS
)
# A route whose answer may outgrow the API cap, with its own allowance.
_RESPONSE_LIMITS = {
    **local_http_routine.RESPONSE_LIMITS,
    "assistant-registry": http_payload.MAX_TEAM_REGISTRY_BYTES,
}
_READ_METHODS = frozenset({"GET", "HEAD"})
_JSON_BODY_LIMITS = {
    "supervisor-key-rotate": MAX_BODY_BYTES,
    "assistant-install": MAX_BODY_BYTES,
    "local-assistant-fresh-install": MAX_BODY_BYTES,
    "local-assistant-install": MAX_BODY_BYTES,
    "assistant-integration-authorize": MAX_BODY_BYTES,
    "assistant-integration-cancel": MAX_BODY_BYTES,
    "assistant-integration-complete": MAX_BODY_BYTES,
    "assistant-invoke": MAX_BODY_BYTES,
    "inference-configure": MAX_BODY_BYTES,
    "team-create": MAX_BODY_BYTES,
    "team-destroy": MAX_BODY_BYTES,
    "team-rename": MAX_BODY_BYTES,
    **local_http_chat.BODY_LIMITS,
    **local_http_routine.BODY_LIMITS,
}


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, controller: object, token: str) -> None:
        super().__init__(address, handler)
        self.controller = controller
        self.token = token
        self.activity = local_activity.Activity()
        self._slots = threading.BoundedSemaphore(16)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(http_deadline.DeadlineRequestHandler):
    server: BoundedServer
    protocol_version = "HTTP/1.1"
    # The per-read idle timeout; the deadline handler bounds headers and body absolutely on top of it.
    timeout = REQUEST_TIMEOUT_SECONDS
    # The resolved route's response allowance; anything sent before a route resolves keeps the API cap.
    _response_limit = MAX_API_RESPONSE_BYTES
    # The Supervisor evidence that authorized this request, once a session assertion verified.
    _supervisor_evidence: local_authority.Evidence | None = None

    def log_message(self, *_args) -> None:
        return

    def _authorized(self) -> bool:
        return strict_http.bearer_matches(self.headers, self.server.token)

    def _send(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
        if len(encoded) > self._response_limit:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            encoded = b'{"error":"response exceeded its limit"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if status == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", 'Bearer realm="shimpz-local"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _send_image(self, contents: bytes, media_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(HTTPStatus.OK)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(contents)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(contents)

    def _body(self, *, max_bytes: int = MAX_BODY_BYTES) -> dict[str, object]:
        raw = getattr(self, "_captured_json_raw", None)
        body = getattr(self, "_captured_json_body", None)
        if not isinstance(raw, bytes) or not isinstance(body, dict) or len(raw) > max_bytes:
            raise ApiProblem(
                HTTPStatus.BAD_REQUEST,
                "request body is unavailable",
                code="invalid-body",
            )
        return body

    def _file_body(self) -> tuple[str, bytes, str]:
        metadata = getattr(self, "_captured_file_metadata", None)
        if not isinstance(metadata, strict_http.FileUploadMetadata):
            raise ApiProblem(
                HTTPStatus.BAD_REQUEST,
                "file upload metadata is unavailable",
                code="invalid-file",
            )
        try:
            content = strict_http.read_file_content(self.rfile, metadata)
        except strict_http.HttpContractError as exc:
            raise ApiProblem(exc.status, exc.message, code=exc.code) from exc
        return metadata.filename, content, metadata.media_type

    def _capture_body(self, operation: str) -> dict[str, object]:
        self._captured_json_raw = None
        self._captured_json_body = None
        self._captured_file_metadata = None
        try:
            captured = strict_http.capture_body(
                self.headers, self.rfile, operation, _JSON_BODY_LIMITS, MAX_FILE_BODY_BYTES
            )
        except strict_http.HttpContractError as exc:
            raise ApiProblem(exc.status, exc.message, code=exc.code) from exc
        self._captured_json_raw = captured.raw
        self._captured_json_body = captured.document
        self._captured_file_metadata = captured.file
        return captured.binding

    def _team_name_body(self) -> object:
        """Team create, rename, and delete each carry exactly one Team name; Team validates it."""
        body = self._body()
        if set(body) != {"team_name"}:
            raise ApiProblem(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "the request requires only team_name",
                code="invalid-body",
            )
        return body["team_name"]

    def _install_body(self) -> tuple[str, str]:
        body = self._body()
        if (
            set(body) != {"assistant_id", "source_digest"}
            or not isinstance(body["assistant_id"], str)
            or not isinstance(body["source_digest"], str)
        ):
            raise ApiProblem(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "Assistant installation requires assistant_id and source_digest",
                code="invalid-body",
            )
        return body["assistant_id"], body["source_digest"]

    def _local_install_body(self) -> str:
        body = self._body()
        if set(body) != {"image_id"} or not isinstance(body["image_id"], str):
            raise ApiProblem(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "Local Assistant installation requires image_id",
                code="invalid-body",
            )
        return body["image_id"]

    def decision_key(self, operation: str) -> str | None:
        return validate_decision_credential_header(
            self.headers.get_all("X-Shimpz-Decision-Api-Key", failobj=[]), operation
        )

    def model_credential_headers(self) -> tuple[str, str]:
        return validate_model_credential_headers(
            self.headers.get_all("X-Shimpz-Model-Provider", failobj=[]),
            self.headers.get_all("X-Shimpz-Model-Api-Key", failobj=[]),
        )

    def _resolved_route(self) -> tuple[list[str], strict_http.ControllerRouteMatch]:
        try:
            target = strict_http.parse_request_target(self.path)
        except strict_http.HttpContractError as exc:
            raise ApiProblem(exc.status, exc.message, code=exc.code) from exc
        parts = list(target.parts)
        canonical_path = "/" + "/".join(parts)
        if target.path != canonical_path:
            raise ApiProblem(
                HTTPStatus.BAD_REQUEST,
                "request path must be canonical",
                code="invalid-path",
            )
        route = strict_http.resolve_controller_route(self.command, tuple(parts))
        if route is None:
            raise ApiProblem(
                HTTPStatus.NOT_FOUND,
                "route not found",
                code="route-not-found",
            )
        return parts, route

    def _model_binding(self, operation: str) -> dict[str, str] | None:
        if operation not in MODEL_BOUND_OPERATIONS:
            return None
        return credential_binding(*self.model_credential_headers())

    def _route(self, route: strict_http.ControllerRouteMatch) -> Result:
        """Dispatch one resolved, authorized operation; the route table and this operation table name the same set."""
        return _OPERATIONS[route.operation](self, route)

    def _invoke_directly(self, team_id: str, assistant_id: str, action: str) -> dict[str, object]:
        """Run one Action outside a turn; a human request it raises has no chat to answer it (ADR-0038)."""
        try:
            return self.server.controller.invoke(team_id, assistant_id, action, self._body())
        except action_human.HumanRequestSuspensionError as exc:
            local_audit.record_request(
                "assistant-action",
                result="denied",
                team_id=team_id,
                assistant=assistant_id,
                detail=f"human-request-required:{action}",
            )
            raise ApiProblem(
                HTTPStatus.CONFLICT,
                "the Action asks a person to answer, which only a Team chat can collect",
                code="action-human-request-required",
            ) from exc

    def _authorized_route(
        self,
        request_audit: RequestAudit,
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        parts, route = self._resolved_route()
        request_audit.operation = route.operation
        self._response_limit = _RESPONSE_LIMITS.get(route.operation, MAX_API_RESPONSE_BYTES)
        decision = decision_binding(self.decision_key(route.operation))
        if route.operation in _MACHINE_ONLY_OPERATIONS:
            self._capture_body(route.operation)
            request_audit.machine()
            request_audit.record("machine-authority", result="ok")
            with local_audit.bind_request_principal(request_audit.principal()):
                return self._route(route)
        if route.operation == local_http_routine.RUN_OPERATION:
            local_http_routine.run(self, parts, route, request_audit)
            return None

        assertion_state = local_authority.credential_state(self.headers)
        request_audit.absent(assertion_state)
        body = self._capture_body(route.operation)
        model = self._model_binding(route.operation)
        assurance = self._expected_human_assurance(route.operation, route.params)
        try:
            evidence = local_authority.verify(
                self.headers,
                request=local_authority.RequestBinding(
                    method=self.command,
                    path="/" + "/".join(parts),
                    body=body,
                    model=model,
                    decision=decision,
                    assurance=assurance,
                    authority_kinds=(
                        frozenset({"session", "host-reset"})
                        if route.operation == "space-reset"
                        else frozenset({"session"})
                    ),
                ),
            )
        except local_authority.SupervisorDeniedError as exc:
            if assertion_state == "assertion_present":
                request_audit.credential_state = "assertion_rejected"
            request_audit.record("human-authority", result="denied", detail="invalid-supervisor")
            raise ApiProblem(
                HTTPStatus.FORBIDDEN,
                "Supervisor authority is required",
                code="invalid-supervisor",
            ) from exc
        except local_authority.SupervisorUnavailableError as exc:
            request_audit.record("human-authority", result="error", detail="supervisor-unavailable")
            raise ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Supervisor authority is unavailable",
                code="supervisor-unavailable",
            ) from exc
        request_audit.human(evidence)
        self._supervisor_evidence = evidence
        request_audit.record("human-authority", result="ok")
        with local_audit.bind_request_principal(request_audit.principal()):
            return self._session_route(route, request_audit)

    def _session_route(
        self,
        route: strict_http.ControllerRouteMatch,
        request_audit: RequestAudit,
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        """Dispatch a request a Supervisor session authorized; streamed and binary responses are sent here."""
        if route.operation in local_http_chat.STREAMED_OPERATIONS:
            local_http_chat.stream(self, route, request_audit)
            return None
        if route.operation in local_http_routine.STREAMED_OPERATIONS:
            local_http_routine.stream(self, route, request_audit)
            return None
        if route.operation == "assistant-icon":
            team_id = validate_team_id(route.params["team_id"])
            assistant_id = route.params["assistant_id"]
            contents = self.server.controller.assistant_icon(team_id, assistant_id)
            request_audit.record("assistant-icon", result="ok", team_id=team_id, assistant=assistant_id)
            self._send_image(contents, "image/png")
            return None
        if route.operation in {"local-assistant-icon", "local-assistant-summary", "local-assistant-details"}:
            self._local_assistant_preview(route, request_audit)
            return None
        if route.operation in {"file-preview", "file-page-preview", "file-content"}:
            self._file_bytes(route, request_audit)
            return None
        return self._route(route)

    def _file_bytes(self, route: strict_http.ControllerRouteMatch, request_audit: RequestAudit) -> None:
        """Send one listed file's bytes to the Supervisor's browser: a sanitized rendition, or the verified original."""
        if route.operation == "file-content":
            self._file_content(route, request_audit)
        else:
            self._file_preview(route, request_audit)

    def _file_preview(self, route: strict_http.ControllerRouteMatch, request_audit: RequestAudit) -> None:
        """Send one listed file's sanitized rendition, or one PDF page's with its page count, framed exactly.

        A busy preview says when to retry.
        """
        team_id = validate_team_id(route.params["team_id"])
        paged = route.operation == "file-page-preview"
        page = http_preview.canonical_page(route.params.get("page")) if paged else None
        if paged and page is None:
            raise ApiProblem(HTTPStatus.NOT_FOUND, "page not found", code="file-not-found")
        controller = self.server.controller
        try:
            if paged:
                rendition = controller.file_preview(team_id, route.params["file_id"], page)
            else:
                rendition = controller.file_preview(team_id, route.params["file_id"])
        except ApiProblem as exc:
            if exc.code != "file-preview-busy":
                raise
            self._send_busy(route.operation, exc, request_audit)
            return
        framing = {"media_type": rendition.media_type, "length": len(rendition.data)}
        admitted = (
            http_preview.canonical_page_framing({**framing, "page_count": rendition.page_count})
            if paged
            else http_preview.canonical_framing(framing)
        )
        if admitted is None or not rendition.data.startswith(http_preview.SIGNATURES[rendition.media_type]):
            raise ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "the file preview failed its framing",
                code="file-preview-invalid",
            )
        request_audit.record(route.operation, result="ok", team_id=team_id)
        extra = {http_preview.PAGE_COUNT_HEADER: str(rendition.page_count)} if paged else {}
        self._send_image(rendition.data, rendition.media_type, extra)

    def _file_content(self, route: strict_http.ControllerRouteMatch, request_audit: RequestAudit) -> None:
        """Send one listed file's verified original as an octet stream framed by its exact length and SHA-256.

        The release is audited, by file id and size only, before its first byte leaves. A busy download says when to
        retry.
        """
        team_id = validate_team_id(route.params["team_id"])

        def deliver(metadata: dict[str, object], content: bytes) -> None:
            framing = http_content.canonical_framing({"length": len(content), "sha256": metadata["sha256"]})
            if framing is None:
                raise ApiProblem(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "the file content failed its framing",
                    code="file-content-invalid",
                )
            request_audit.record(
                route.operation, result="ok", team_id=team_id, detail=f"file={metadata['id']} bytes={len(content)}"
            )
            digest = base64.b64encode(bytes.fromhex(str(metadata["sha256"]))).decode("ascii")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Repr-Digest", f"sha-256=:{digest}:")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(content)

        try:
            self.server.controller.file_content(team_id, route.params["file_id"], deliver)
        except ApiProblem as exc:
            if exc.code != "file-download-busy":
                raise
            self._send_busy(route.operation, exc, request_audit)

    def _send_busy(self, operation: str, exc: ApiProblem, request_audit: RequestAudit) -> None:
        trace_id = request_audit.record(operation, result="error", detail=exc.code)
        self._send(
            exc.status,
            {"error": exc.message, "code": exc.code, "retry_after_ms": 250, "trace_id": trace_id},
        )

    def _local_assistant_preview(self, route: strict_http.ControllerRouteMatch, request_audit: RequestAudit) -> None:
        """Send one staged snapshot's icon, localized summary, or localized page; a busy preview says when to retry."""
        image_id = f"sha256:{route.params['image_hash']}"
        controller = self.server.controller
        try:
            if route.operation == "local-assistant-icon":
                contents = controller.local_snapshot_icon(image_id)
            elif route.operation == "local-assistant-summary":
                document = controller.local_snapshot_summary(image_id, route.params["locale"])
            else:
                document = controller.local_snapshot_details(image_id, route.params["locale"])
        except ApiProblem as exc:
            if exc.code != "local-assistant-preview-busy":
                raise
            self._send_busy(route.operation, exc, request_audit)
            return
        trace_id = request_audit.record(route.operation, result="ok")
        if route.operation == "local-assistant-icon":
            self._send_image(contents, "image/png")
        else:
            self._send(HTTPStatus.OK, {**document, "trace_id": trace_id})

    def _expected_human_assurance(
        self,
        operation: str,
        params: dict[str, str],
    ) -> dict[str, str] | None:
        if operation == "routine-human-submit":
            return local_http_routine.expected_assurance(self, params)
        if operation != "chat-human-submit":
            return None

        def current() -> object:
            team_id = validate_team_id(params.get("team_id"))
            service = self.server.controller.chat_turn_service
            service._expire_human_challenges()
            return service.human_challenges.current(team_id)

        body = self._body(max_bytes=local_http_chat.MAX_HUMAN_RESPONSE_BODY_BYTES)
        return local_http_routine.human_assurance(body, current)

    def _handle(self) -> None:
        self.close_connection = True
        request_audit = RequestAudit()
        if not self._authorized():
            request_audit.absent("machine_bearer_rejected")
            trace_id = request_audit.record("authentication", result="denied", detail="invalid-bearer")
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "authentication required", "trace_id": trace_id})
            return

        mutating = self.command not in _READ_METHODS
        work = self.server.activity.working() if mutating else contextlib.nullcontext()
        with work, brain_usage.metered():
            try:
                local.dispatch_route(
                    lambda: self._authorized_route(request_audit),
                    request_audit.record,
                    self._send,
                    ApiProblem,
                    DockerException,
                )
            finally:
                # Opened before the in-flight count drops, so a Supervisor's work never reads as idle in between.
                if mutating and request_audit.principal_class == "human":
                    self.server.activity.supervised()

    @override
    def do_GET(self) -> None:
        self._handle()

    @override
    def do_POST(self) -> None:
        self._handle()

    @override
    def do_DELETE(self) -> None:
        self._handle()

    @override
    def do_HEAD(self) -> None:
        self._handle()

    @override
    def do_OPTIONS(self) -> None:
        self._handle()

    @override
    def do_PATCH(self) -> None:
        self._handle()

    @override
    def do_PUT(self) -> None:
        self._handle()


def _ok(route: strict_http.ControllerRouteMatch, payload: dict[str, object]) -> Result:
    return HTTPStatus.OK, payload, route.operation, None, None


def _space_bootstrap_reset(handler: Handler, route: strict_http.ControllerRouteMatch) -> Result:
    """Reset a Space whose Admin publishes no Supervisor key, and forget Team's pin of an earlier one with it."""
    try:
        local_authority.require_supervisor_absent()
    except local_authority.SupervisorEstablishedError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "Supervisor authority is already established",
            code="supervisor-established",
        ) from exc
    except local_authority.SupervisorUnavailableError as exc:
        raise supervisor_unavailable() from exc
    result = handler.server.controller.reset_space()
    try:
        local_authority.forget_supervisor_key()
    except local_authority.SupervisorUnavailableError as exc:
        raise supervisor_unavailable() from exc
    return _ok(route, result)


def _supervisor_key_rotate(handler: Handler, route: strict_http.ControllerRouteMatch) -> Result:
    try:
        signed_by = "" if handler._supervisor_evidence is None else handler._supervisor_evidence.key_sha256
        rotated = local_authority.rotate_supervisor_key(handler._body(), signed_by)
    except local_authority.SupervisorDeniedError as exc:
        raise ApiProblem(
            HTTPStatus.CONFLICT,
            "the Supervisor key rotation is invalid or no longer current",
            code="supervisor-key-rotation-refused",
        ) from exc
    except local_authority.SupervisorUnavailableError as exc:
        raise supervisor_unavailable() from exc
    local_audit.record_request("supervisor-key-rotate", result="ok", detail=rotated["key_sha256"][:16])
    return _ok(route, rotated)


def _oauth_callback(handler: Handler, route: strict_http.ControllerRouteMatch) -> Result:
    body = handler._body()
    if set(body) != {"state", "claim", "session_binding"}:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "OAuth callback is invalid", code="invalid-body")
    result = handler.server.controller.chat_turn_service.complete_cloudflare_oauth_callback(
        state=body["state"],
        claim=body["claim"],
        session_binding=body["session_binding"],
    )
    return _ok(route, result)


def _local_install(install: str) -> Operation:
    """Install one staged Local snapshot through the controller's ``install`` method, audited under its Assistant."""

    def operation(handler: Handler, route: strict_http.ControllerRouteMatch) -> Result:
        team_id = validate_team_id(route.params["team_id"])
        result = getattr(handler.server.controller, install)(team_id, handler._local_install_body())
        return HTTPStatus.OK, result, route.operation, team_id, str(result["assistant"])

    return operation


def _file_upload(handler: Handler, team_id: str) -> dict[str, object]:
    if not _FILE_UPLOAD_SLOTS.acquire(blocking=False):
        raise ApiProblem(
            HTTPStatus.TOO_MANY_REQUESTS,
            "another Team file upload is in progress",
            code="file-upload-busy",
        )
    try:
        filename, content, media_type = handler._file_body()
        return handler.server.controller.put_file(team_id, filename, content, media_type)
    finally:
        _FILE_UPLOAD_SLOTS.release()


def _integration_authorize(handler: Handler, route: strict_http.ControllerRouteMatch) -> Result:
    team_id = validate_team_id(route.params["team_id"])
    body = handler._body()
    if set(body) != {"assistant_id", "integration_id", "callback_mode", "session_binding"}:
        raise oauth_authorization_invalid()
    if body["callback_mode"] not in integration_broker.CALLBACK_MODES:
        raise oauth_authorization_invalid()
    result = handler.server.controller.chat_turn_service.start_assistant_integration_authorization(
        team_id,
        route.params["challenge_id"],
        body["assistant_id"],
        body["integration_id"],
        body["session_binding"],
        body["callback_mode"],
    )
    return HTTPStatus.OK, result, route.operation, team_id, None


def _integration_cancel(handler: Handler, team_id: str) -> dict[str, object]:
    body = handler._body()
    if set(body) != {"session_binding"}:
        raise ApiProblem(HTTPStatus.UNPROCESSABLE_ENTITY, "OAuth cancellation is invalid", code="invalid-body")
    return handler.server.controller.chat_turn_service.cancel_assistant_integration_authorization(
        body["session_binding"]
    )


def _assistant_install(handler: Handler, route: strict_http.ControllerRouteMatch) -> Result:
    team_id = validate_team_id(route.params["team_id"])
    assistant_id, source_digest = handler._install_body()
    result = handler.server.controller.install_publication(team_id, assistant_id, source_digest)
    return HTTPStatus.OK, result, route.operation, team_id, assistant_id


def _assistant_operation(
    call: Callable[[Handler, strict_http.ControllerRouteMatch, str, str], dict[str, object]],
) -> Operation:
    """An operation on one Assistant of the route's Team, audited under that Team and Assistant."""

    def operation(handler: Handler, route: strict_http.ControllerRouteMatch) -> Result:
        team_id = validate_team_id(route.params["team_id"])
        assistant_id = route.params["assistant_id"]
        return HTTPStatus.OK, call(handler, route, team_id, assistant_id), route.operation, team_id, assistant_id

    return operation


_SERVER_OPERATIONS: dict[str, Operation] = {
    "health": lambda handler, route: _ok(route, handler.server.controller.health()),
    "activity": lambda handler, route: _ok(route, {"state": handler.server.activity.state()}),
    "registry-list": lambda handler, route: _ok(route, handler.server.controller.list_registry()),
    "team-list": lambda handler, route: _ok(route, handler.server.controller.list_teams()),
    "space-reset": lambda handler, route: _ok(route, handler.server.controller.reset_space()),
    "space-bootstrap-reset": _space_bootstrap_reset,
    "supervisor-key-rotate": _supervisor_key_rotate,
    "assistant-integration-complete": _oauth_callback,
    "team-create": team_operation(
        lambda handler, team_id: handler.server.controller.create_team(team_id, handler._team_name_body())
    ),
    "team-destroy": team_operation(
        lambda handler, team_id: handler.server.controller.destroy_team(team_id, handler._team_name_body())
    ),
    "team-rename": team_operation(
        lambda handler, team_id: handler.server.controller.rename_team(team_id, handler._team_name_body())
    ),
    "local-assistant-list": lambda handler, route: _ok(route, handler.server.controller.list_local_snapshots()),
    "local-assistant-install": _local_install("install_local_snapshot"),
    "local-assistant-fresh-install": _local_install("install_fresh_local_snapshot"),
    "file-list": team_operation(lambda handler, team_id: handler.server.controller.list_files(team_id)),
    "file-upload": team_operation(_file_upload),
    "assistant-integration-list": team_operation(
        lambda handler, team_id: handler.server.controller.chat_turn_service.list_assistant_integrations(team_id)
    ),
    "assistant-integration-authorize": _integration_authorize,
    "assistant-integration-cancel": team_operation(_integration_cancel),
    "assistant-stored-input-list": team_operation(
        lambda handler, team_id: handler.server.controller.chat_turn_service.list_assistant_stored_inputs(team_id)
    ),
    "assistant-stored-input-clear": _assistant_operation(
        lambda handler, route, team_id, assistant_id: (
            handler.server.controller.chat_turn_service.clear_assistant_stored_input(
                team_id, assistant_id, route.params["stored_input_id"]
            )
        )
    ),
    "assistant-list": team_operation(lambda handler, team_id: handler.server.controller.list_assistants(team_id)),
    "assistant-registry": team_operation(lambda handler, team_id: handler.server.controller.team_registry(team_id)),
    "assistant-install": _assistant_install,
    "assistant-summary": _assistant_operation(
        lambda handler, route, team_id, assistant_id: handler.server.controller.assistant_summary(
            team_id, assistant_id, route.params["locale"]
        )
    ),
    "assistant-details": _assistant_operation(
        lambda handler, route, team_id, assistant_id: handler.server.controller.assistant_details(
            team_id, assistant_id, route.params["locale"]
        )
    ),
    "assistant-uninstall": _assistant_operation(
        lambda handler, _route, team_id, assistant_id: (
            handler.server.controller.assistant_lifecycle.uninstall_assistant(team_id, assistant_id)
        )
    ),
    "assistant-invoke": _assistant_operation(
        lambda handler, route, team_id, assistant_id: handler._invoke_directly(
            team_id, assistant_id, route.params["action_id"]
        )
    ),
}
_ROUTINE_OPERATIONS: dict[str, Operation] = {
    route.operation: local_http_routine.route
    for route in strict_http.CONTROLLER_ROUTES
    if route.operation.startswith("routine-")
    and route.operation not in local_http_routine.STREAMED_OPERATIONS
    and route.operation != local_http_routine.RUN_OPERATION
}
_OPERATIONS: dict[str, Operation] = {
    **_SERVER_OPERATIONS,
    **local_http_chat.OPERATIONS,
    **local_http_inference.OPERATIONS,
    **_ROUTINE_OPERATIONS,
}
