"""Bounded HTTP adapter for the local Team controller."""

import contextlib
import hashlib
import json
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from docker.errors import DockerException

from chat import progress as chat_progress
from chat import turn as chat_turn_engine
from core.http import stdlib
from core.http import strict as strict_http
from integrations import broker as integration_broker
from local import audit as local_audit
from local import authority as local_authority
from local.errors import ApiProblemError as ApiProblem
from local.http import dispatch as local
from local.validation import (
    validate_model_credential_headers,
    validate_team_id,
    validate_team_name,
)
from protocol.http.v1 import progress as progress_contract
from protocol.http.v1 import supervisor as supervisor_contract

MAX_BODY_BYTES = 16 * 1024
MAX_CHAT_BODY_BYTES = 24 * 1024
MAX_API_RESPONSE_BYTES = 128 * 1024
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FILE_BODY_BYTES = MAX_UPLOAD_BYTES
MAX_PATH_BYTES = 512
REQUEST_TIMEOUT_SECONDS = 10
_FILE_UPLOAD_SLOTS = threading.BoundedSemaphore(1)
_MACHINE_ONLY_OPERATIONS = frozenset({"health", "assistant-integration-complete"})
_JSON_BODY_LIMITS = {
    "assistant-install": MAX_BODY_BYTES,
    "assistant-integration-authorize": MAX_BODY_BYTES,
    "assistant-integration-cancel": MAX_BODY_BYTES,
    "assistant-integration-complete": MAX_BODY_BYTES,
    "assistant-invoke": MAX_BODY_BYTES,
    "chat": MAX_CHAT_BODY_BYTES,
    "chat-integration-submit": MAX_BODY_BYTES,
    "chat-stop": MAX_BODY_BYTES,
    "inference-configure": MAX_BODY_BYTES,
    "team-create": MAX_BODY_BYTES,
}


@dataclass(slots=True)
class _RequestAudit:
    operation: str = "request"
    principal_id: str | None = None
    principal_class: str = "absent"
    credential_state: str = "machine_bearer_present"
    trace_id: str | None = None

    def machine(self) -> None:
        self.principal_id = "admin"
        self.principal_class = "machine"
        self.credential_state = "machine_bearer_present"

    def human(self, evidence: local_authority.Evidence) -> None:
        self.principal_id = evidence.supervisor_id
        self.principal_class = "human"
        self.credential_state = "assertion_present"

    def absent(self, credential_state: str) -> None:
        self.principal_id = None
        self.principal_class = "absent"
        self.credential_state = credential_state

    def principal(self) -> local_audit.AuditPrincipal:
        return local_audit.AuditPrincipal(
            principal_id=self.principal_id,
            principal_class=self.principal_class,
            credential_state=self.credential_state,
            trace_id=self.trace_id,
        )

    def record(
        self,
        operation: str,
        *,
        result: str,
        team_id: str | None = None,
        assistant: str | None = None,
        detail: str | None = None,
    ) -> str:
        selected_operation = self.operation if operation == "request" else operation
        self.trace_id = local_audit.record(
            selected_operation,
            result=result,
            principal=self.principal(),
            team_id=team_id,
            assistant=assistant,
            detail=detail,
        )
        return self.trace_id


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, controller: object, token: str) -> None:
        super().__init__(address, handler)
        self.controller = controller
        self.token = token
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


