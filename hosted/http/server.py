"""Bounded HTTP transport and route dispatch for the hosted Team controller."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import docker

from assistant import spec as assistant_registry
from core.http import stdlib
from core.http import strict as strict_http
from hosted import audit
from hosted import authority as account_authority
from hosted import state as runtime_state
from hosted import validation as validate
from hosted.assistant import lifecycle as assistant_lifecycle
from hosted.assistant import runtime as hosted_assistants
from hosted.chat import api as hosted_chat_api
from hosted.chat import human as hosted_chat_human
from hosted.chat import segment as hosted_chat_segment
from hosted.http import admission
from hosted.http import routes as hosted
from hosted.install import developers_client, publication
from hosted.install import http as developers_http
from hosted.team import lifecycle as hosted_lifecycle
from hosted.team import resources as hosted_resources
from inference import token as brain_runtime_token_store
from install import artifact_trust
from install import bindings as dynamic_assistants
from install import icons as assistant_icons
from protocol.http.v1 import payload as team_http_contract

_SOURCE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ACCOUNT_ID = re.compile(r"^[0-9a-f]{32}$")
_CHALLENGE_ID = re.compile(r"^[0-9a-f]{32}$")
_FILE_ID = re.compile(r"^[0-9a-f]{32}$")
_JSON_BODY_LIMITS = {
    "assistant-install": runtime_state.MAX_TEAM_JSON_BODY_BYTES,
    "assistant-integration-authorize": runtime_state.MAX_JSON_BODY_BYTES,
    "assistant-integration-complete": runtime_state.MAX_JSON_BODY_BYTES,
    "chat": runtime_state.MAX_JSON_BODY_BYTES,
    "chat-integration-submit": runtime_state.MAX_JSON_BODY_BYTES,
    "chat-human-submit": runtime_state.MAX_JSON_BODY_BYTES,
    "chat-stream": runtime_state.MAX_JSON_BODY_BYTES,
    "inference-configure": runtime_state.MAX_JSON_BODY_BYTES,
    "team-create": runtime_state.MAX_TEAM_JSON_BODY_BYTES,
}


@dataclass(frozen=True, slots=True)
class _AuthorizedRequest:
    params: dict[str, str]
    team_id: str
    principal: tuple[str, str | None]
    lease: hosted_resources._AuthorizationLease
    query: dict[str, str]
    assurance: dict[str, str] | None = None


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Thread-per-request server with hard admission and slow-client expiry."""

    daemon_threads = True

    def __init__(self, *args, max_concurrency: int | None = None, **kwargs) -> None:
        concurrency = runtime_state.MAX_HTTP_CONCURRENCY if max_concurrency is None else max_concurrency
        self._request_slots = threading.BoundedSemaphore(concurrency)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(runtime_state.HTTP_CONNECTION_TIMEOUT_SECONDS)
        return request, client_address

    def process_request(self, request, client_address) -> None:
        # Backpressure happens before a thread exists. At the ceiling, at most the kernel's bounded
        # listen backlog plus this accepted socket waits; Python thread count cannot grow unbounded.
        self._request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "team/1.0"

    def log_message(self, *_args) -> None:  # audit.log is the ONLY log source
        pass

    def _send_json(self, status: HTTPStatus, payload: dict, *, no_store: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_icon(self, contents: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(contents)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(contents)

    def _stream_chat(
        self,
        team_id: str,
        message: str,
        file_ids: object,
        assistant_ids: tuple[str, ...],
        lease: hosted_resources._AuthorizationLease,
    ) -> None:
        """Preserve the NDJSON transport while exposing only the validated terminal reply."""
        terminal: dict[str, object]
        stream_error = None
        with hosted_chat_api._exclusive_chat_turn(team_id, lease) as (token, container):
            pending = hosted_chat_api._pending_hosted_chat(team_id)
            if pending is not None:
                self._send_json(
                    HTTPStatus.PRECONDITION_REQUIRED,
                    pending,
                    no_store=True,
                )
                return
                # The durable token is claimed before a 200 or any response byte reaches the client.
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            def emit(obj: dict) -> None:
                line = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
                self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                self.wfile.flush()

            try:
                result = hosted_chat_segment._chat_in_turn(
                    team_id,
                    message,
                    file_ids,
                    assistant_ids,
                    token,
                    container,
                    lease.owner,
                )
                paused = result.get("status") in hosted_assistants.CHAT_PAUSED_STATUSES
                terminal = (
                    {"type": str(result["status"]), **result}
                    if paused
                    else {
                        "type": "done",
                        "reply": result["reply"],
                        "team_id": result["team_id"],
                        "team_name": result["team_name"],
                    }
                )
                emit(terminal)
            except runtime_state.ApiError as exc:
                terminal = (
                    {"type": "stopped"}
                    if exc.status == HTTPStatus.CONFLICT and exc.message == "brain turn stopped"
                    else {"type": "error", "status": int(exc.status), "detail": exc.message}
                )
                with contextlib.suppress(OSError):
                    emit(terminal)
            except (docker.errors.DockerException, OSError) as exc:
                stream_error = type(exc).__name__
                terminal = {"type": "error", "status": 500, "detail": "brain stream failed"}
                with contextlib.suppress(OSError):
                    emit(terminal)
            finally:
                with contextlib.suppress(OSError):
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
        self._audit_security(
            "chat",
            team_id,
            result=(
                "ok"
                if terminal["type"] == "done" or terminal["type"] in hosted_assistants.CHAT_PAUSED_STATUSES
                else "error"
            ),
            streamed=True,
            status=terminal.get("status"),
            reason=stream_error,
        )

    def _read_body(self, *, max_bytes: int | None = None) -> dict:
        raw = getattr(self, "_captured_json_raw", None)
        body = getattr(self, "_captured_json_body", None)
        limit = runtime_state.MAX_JSON_BODY_BYTES if max_bytes is None else max_bytes
        if not isinstance(raw, bytes) or not isinstance(body, dict) or len(raw) > limit:
            raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "request body is unavailable")
        return body

    def _read_file_body(self) -> tuple[str, bytes, str]:
        metadata = getattr(self, "_captured_file_metadata", None)
        if not isinstance(metadata, strict_http.FileUploadMetadata):
            raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "file upload metadata is unavailable")
        try:
            content = strict_http.read_file_content(self.rfile, metadata)
        except strict_http.HttpContractError as exc:
            raise runtime_state.ApiError(exc.status, exc.message) from exc
        return metadata.filename, content, metadata.media_type

    def _capture_body(self, operation: str) -> dict[str, object]:
        self._captured_json_raw = None
        self._captured_json_body = None
        self._captured_file_metadata = None
        try:
            if operation == "file-upload":
                metadata = strict_http.file_upload_metadata(
                    self.headers,
                    max_bytes=hosted_assistants.MAX_FILE_BODY_BYTES,
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
                raw, body = strict_http.read_json_document(self.headers, self.rfile, max_bytes=limit)
                self._captured_json_raw = raw
                self._captured_json_body = body
                return {
                    "kind": "json",
                    "length": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            strict_http.reject_body(self.headers)
        except strict_http.HttpContractError as exc:
            raise runtime_state.ApiError(exc.status, exc.message) from exc
        return {
            "kind": "none",
            "length": 0,
            "sha256": account_authority.EMPTY_SHA256,
        }

    def _read_team_body(self, keys: set[str]) -> dict[str, object]:
        """Read one closed Team mutation document; arbitrary scripts/shapes never cross the bridge."""
        body = self._read_body(max_bytes=runtime_state.MAX_TEAM_JSON_BODY_BYTES)
        if not isinstance(body, dict) or set(body) != keys:
            raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "request body does not match the Team operation")
        return body

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        if developers_http.is_path(self.path):
            developers_http.dispatch(
                developers_http.RequestIO(
                    self.headers,
                    self.path,
                    self._capture_body,
                    self._read_team_body,
                    self._send_json,
                ),
                method,
            )
            return
        stdlib.dispatch(
            lambda: self._dispatch_resolved(method),
            classify=lambda exc: hosted.classify_failure(
                exc,
                runtime_state.ApiError,
                validate.ValidationError,
                assistant_registry.AssistantSpecError,
            ),
            emit=lambda failure: self._emit_failure(method, failure),
            unexpected_message="internal Team error",
        )

    def _validated_params(self, route: strict_http.ControllerRouteMatch) -> dict[str, str]:
        params = dict(route.params)
        if "team_id" in params:
            team_id = validate.validate_team_id(params["team_id"])
            if team_id != params["team_id"]:
                raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "Team id must be canonical")
            params["team_id"] = team_id
        for field in ("assistant_id", "integration_id"):
            if field in params:
                params[field] = assistant_registry.validate_assistant_id(params[field])
        if "challenge_id" in params and _CHALLENGE_ID.fullmatch(params["challenge_id"]) is None:
            raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "OAuth challenge id is invalid")
        if "file_id" in params and _FILE_ID.fullmatch(params["file_id"]) is None:
            raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "file id is invalid")
        return params

    @staticmethod
    def _validated_query(operation: str, query: dict[str, str]) -> dict[str, str]:
        if operation != "team-logs":
            if query:
                raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "query is not accepted for this operation")
            return {}
        if set(query) - {"lines"}:
            raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "Team logs query is invalid")
        lines = query.get("lines")
        if lines is not None and (not lines.isascii() or not lines.isdecimal() or not 1 <= int(lines) <= 1000):
            raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "Team logs line count is invalid")
        return dict(query)

    def _owner_target(self, operation: str) -> str | None:
        if operation != "team-create":
            return None
        body = self._read_body(max_bytes=runtime_state.MAX_TEAM_JSON_BODY_BYTES)
        if set(body) - {"team_name", "provider", "model", "owner_account_id"}:
            raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Team creation request is invalid")
        owner = body.get("owner_account_id")
        if owner is not None and (not isinstance(owner, str) or _ACCOUNT_ID.fullmatch(owner) is None):
            raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "Team Owner Account is invalid")
        return owner

    def _account_session(self) -> str:
        self._audit_credential_state = "credential_absent_or_malformed"
        values = self.headers.get_all(team_http_contract.ACCOUNT_SESSION_HEADER, failobj=[])
        if len(values) != 1:
            raise runtime_state.ApiError(HTTPStatus.FORBIDDEN, "invalid or missing credentials")
        try:
            value = account_authority.session_token(values[0])
        except account_authority.AuthorityDeniedError as exc:
            raise runtime_state.ApiError(HTTPStatus.FORBIDDEN, "invalid or missing credentials") from exc
        self._audit_credential_state = "credential_present"
        return value

    def _human_authority(
        self,
        session_token: str,
        request: admission.AuthorityRequest,
    ) -> account_authority.Evaluation:
        binding: dict[str, object] = {
            "method": request.method,
            "operation": request.route.operation,
            "params": request.params,
            "query": request.query,
            "body": request.body,
        }
        owner = self._owner_target(request.route.operation)
        if owner is not None:
            binding["owner_account_id"] = owner
        if request.assurance is not None:
            binding["assurance"] = request.assurance
        target = request.params.get("team_id", request.route.operation)
        binding_digest: str | None = None
        try:
            binding_digest = account_authority.binding_digest(binding)
            evaluation = account_authority.evaluate(session_token, binding, request.assurance_handle)
        except account_authority.AuthorityDeniedError as exc:
            self._audit_credential_state = "credential_rejected"
            self._audit_trace_id = audit.log(
                "human_authority",
                target,
                result="denied",
                principal_id=None,
                principal_class="absent",
                credential_state=self._audit_credential_state,
                operation=request.route.operation,
                **({"binding_digest": binding_digest} if binding_digest is not None else {}),
            )
            raise runtime_state.ApiError(HTTPStatus.FORBIDDEN, "invalid or missing credentials") from exc
        except account_authority.AuthorityUnavailableError as exc:
            self._audit_trace_id = audit.log(
                "human_authority",
                target,
                result="error",
                principal_id=None,
                principal_class="absent",
                credential_state=self._audit_credential_state,
                operation=request.route.operation,
                **({"binding_digest": binding_digest} if binding_digest is not None else {}),
            )
            raise runtime_state.ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Account authority is unavailable") from exc
        self._audit_account_id = evaluation.account_id
        self._audit_supervisor = evaluation.supervisor
        self._audit_trace_id = audit.log(
            "human_authority",
            target,
            result="ok",
            principal_id=evaluation.account_id,
            principal_class="human",
            supervisor=evaluation.supervisor,
            operation=request.route.operation,
            binding_digest=evaluation.binding_digest,
        )
        return evaluation

    def _dispatch_resolved(self, method: str) -> None:
        _target, route = hosted.route_target(self.headers, self.path, method, runtime_state.ApiError)
        params = self._validated_params(route)
        query = self._validated_query(route.operation, _target.query)
        if route.operation == "assistant-integration-complete":
            if not strict_http.bearer_matches(self.headers, runtime_state._token):
                raise runtime_state.ApiError(HTTPStatus.FORBIDDEN, "invalid or missing credentials")
            self._audit_machine_principal = "admin"
            self._capture_body(route.operation)
            self._route_assistant_integration_complete()
            return
        session_token = self._account_session()
        body_binding = self._capture_body(route.operation)
        captured_body = self._read_body() if route.operation == "chat-human-submit" else None
        assurance, assurance_handle = admission.action_assurance(route.operation, params, captured_body)
        evaluation = self._human_authority(
            session_token,
            admission.AuthorityRequest(
                method,
                route,
                params,
                query,
                body_binding,
                assurance,
                assurance_handle,
            ),
        )
        self._route(route, params, query, evaluation)

    def _publication_dependencies(self):
        dependencies = (
            runtime_state._developers_client,
            runtime_state._artifact_trust,
        )
        if any(dependency is None for dependency in dependencies):
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Developers integration is unavailable",
            )
        return dependencies

    def _emit_failure(self, method: str, failure: stdlib.HttpFailure) -> None:
        self._audit_security(
            method.lower(),
            self.path,
            result=failure.result,
            reason=failure.audit_reason,
        )
        self._send_json(failure.status, {"error": failure.public_message})

    def _audit_security(self, operation: str, target: str, *, result: str, **extra: object) -> str:
        principal: dict[str, object] = {}
        machine_principal = getattr(self, "_audit_machine_principal", None)
        account_id = getattr(self, "_audit_account_id", None)
        supervisor = getattr(self, "_audit_supervisor", None)
        if isinstance(machine_principal, str):
            principal = {
                "principal_id": machine_principal,
                "principal_class": "machine",
            }
        elif isinstance(account_id, str) and isinstance(supervisor, bool):
            principal = {
                "principal_id": account_id,
                "principal_class": "human",
                "supervisor": supervisor,
            }
        else:
            principal = {
                "principal_id": None,
                "principal_class": "absent",
            }
        credential_state = getattr(self, "_audit_credential_state", None)
        if isinstance(credential_state, str):
            principal["credential_state"] = credential_state
        return audit.log(
            operation,
            target,
            result=result,
            trace_id=getattr(self, "_audit_trace_id", None),
            **principal,
            **extra,
        )

    def _route(
        self,
        route: strict_http.ControllerRouteMatch,
        params: dict[str, str],
        query: dict[str, str],
        evaluation: account_authority.Evaluation,
    ) -> None:
        principal = evaluation.principal
        global_handler = _GLOBAL_ROUTES.get(route.operation)
        if global_handler is not None:
            global_handler(self, principal)
            return

        team_id = params["team_id"]
        preauthorized_handler = _PREAUTHORIZED_ROUTES.get(route.operation)
        if preauthorized_handler is not None:
            preauthorized_handler(self, team_id, principal, evaluation.owner_account_id)
            return

        lease = hosted_resources._authorize(team_id, principal)
        if route.operation in {"chat-human-pending", "chat-human-submit"} and evaluation.account_id != lease.owner:
            raise runtime_state.ApiError(HTTPStatus.FORBIDDEN, "Team Owner authority is required")
        request = _AuthorizedRequest(params, team_id, principal, lease, query, evaluation.assurance)
        _AUTHORIZED_ROUTES[route.operation](self, request)

    def _route_team_list(self, principal: tuple[str, str | None]) -> None:
        kind, account_id = principal
        self._send_json(
            HTTPStatus.OK,
            hosted_lifecycle._list(owner=None if kind == "supervisor" else account_id),
        )

    def _route_assistant_integration_complete(self) -> None:
        result, owner = hosted_chat_api._complete_oauth_integration(self._read_body())
        self._audit_security(
            "assistant_integration_complete",
            result["team_id"],
            result="ok",
            assistant=result["assistant_id"],
            provider=result["provider"],
            owner_account_id=owner,
        )
        self._send_json(HTTPStatus.OK, result, no_store=True)

    def _route_team_create(
        self,
        team_id: str,
        principal: tuple[str, str | None],
        owner_account_id: str | None,
    ) -> None:
        if owner_account_id is None:
            raise runtime_state.ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Account Owner evidence is unavailable")
        runtime_state._enforce_rate("create", principal)
        body = self._read_body(max_bytes=runtime_state.MAX_TEAM_JSON_BODY_BYTES)
        lifecycle_body = {key: value for key, value in body.items() if key != "owner_account_id"}
        result = hosted_lifecycle._create(team_id, lifecycle_body, owner_account_id)
        trace = self._audit_security(
            "create",
            team_id,
            result="ok",
            created=result.get("created"),
            owner=owner_account_id,
        )
        self._send_json(HTTPStatus.OK, {**result, "trace_id": trace})

    def _route_team_destroy(
        self,
        team_id: str,
        principal: tuple[str, str | None],
        _owner_account_id: str | None,
    ) -> None:
        # Destroy may authorize against a bounded non-runnable cleanup successor.
        lease = hosted_resources._authorize_destroy(team_id, principal)
        result = hosted_lifecycle._destroy(team_id, lease)
        trace = self._audit_security("destroy", team_id, result="ok", db_dropped=result["db_dropped"])
        self._send_json(HTTPStatus.OK, {**result, "trace_id": trace})

    def _route_assistant_integration_list(self, request: _AuthorizedRequest) -> None:
        self._send_json(
            HTTPStatus.OK,
            hosted_assistants._assistant_integration_inventory(request.team_id, request.lease),
            no_store=True,
        )

    def _route_assistant_integration_authorize(self, request: _AuthorizedRequest) -> None:
        body = self._read_body()
        if not isinstance(body, dict) or set(body) != {"assistant_id", "integration_id", "session_binding"}:
            raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "OAuth authorization request is invalid")
        result = hosted_chat_api._start_oauth_integration(
            request.team_id,
            request.params["challenge_id"],
            body["assistant_id"],
            body["integration_id"],
            body["session_binding"],
            request.lease,
        )
        self._audit_security("assistant_integration_start", request.team_id, result="ok")
        self._send_json(HTTPStatus.OK, result, no_store=True)

    def _route_assistant_integration_disconnect(self, request: _AuthorizedRequest) -> None:
        assistant_id = request.params["assistant_id"]
        integration_id = request.params["integration_id"]
        result = hosted_chat_api._disconnect_oauth_integration(
            request.team_id,
            assistant_id,
            integration_id,
            request.lease,
        )
        self._audit_security(
            "assistant_integration_disconnect",
            request.team_id,
            result="ok",
            assistant=assistant_id,
            integration=integration_id,
            disconnected=result["disconnected"],
        )
        self._send_json(HTTPStatus.OK, result, no_store=True)

    def _route_assistant_stored_input_list(self, request: _AuthorizedRequest) -> None:
        self._send_json(
            HTTPStatus.OK,
            hosted_assistants._assistant_stored_input_inventory(request.team_id, request.lease),
            no_store=True,
        )

    def _route_assistant_stored_input_clear(self, request: _AuthorizedRequest) -> None:
        assistant_id = request.params["assistant_id"]
        stored_input_id = request.params["stored_input_id"]
        result = hosted_chat_api._clear_assistant_stored_input(
            request.team_id,
            assistant_id,
            stored_input_id,
            request.lease,
        )
        self._audit_security(
            "assistant_stored_input_clear",
            request.team_id,
            result="ok",
            assistant=assistant_id,
            stored_input=stored_input_id,
            cleared=result["cleared"],
        )
        self._send_json(HTTPStatus.OK, result, no_store=True)

    def _route_team_status(self, request: _AuthorizedRequest) -> None:
        self._send_json(HTTPStatus.OK, hosted_lifecycle._status(request.team_id, request.lease))

    def _route_team_logs(self, request: _AuthorizedRequest) -> None:
        lines = int(request.query.get("lines", "200"))
        self._send_json(
            HTTPStatus.OK,
            hosted_lifecycle._logs(request.team_id, lines, request.lease),
        )

    def _route_team_lifecycle(self, request: _AuthorizedRequest, *, operation: str) -> None:
        result = hosted_lifecycle._lifecycle(request.team_id, operation, request.lease)
        self._audit_security(operation, request.team_id, result="ok")
        self._send_json(HTTPStatus.OK, result)

    def _route_file_list(self, request: _AuthorizedRequest) -> None:
        self._send_json(
            HTTPStatus.OK,
            hosted_lifecycle._list_team_files(request.team_id, request.lease),
        )

    def _route_file_upload(self, request: _AuthorizedRequest) -> None:
        runtime_state._enforce_rate("file_upload", request.principal)
        if not runtime_state._file_upload_slots.acquire(blocking=False):
            raise runtime_state.ApiError(
                HTTPStatus.TOO_MANY_REQUESTS,
                "another Team file upload is in progress",
            )
        try:
            filename, content, media_type = self._read_file_body()
            result = hosted_lifecycle._put_inbox_file(
                request.team_id,
                filename,
                content,
                media_type,
                request.lease,
            )
        finally:
            runtime_state._file_upload_slots.release()
        trace = self._audit_security(
            "team_file_upload",
            request.team_id,
            result="ok",
            file_id=result["file"]["id"],
            bytes=result["file"]["size"],
        )
        self._send_json(HTTPStatus.OK, {**result, "trace_id": trace})

    def _route_file_delete(self, request: _AuthorizedRequest) -> None:
        result = hosted_lifecycle._delete_team_file(
            request.team_id,
            request.params["file_id"],
            request.lease,
        )
        trace = self._audit_security(
            "team_file_delete",
            request.team_id,
            result="ok",
            file_id=result["id"],
            deleted=result["deleted"],
        )
        self._send_json(HTTPStatus.OK, {**result, "trace_id": trace})

    def _route_inference_status(self, request: _AuthorizedRequest) -> None:
        self._send_json(
            HTTPStatus.OK,
            hosted_lifecycle._inference_status(request.team_id, request.lease),
        )

    def _route_inference_configure(self, request: _AuthorizedRequest) -> None:
        result = hosted_lifecycle._configure_inference(
            request.team_id,
            self._read_body(),
            request.lease,
        )
        self._audit_security(
            "inference_configure",
            request.team_id,
            result="ok",
            provider=result["provider"],
            model=result["model"],
        )
        self._send_json(HTTPStatus.OK, result)

    def _route_chat_turn(
        self,
        request: _AuthorizedRequest,
        *,
        stream: bool,
    ) -> None:
        body = self._read_body()
        if not isinstance(body, dict) or set(body) != {"message", "files", "assistant_ids"}:
            raise runtime_state.ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "Team chat requires message, files, and assistant_ids",
            )
        message = validate.validate_chat_message(body["message"])
        file_ids = body["files"]
        assistant_ids = hosted_assistants._chat_assistant_ids(body["assistant_ids"])
        if stream:
            runtime_state._enforce_rate("stream", request.principal)
            pending = hosted_chat_api._pending_hosted_chat(request.team_id)
            if pending is not None:
                self._send_json(
                    HTTPStatus.PRECONDITION_REQUIRED,
                    pending,
                    no_store=True,
                )
                return
            self._stream_chat(request.team_id, message, file_ids, assistant_ids, request.lease)
            return
        runtime_state._enforce_rate("chat", request.principal)
        result = hosted_chat_api._chat(
            request.team_id,
            message,
            file_ids,
            assistant_ids,
            request.lease,
        )
        self._audit_security(
            "chat",
            request.team_id,
            result="ok",
            chars_in=len(message),
            chars_out=len(str(result.get("reply", ""))),
            paused=result.get("status") in hosted_assistants.CHAT_PAUSED_STATUSES,
        )
        paused = result.get("status") in hosted_assistants.CHAT_PAUSED_STATUSES
        self._send_json(
            HTTPStatus.PRECONDITION_REQUIRED if paused else HTTPStatus.OK,
            result,
            no_store=paused,
        )

    def _route_chat_integrations(
        self,
        request: _AuthorizedRequest,
        *,
        submit: bool,
    ) -> None:
        if not submit:
            pending = runtime_state._integration_challenges.current(request.team_id)
            self._send_json(
                HTTPStatus.OK,
                (
                    hosted_chat_segment._hosted_integration_challenge_payload(pending)
                    if pending is not None
                    else {"team_id": request.team_id, "status": "none"}
                ),
                no_store=True,
            )
            return
        runtime_state._enforce_rate("chat", request.principal)
        body = self._read_body()
        if not isinstance(body, dict) or set(body) != {"challenge_id"}:
            raise runtime_state.ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "integration continuation is invalid")
        result = hosted_chat_api._resume_chat_integrations(
            request.team_id,
            body["challenge_id"],
            request.lease,
        )
        paused = result.get("status") in hosted_assistants.CHAT_PAUSED_STATUSES
        self._send_json(
            HTTPStatus.PRECONDITION_REQUIRED if paused else HTTPStatus.OK,
            result,
            no_store=True,
        )

    def _route_chat_human(
        self,
        request: _AuthorizedRequest,
        *,
        submit: bool,
    ) -> None:
        if not submit:
            self._send_json(
                HTTPStatus.OK,
                hosted_chat_human.pending_chat_human(request.team_id),
                no_store=True,
            )
            return
        runtime_state._enforce_rate("chat", request.principal)
        result = hosted_chat_api._resume_chat_human(
            request.team_id,
            self._read_body(),
            request.assurance,
            request.lease,
        )
        paused = result.get("status") in hosted_assistants.CHAT_PAUSED_STATUSES
        self._send_json(
            HTTPStatus.PRECONDITION_REQUIRED if paused else HTTPStatus.OK,
            result,
            no_store=True,
        )

    def _route_chat_stop(self, request: _AuthorizedRequest) -> None:
        runtime_state._enforce_rate("stop", request.principal)
        result = hosted_chat_api._stop_chat(request.team_id, request.lease)
        self._audit_security(
            "chat_stop",
            request.team_id,
            result="ok" if result["accepted"] else "denied",
        )
        self._send_json(HTTPStatus.OK, result)

    def _route_assistant_install(self, request: _AuthorizedRequest) -> None:
        body = self._read_team_body({"assistant_id", "source_digest"})
        assistant_id = assistant_registry.validate_assistant_id(body["assistant_id"])
        source_digest = body["source_digest"]
        if not isinstance(source_digest, str) or _SOURCE_DIGEST.fullmatch(source_digest) is None:
            raise runtime_state.ApiError(HTTPStatus.BAD_REQUEST, "source digest is invalid")
        runtime_state._enforce_rate("install", request.principal)
        client, trust = self._publication_dependencies()
        try:
            resolution = client.resolve(source_digest)
            if resolution["assistant_id"] != assistant_id:
                raise developers_client.AssistantNotInstallableError(
                    "Assistant publication does not match the requested identifier"
                )
            trust.verify(resolution)
            binding = dynamic_assistants.binding_from_resolution(request.team_id, resolution)
            hosted_resources._prepare_assistant_image(publication.assistant_spec(binding))
            publication.retain_icon(client, runtime_state._assistant_icons, resolution)

            def authorize_start() -> None:
                current = client.resolve(source_digest)
                if current["assistant_id"] != assistant_id or current["oci_digest"] != resolution["oci_digest"]:
                    raise developers_client.InstallAuthorizationDeniedError(
                        "Assistant publication changed before installation"
                    )

            try:
                installed = assistant_lifecycle._install_assistant(
                    request.team_id,
                    binding,
                    request.lease.owner,
                    request.lease,
                    authorize_start=authorize_start,
                )
            finally:
                publication.discard_icon(
                    runtime_state._assistant_icons,
                    runtime_state._dynamic_assistants,
                    source_digest,
                )
        except developers_client.AssistantNotInstallableError as exc:
            raise runtime_state.ApiError(HTTPStatus.NOT_FOUND, "Assistant is not installable") from exc
        except developers_client.InstallAuthorizationDeniedError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.CONFLICT,
                "Assistant installation is no longer authorized",
            ) from exc
        except developers_client.DevelopersClientError as exc:
            raise runtime_state.ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Developers is unavailable") from exc
        except artifact_trust.ArtifactTrustError as exc:
            raise runtime_state.ApiError(HTTPStatus.CONFLICT, "Assistant artifact trust failed") from exc
        except assistant_icons.AssistantIconError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant icon storage is unavailable",
            ) from exc
        response = {
            "assistant": assistant_id,
            "installed": True,
            "source_digest": installed["source_digest"],
            "oci_digest": installed["oci_digest"],
            "binding_digest": installed["binding_digest"],
        }
        trace = self._audit_security(
            "assistant_install",
            request.team_id,
            result="ok",
            assistant=assistant_id,
            source_digest=source_digest,
        )
        self._send_json(HTTPStatus.OK, {**response, "trace_id": trace}, no_store=True)

    def _route_assistant_list(self, request: _AuthorizedRequest) -> None:
        inventory = assistant_lifecycle._list_assistants(request.team_id, request.lease)
        self._send_json(
            HTTPStatus.OK,
            {"assistants": inventory["assistants"]},
            no_store=True,
        )

    def _route_assistant_icon(self, request: _AuthorizedRequest) -> None:
        assistant_id = request.params["assistant_id"]
        contents = assistant_lifecycle._assistant_icon(
            request.team_id,
            assistant_id,
            request.lease,
        )
        self._audit_security(
            "assistant_icon",
            request.team_id,
            result="ok",
            assistant=assistant_id,
        )
        self._send_icon(contents)

    def _route_assistant_uninstall(self, request: _AuthorizedRequest) -> None:
        assistant_id = assistant_registry.validate_assistant_id(request.params["assistant_id"])
        try:
            binding = runtime_state._dynamic_assistants.get(request.team_id, assistant_id)
        except dynamic_assistants.DynamicAssistantError as exc:
            raise runtime_state.ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Assistant metadata is unavailable",
            ) from exc
        if binding is None:
            raise runtime_state.ApiError(HTTPStatus.NOT_FOUND, "Assistant is not installed")
        result = assistant_lifecycle._uninstall_assistant(
            request.team_id,
            assistant_id,
            request.lease,
        )
        trace = self._audit_security(
            "assistant_uninstall",
            request.team_id,
            result="ok",
            assistant=assistant_id,
        )
        self._send_json(
            HTTPStatus.OK,
            {
                "assistant": assistant_id,
                "uninstalled": result["uninstalled"],
                "trace_id": trace,
            },
            no_store=True,
        )