class Handler(BaseHTTPRequestHandler):
    server: BoundedServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        return

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def _authorized(self) -> bool:
        return strict_http.bearer_matches(self.headers, self.server.token)

    def _send(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_API_RESPONSE_BYTES:
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
            if operation == "file-upload":
                metadata = strict_http.file_upload_metadata(
                    self.headers,
                    max_bytes=MAX_FILE_BODY_BYTES,
                )
                self._captured_file_metadata = metadata
                return {
                    "kind": "file",
                    "length": metadata.length,
                    "filename": metadata.filename,
                    "media_type": metadata.media_type,
                }
            limit = _JSON_BODY_LIMITS.get(operation)
            if limit is not None:
                raw, body = strict_http.read_json_document(
                    self.headers,
                    self.rfile,
                    max_bytes=limit,
                )
                self._captured_json_raw = raw
                self._captured_json_body = body
                return {
                    "kind": "json",
                    "length": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            strict_http.reject_body(self.headers)
        except strict_http.HttpContractError as exc:
            raise ApiProblem(exc.status, exc.message, code=exc.code) from exc
        return {
            "kind": "none",
            "length": 0,
            "sha256": supervisor_contract.EMPTY_SHA256,
        }

    def _team_create_body(self) -> str:
        body = self._body()
        if set(body) != {"team_name"}:
            raise ApiProblem(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "Team creation requires only team_name",
                code="invalid-body",
            )
        return validate_team_name(body["team_name"])

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

    def _model_credential_headers(self) -> tuple[str, str]:
        return validate_model_credential_headers(
            self.headers.get_all("X-Shimpz-Model-Provider", failobj=[]),
            self.headers.get_all("X-Shimpz-Model-Api-Key", failobj=[]),
        )

    def _resolved_route(self) -> tuple[list[str], strict_http.ControllerRouteMatch]:
        try:
            target = strict_http.parse_request_target(
                self.path,
                allow_query=False,
                max_bytes=MAX_PATH_BYTES,
            )
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
        route = strict_http.resolve_controller_route(
            strict_http.LOCAL_CONTROLLER,
            self.command,
            tuple(parts),
        )
        if route is None:
            raise ApiProblem(
                HTTPStatus.NOT_FOUND,
                "route not found",
                code="route-not-found",
            )
        return parts, route

    def _model_binding(self, operation: str) -> dict[str, str] | None:
        if operation not in {"chat", "chat-integration-submit"}:
            return None
        provider, api_key = self._model_credential_headers()
        return {
            "provider": provider,
            "key_sha256": hashlib.sha256(api_key.encode("ascii")).hexdigest(),
        }

    def _fixed_route(
        self, parts: list[str]
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        controller = self.server.controller
        if self.command == "GET" and parts == ["healthz"]:
            return HTTPStatus.OK, controller.health(), "health", None, None
        if self.command == "GET" and parts == ["v1", "assistants"]:
            return HTTPStatus.OK, controller.list_registry(), "registry-list", None, None
        if self.command == "GET" and parts == ["v1", "teams"]:
            return HTTPStatus.OK, controller.list_teams(), "team-list", None, None
        if self.command == "DELETE" and parts == ["v1", "space"]:
            return HTTPStatus.OK, controller.reset_space(), "space-reset", None, None
        if self.command == "POST" and parts == ["v1", "oauth", "cloudflare", "callback"]:
            body = self._body()
            if set(body) != {"state", "claim", "session_binding"}:
                raise ApiProblem(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "OAuth callback is invalid",
                    code="invalid-body",
                )
            result = controller.chat_turn_service.complete_cloudflare_oauth_callback(
                state=body["state"],
                claim=body["claim"],
                session_binding=body["session_binding"],
            )
            return HTTPStatus.OK, result, "assistant-integration-complete", None, None
        return None

    def _file_route(self, parts: list[str]) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        if len(parts) not in {4, 5} or parts[:2] != ["v1", "teams"] or parts[3] != "files":
            return None
        controller = self.server.controller
        team_id = validate_team_id(parts[2])
        if len(parts) == 4 and self.command == "GET":
            return HTTPStatus.OK, controller.list_files(team_id), "file-list", team_id, None
        if len(parts) == 4 and self.command == "POST":
            if not _FILE_UPLOAD_SLOTS.acquire(blocking=False):
                raise ApiProblem(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    "another Team file upload is in progress",
                    code="file-upload-busy",
                )
            try:
                filename, content, media_type = self._file_body()
                return (
                    HTTPStatus.OK,
                    controller.put_file(team_id, filename, content, media_type),
                    "file-upload",
                    team_id,
                    None,
                )
            finally:
                _FILE_UPLOAD_SLOTS.release()
        if len(parts) == 5 and self.command == "DELETE":
            return (
                HTTPStatus.OK,
                controller.delete_file(team_id, parts[4]),
                "file-delete",
                team_id,
                None,
            )
        return None

    def _inference_route(
        self, parts: list[str]
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        if len(parts) != 4 or parts[:2] != ["v1", "teams"] or parts[3] != "inference":
            return None
        team_id = validate_team_id(parts[2])
        if self.command == "GET":
            return (
                HTTPStatus.OK,
                self.server.controller.inference_status(team_id),
                "inference-status",
                team_id,
                None,
            )
        if self.command == "PUT":
            return (
                HTTPStatus.OK,
                self.server.controller.configure_inference(team_id, self._body()),
                "inference-configure",
                team_id,
                None,
            )
        return None

    @staticmethod
    def _chat_status(payload: dict[str, object]) -> HTTPStatus:
        return (
            HTTPStatus.PRECONDITION_REQUIRED
            if payload.get("status") in chat_turn_engine.CHAT_PAUSED_STATUSES
            else HTTPStatus.OK
        )

    def _chat_start(
        self,
        team_id: str,
        progress: chat_progress.Reporter | None = None,
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None]:
        provider, api_key = self._model_credential_headers()
        body = self._body(max_bytes=MAX_CHAT_BODY_BYTES)
        payload = self.server.controller.chat_turn_service.chat(
            team_id,
            body,
            provider,
            api_key,
            progress,
        )
        return self._chat_status(payload), payload, "chat", team_id, None

    def _chat_pending(
        self,
        team_id: str,
        segment: str,
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        pending = {
            "integrations": ("pending_chat_integrations", "chat-integration-pending"),
        }.get(segment)
        if pending is None:
            return None
        method_name, operation_name = pending
        operation = getattr(self.server.controller.chat_turn_service, method_name)
        return HTTPStatus.OK, operation(team_id), operation_name, team_id, None

    def _chat_submit(
        self,
        team_id: str,
        segment: str,
        progress: chat_progress.Reporter | None = None,
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        submission = {
            "integrations": ("resume_chat_integrations", "chat-integration-submit", MAX_BODY_BYTES),
        }.get(segment)
        if submission is None:
            return None
        method_name, operation_name, max_bytes = submission
        operation = getattr(self.server.controller.chat_turn_service, method_name)
        provider, api_key = self._model_credential_headers()
        payload = operation(
            team_id,
            self._body(max_bytes=max_bytes),
            provider,
            api_key,
            progress,
        )
        return self._chat_status(payload), payload, operation_name, team_id, None

    def _chat_stop(
        self,
        team_id: str,
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None]:
        if self._body() != {}:
            raise ApiProblem(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "chat stop requires an empty object",
                code="invalid-body",
            )
        return (
            HTTPStatus.OK,
            self.server.controller.chat_turn_service.stop_chat(team_id),
            "chat-stop",
            team_id,
            None,
        )

    def _chat_route(
        self,
        parts: list[str],
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        if len(parts) not in {4, 5} or parts[:2] != ["v1", "teams"] or parts[3] != "chat":
            return None
        team_id = validate_team_id(parts[2])
        if len(parts) == 4:
            return self._chat_start(team_id) if self.command == "POST" else None
        segment = parts[4]
        if self.command == "GET":
            return self._chat_pending(team_id, segment)
        if self.command == "POST" and segment == "stop":
            return self._chat_stop(team_id)
        return self._chat_submit(team_id, segment) if self.command == "POST" else None

    def _write_stream_record(self, record: dict[str, object]) -> None:
        encoded = progress_contract.encode_record(record)
        self.wfile.write(f"{len(encoded):X}\r\n".encode("ascii"))
        self.wfile.write(encoded)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _stream_chat_route(
        self,
        parts: list[str],
        route: strict_http.ControllerRouteMatch,
        request_audit: _RequestAudit,
    ) -> None:
        """Push advisory progress followed by one authoritative terminal record."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        writable = True
        def emit_progress(event: dict[str, object]) -> None:
            nonlocal writable
            if not writable:
                return
            try:
                self._write_stream_record({"type": "progress", **event})
            except (OSError, progress_contract.ProgressContractError):
                writable = False

        reporter = chat_progress.Reporter(emit_progress)
        operation = route.operation
        team_id = validate_team_id(route.params["team_id"])
        terminal: dict[str, object] = {}

        def execute() -> None:
            if operation == "chat":
                status, payload, *_audit = self._chat_start(team_id, reporter)
            elif operation == "chat-integration-submit":
                status, payload, *_audit = self._chat_submit(team_id, parts[4], reporter)
            else:
                raise AssertionError("non-streaming route reached chat stream")
            trace_id = request_audit.record(operation, result="ok", team_id=team_id)
            payload["trace_id"] = trace_id
            terminal.update(type="terminal", status=int(status), body=payload)

        def emit_failure(failure: stdlib.HttpFailure) -> None:
            trace_id = request_audit.record(
                operation,
                result=failure.result,
                team_id=team_id,
                detail=failure.audit_reason,
            )
            payload = {"error": failure.public_message, "trace_id": trace_id}
            if failure.public_code is not None:
                payload["code"] = failure.public_code
            terminal.update(type="terminal", status=int(failure.status), body=payload)

        stdlib.dispatch(
            execute,
            classify=lambda exc: local.classify_failure(exc, ApiProblem, DockerException),
            emit=emit_failure,
            unexpected_message="internal error",
        )
        if writable:
            try:
                self._write_stream_record(terminal)
            except (OSError, progress_contract.ProgressContractError):
                writable = False
        if writable:
            with contextlib.suppress(OSError):
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

    def _assistant_integration_route(
        self,
        parts: list[str],
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        if len(parts) < 4 or parts[:2] != ["v1", "teams"] or parts[3] != "assistant-integrations":
            return None
        team_id = validate_team_id(parts[2])
        if len(parts) == 4 and self.command == "GET":
            return (
                HTTPStatus.OK,
                self.server.controller.chat_turn_service.list_assistant_integrations(team_id),
                "assistant-integration-list",
                team_id,
                None,
            )
        if len(parts) == 7 and parts[4] == "challenges" and parts[6] == "authorize" and self.command == "POST":
            body = self._body()
            if set(body) != {"callback_mode", "session_binding"}:
                raise ApiProblem(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "OAuth authorization is invalid",
                    code="invalid-body",
                )
            if body["callback_mode"] not in integration_broker.CALLBACK_MODES:
                raise ApiProblem(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "OAuth authorization is invalid",
                    code="invalid-body",
                )
            return (
                HTTPStatus.OK,
                self.server.controller.chat_turn_service.start_assistant_integration_authorization(
                    team_id,
                    parts[5],
                    body["session_binding"],
                    body["callback_mode"],
                ),
                "assistant-integration-authorize",
                team_id,
                None,
            )
        if len(parts) == 7 and parts[4] == "challenges" and parts[6] == "authorize" and self.command == "DELETE":
            body = self._body()
            if set(body) != {"session_binding"}:
                raise ApiProblem(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "OAuth cancellation is invalid",
                    code="invalid-body",
                )
            return (
                HTTPStatus.OK,
                self.server.controller.chat_turn_service.cancel_assistant_integration_authorization(
                    body["session_binding"]
                ),
                "assistant-integration-cancel",
                team_id,
                None,
            )
        if len(parts) == 6 and self.command == "DELETE":
            return (
                HTTPStatus.OK,
                self.server.controller.chat_turn_service.disconnect_assistant_integration(
                    team_id,
                    parts[4],
                    parts[5],
                ),
                "assistant-integration-disconnect",
                team_id,
                parts[4],
            )
        return None

    def _team_route(self, parts: list[str]) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        if len(parts) == 4 and parts[:2] == ["v1", "teams"] and parts[3] == "create":
            team_id = validate_team_id(parts[2])
            if self.command == "POST":
                return (
                    HTTPStatus.OK,
                    self.server.controller.create_team(team_id, self._team_create_body()),
                    "team-create",
                    team_id,
                    None,
                )
        if len(parts) == 3 and parts[:2] == ["v1", "teams"] and self.command == "DELETE":
            team_id = validate_team_id(parts[2])
            return (
                HTTPStatus.OK,
                self.server.controller.destroy_team(team_id),
                "team-destroy",
                team_id,
                None,
            )
        return None

    def _route(
        self,
        parts: list[str],
        route: strict_http.ControllerRouteMatch,
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None]:
        controller = self.server.controller
        operation = route.operation
        grouped_resolver = {
            "fixed": self._fixed_route,
            "file": self._file_route,
            "inference": self._inference_route,
            "chat": self._chat_route,
            "assistant-integration": self._assistant_integration_route,
            "team": self._team_route,
        }.get(route.group)
        if grouped_resolver is not None:
            result = grouped_resolver(parts)
            if result is None:
                raise AssertionError("canonical local route group was not dispatched")
            return result

        team_id = validate_team_id(route.params["team_id"])
        if operation == "assistant-list":
            return HTTPStatus.OK, controller.list_assistants(team_id), operation, team_id, None
        if operation == "assistant-install":
            assistant_id, source_digest = self._install_body()
            return (
                HTTPStatus.OK,
                controller.install_publication(team_id, assistant_id, source_digest),
                operation,
                team_id,
                assistant_id,
            )
        assistant_id = route.params["assistant_id"]
        if operation == "assistant-uninstall":
            return (
                HTTPStatus.OK,
                controller.assistant_lifecycle.uninstall_assistant(team_id, assistant_id),
                operation,
                team_id,
                assistant_id,
            )
        if operation == "assistant-invoke":
            return (
                HTTPStatus.OK,
                controller.invoke(team_id, assistant_id, route.params["power_id"], self._body()),
                operation,
                team_id,
                assistant_id,
            )
        raise AssertionError("canonical local route was not dispatched")

    def _authorized_route(
        self,
        request_audit: _RequestAudit,
    ) -> tuple[HTTPStatus, dict[str, object], str, str | None, str | None] | None:
        parts, route = self._resolved_route()
        request_audit.operation = route.operation
        if route.operation in _MACHINE_ONLY_OPERATIONS:
            self._capture_body(route.operation)
            request_audit.machine()
            request_audit.record("machine-authority", result="ok")
            with local_audit.bind_request_principal(request_audit.principal()):
                return self._route(parts, route)

        assertion_state = local_authority.credential_state(self.headers)
        request_audit.absent(assertion_state)
        body = self._capture_body(route.operation)
        model = self._model_binding(route.operation)
        try:
            evidence = local_authority.verify(
                self.headers,
                method=self.command,
                path="/" + "/".join(parts),
                body=body,
                model=model,
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
        request_audit.record("human-authority", result="ok")
        with local_audit.bind_request_principal(request_audit.principal()):
            if route.operation in {"chat", "chat-integration-submit"}:
                self._stream_chat_route(parts, route, request_audit)
                return None
            return self._route(parts, route)

    def _handle(self) -> None:
        self.close_connection = True
        request_audit = _RequestAudit()
        if not self._authorized():
            request_audit.absent("machine_bearer_rejected")
            trace_id = request_audit.record("authentication", result="denied", detail="invalid-bearer")
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "authentication required", "trace_id": trace_id})
            return

        local.dispatch_route(
            lambda: self._authorized_route(request_audit),
            request_audit.record,
            self._send,
            ApiProblem,
            DockerException,
        )

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        self._handle()

    def do_PATCH(self) -> None:
        self._handle()

    def do_PUT(self) -> None:
        self._handle()