_GLOBAL_ROUTES = {
    "team-list": Handler._route_team_list,
}
_PREAUTHORIZED_ROUTES = {
    "team-create": Handler._route_team_create,
    "team-destroy": Handler._route_team_destroy,
}
_AUTHORIZED_ROUTES = {
    "file-list": Handler._route_file_list,
    "file-upload": Handler._route_file_upload,
    "file-delete": Handler._route_file_delete,
    "inference-status": Handler._route_inference_status,
    "inference-configure": Handler._route_inference_configure,
    "chat": functools.partial(Handler._route_chat_turn, stream=False),
    "chat-stream": functools.partial(Handler._route_chat_turn, stream=True),
    "chat-integration-pending": functools.partial(Handler._route_chat_integrations, submit=False),
    "chat-integration-submit": functools.partial(Handler._route_chat_integrations, submit=True),
    "chat-human-pending": functools.partial(Handler._route_chat_human, submit=False),
    "chat-human-submit": functools.partial(Handler._route_chat_human, submit=True),
    "chat-stop": Handler._route_chat_stop,
    "assistant-integration-list": Handler._route_assistant_integration_list,
    "assistant-integration-authorize": Handler._route_assistant_integration_authorize,
    "assistant-integration-disconnect": Handler._route_assistant_integration_disconnect,
    "assistant-stored-input-list": Handler._route_assistant_stored_input_list,
    "assistant-stored-input-clear": Handler._route_assistant_stored_input_clear,
    "assistant-install": Handler._route_assistant_install,
    "assistant-icon": Handler._route_assistant_icon,
    "assistant-list": Handler._route_assistant_list,
    "assistant-uninstall": Handler._route_assistant_uninstall,
    "team-status": Handler._route_team_status,
    "team-logs": Handler._route_team_logs,
    "team-stop": functools.partial(Handler._route_team_lifecycle, operation="stop"),
    "team-start": functools.partial(Handler._route_team_lifecycle, operation="start"),
    "team-restart": functools.partial(Handler._route_team_lifecycle, operation="restart"),
}


def main() -> None:
    # The Controller owns this bearer. The runtime receives the same named volume read-only and
    # cannot rotate or replace its authority.
    brain_runtime_token_store.ensure()
    runtime_state._initialize_developers_integration()
    _BoundedThreadingHTTPServer((runtime_state.ALL_INTERFACES, runtime_state.LISTEN_PORT), Handler).serve_forever()
