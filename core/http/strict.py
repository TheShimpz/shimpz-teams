"""Fail-closed HTTP parsing primitives shared by both Team profiles."""

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import BinaryIO
from urllib.parse import parse_qsl, quote, unquote_to_bytes, urlsplit

from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import strict_json

MAX_REQUEST_TARGET_BYTES = 512
MAX_FILENAME_BYTES = 255
MAX_MEDIA_TYPE_CHARS = 127
FILE_NAME_HEADER = "X-Shimpz-Filename"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class HttpContractError(ValueError):
    def __init__(self, status: HTTPStatus, message: str, *, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


@dataclass(frozen=True)
class RequestTarget:
    path: str
    parts: tuple[str, ...]
    query: dict[str, str]


@dataclass(frozen=True, slots=True)
class FileUploadMetadata:
    length: int
    filename: str
    media_type: str


@dataclass(frozen=True, slots=True)
class CapturedBody:
    """One request body read once before authorization: its binding and its JSON document or file metadata."""

    binding: dict[str, object]
    raw: bytes | None = None
    document: dict[str, object] | None = None
    file: FileUploadMetadata | None = None


def bearer_matches(headers: object, token: str) -> bool:
    """Accept exactly one bearer header and compare it in constant time."""
    values = headers.get_all("Authorization", failobj=[])
    # Headers decode as Latin-1, and compare_digest raises on non-ASCII text instead of refusing it.
    return len(values) == 1 and values[0].isascii() and hmac.compare_digest(values[0], f"Bearer {token}")


def _content_length(headers: object) -> int:
    """The single declared length of an unchunked request body."""
    if headers.get_all("Transfer-Encoding", failobj=[]):
        raise HttpContractError(
            HTTPStatus.BAD_REQUEST,
            "chunked requests are not accepted",
            code="chunked-request",
        )
    lengths = headers.get_all("Content-Length", failobj=[])
    if len(lengths) != 1:
        raise HttpContractError(
            HTTPStatus.LENGTH_REQUIRED,
            "one Content-Length is required",
            code="content-length",
        )
    return _declared_length(lengths[0])


def _declared_length(value: str) -> int:
    """One Content-Length value: ASCII digits only, with optional surrounding space or tab (RFC 9110 OWS)."""
    digits = value.strip(" \t")
    try:
        if not digits.isascii() or not digits.isdigit():
            raise ValueError("Content-Length is not a decimal length")
        return int(digits)
    except ValueError as exc:
        raise HttpContractError(
            HTTPStatus.BAD_REQUEST,
            "invalid Content-Length",
            code="content-length",
        ) from exc


def read_json_document(
    headers: object,
    stream: BinaryIO,
    *,
    max_bytes: int,
) -> tuple[bytes, dict[str, object]]:
    """Capture and parse one finite duplicate-free JSON object exactly once."""
    length = _content_length(headers)
    if length < 2 or length > max_bytes:
        raise HttpContractError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"request body is too large (max {max_bytes} bytes)",
            code="body-too-large",
        )
    content_types = headers.get_all("Content-Type", failobj=[])
    if len(content_types) != 1 or content_types[0].partition(";")[0].strip().lower() != "application/json":
        raise HttpContractError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            "Content-Type must be application/json",
            code="content-type",
        )
    try:
        raw = stream.read(length)
        if len(raw) != length:
            raise ValueError("short request body")
        body = strict_json.loads(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise HttpContractError(
            HTTPStatus.BAD_REQUEST,
            "invalid JSON body",
            code="invalid-json",
        ) from exc
    if not isinstance(body, dict):
        raise HttpContractError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "a JSON object is required",
            code="invalid-body",
        )
    return raw, body


def read_json_object(
    headers: object,
    stream: BinaryIO,
    *,
    max_bytes: int,
) -> dict[str, object]:
    """Read one JSON object when the caller does not need its exact byte binding."""
    _raw, body = read_json_document(headers, stream, max_bytes=max_bytes)
    return body


def file_upload_metadata(
    headers: object,
    *,
    max_bytes: int,
) -> FileUploadMetadata:
    """Validate file framing and metadata before reading untrusted content."""
    length = _content_length(headers)
    if not 1 <= length <= max_bytes:
        raise HttpContractError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            f"file must contain 1 to {max_bytes} bytes",
            code="file-too-large",
        )

    content_types = headers.get_all("Content-Type", failobj=[])
    media_type = content_types[0] if len(content_types) == 1 else ""
    if (
        not media_type
        or media_type != media_type.lower()
        or len(media_type) > MAX_MEDIA_TYPE_CHARS
        or http_payload.MEDIA_TYPE_RE.fullmatch(media_type) is None
    ):
        raise HttpContractError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            "Content-Type must be a canonical media type",
            code="content-type",
        )

    filenames = headers.get_all(FILE_NAME_HEADER, failobj=[])
    encoded_filename = filenames[0] if len(filenames) == 1 else ""
    try:
        filename = unquote_to_bytes(encoded_filename).decode("utf-8")
    except UnicodeError as exc:
        raise HttpContractError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "invalid file name",
            code="invalid-file",
        ) from exc
    if (
        not filename
        or len(filename.encode("utf-8")) > MAX_FILENAME_BYTES
        or quote(filename, safe="") != encoded_filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or filename.strip() != filename
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise HttpContractError(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "invalid file name",
            code="invalid-file",
        )

    return FileUploadMetadata(length, filename, media_type)


def read_file_content(stream: BinaryIO, metadata: FileUploadMetadata) -> bytes:
    """Read exactly the content covered by previously validated file metadata."""
    try:
        body = stream.read(metadata.length)
    except OSError as exc:
        raise HttpContractError(
            HTTPStatus.BAD_REQUEST,
            "invalid file body",
            code="invalid-file",
        ) from exc
    if len(body) != metadata.length:
        raise HttpContractError(
            HTTPStatus.BAD_REQUEST,
            "invalid file body",
            code="invalid-file",
        )
    return body


def read_file_upload(
    headers: object,
    stream: BinaryIO,
    *,
    max_bytes: int,
) -> tuple[str, bytes, str]:
    """Read one file when the caller does not need pre-content authorization."""
    metadata = file_upload_metadata(headers, max_bytes=max_bytes)
    return metadata.filename, read_file_content(stream, metadata), metadata.media_type


def reject_body(headers: object) -> None:
    """Reject transfer framing or a nonzero body on a bodyless route."""
    if headers.get_all("Transfer-Encoding", failobj=[]):
        raise HttpContractError(
            HTTPStatus.BAD_REQUEST,
            "this request cannot have a body",
            code="unexpected-body",
        )
    lengths = headers.get_all("Content-Length", failobj=[])
    if len(lengths) > 1:
        raise HttpContractError(
            HTTPStatus.BAD_REQUEST,
            "invalid Content-Length",
            code="content-length",
        )
    if lengths and _declared_length(lengths[0]) != 0:
        raise HttpContractError(
            HTTPStatus.BAD_REQUEST,
            "this request cannot have a body",
            code="unexpected-body",
        )


def parse_request_target(
    raw_target: str,
    *,
    allow_query: bool,
    max_bytes: int = MAX_REQUEST_TARGET_BYTES,
) -> RequestTarget:
    """Parse one bounded origin-form target without encoded or ambiguous routing."""
    if len(raw_target.encode("utf-8", "replace")) > max_bytes:
        raise HttpContractError(
            HTTPStatus.URI_TOO_LONG,
            "request path is too long",
            code="path-too-long",
        )
    parsed = urlsplit(raw_target)
    if parsed.fragment or "%" in parsed.path or (parsed.query and not allow_query):
        raise HttpContractError(
            HTTPStatus.BAD_REQUEST,
            "query and encoded paths are not accepted",
            code="invalid-path",
        )
    query: dict[str, str] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if not key or key in query:
            raise HttpContractError(
                HTTPStatus.BAD_REQUEST,
                "request query is ambiguous",
                code="invalid-path",
            )
        query[key] = value
    return RequestTarget(
        path=parsed.path,
        parts=tuple(part for part in parsed.path.split("/") if part),
        query=query,
    )


def parse_routed_request(
    headers: object,
    raw_target: str,
    method: str,
    *,
    body_methods: frozenset[str],
    allow_query: bool,
    max_bytes: int = MAX_REQUEST_TARGET_BYTES,
) -> RequestTarget:
    """Parse a request target and enforce the route table's body-capable methods."""
    target = parse_request_target(raw_target, allow_query=allow_query, max_bytes=max_bytes)
    if method not in body_methods:
        reject_body(headers)
    return target


# Canonical route matching lives beside strict target parsing so adding a Team endpoint cannot make
# the hosted and local Controllers disagree about method/path semantics.
HOSTED_CONTROLLER = "hosted"
LOCAL_CONTROLLER = "local"
_BOTH_CONTROLLERS = frozenset({HOSTED_CONTROLLER, LOCAL_CONTROLLER})


@dataclass(frozen=True, slots=True)
class ControllerRoute:
    method: str
    pattern: tuple[str, ...]
    operation: str
    profiles: frozenset[str] = _BOTH_CONTROLLERS


@dataclass(frozen=True, slots=True)
class ControllerRouteMatch:
    operation: str
    params: dict[str, str]

    @property
    def group(self) -> str | None:
        fixed = {
            "health",
            "activity",
            "registry-list",
            "team-list",
            "space-bootstrap-reset",
            "space-reset",
            "assistant-integration-complete",
        }
        if self.operation in fixed:
            return "fixed"
        if self.operation in {"team-create", "team-destroy", "team-rename"}:
            return "team"
        if self.operation.startswith("local-assistant-"):
            return "local-assistant"
        for prefix, group in (
            ("routine-", "routine"),
            ("file-", "file"),
            ("inference-", "inference"),
            ("chat-", "chat"),
            ("assistant-integration-", "assistant-integration"),
            ("assistant-stored-input-", "assistant-stored-input"),
        ):
            if self.operation.startswith(prefix):
                return group
        return "chat" if self.operation == "chat" else None


def _controller_route(
    method: str,
    path: str,
    operation: str,
    profiles: frozenset[str] = _BOTH_CONTROLLERS,
) -> ControllerRoute:
    return ControllerRoute(method, tuple(part for part in path.split("/") if part), operation, profiles)


_HOSTED_CONTROLLER_ONLY = frozenset({HOSTED_CONTROLLER})
_LOCAL_CONTROLLER_ONLY = frozenset({LOCAL_CONTROLLER})
CONTROLLER_ROUTES = (
    _controller_route("GET", "/v1/teams", "team-list"),
    _controller_route("POST", "/v1/oauth/cloudflare/callback", "assistant-integration-complete"),
    _controller_route("POST", "/v1/teams/:team_id/create", "team-create"),
    _controller_route("DELETE", "/v1/teams/:team_id", "team-destroy"),
    _controller_route("PATCH", "/v1/teams/:team_id", "team-rename", _LOCAL_CONTROLLER_ONLY),
    _controller_route("GET", "/v1/teams/:team_id/files", "file-list"),
    _controller_route("POST", "/v1/teams/:team_id/files", "file-upload"),
    _controller_route("DELETE", "/v1/teams/:team_id/files/:file_id", "file-delete"),
    _controller_route("GET", "/v1/teams/:team_id/inference", "inference-status"),
    _controller_route("PUT", "/v1/teams/:team_id/inference", "inference-configure"),
    _controller_route("POST", "/v1/teams/:team_id/chat", "chat"),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/chat/capability-plan",
        "chat-capability-plan",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/chat/intent-route",
        "chat-intent-route",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route("GET", "/v1/teams/:team_id/chat/integrations", "chat-integration-pending"),
    _controller_route("POST", "/v1/teams/:team_id/chat/integrations", "chat-integration-submit"),
    _controller_route("GET", "/v1/teams/:team_id/chat/human", "chat-human-pending"),
    _controller_route("POST", "/v1/teams/:team_id/chat/human", "chat-human-submit"),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/chat/human/challenge",
        "chat-human-open",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route("POST", "/v1/teams/:team_id/chat/stop", "chat-stop"),
    _controller_route("GET", "/v1/teams/:team_id/assistant-integrations", "assistant-integration-list"),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/assistant-integrations/challenges/:challenge_id/authorize",
        "assistant-integration-authorize",
    ),
    _controller_route(
        "DELETE",
        "/v1/teams/:team_id/assistant-integrations/challenges/:challenge_id/authorize",
        "assistant-integration-cancel",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route(
        "DELETE",
        "/v1/teams/:team_id/assistant-integrations/:assistant_id/:integration_id",
        "assistant-integration-disconnect",
    ),
    _controller_route("GET", "/v1/teams/:team_id/assistant-stored-inputs", "assistant-stored-input-list"),
    _controller_route(
        "DELETE",
        "/v1/teams/:team_id/assistant-stored-inputs/:assistant_id/:stored_input_id",
        "assistant-stored-input-clear",
    ),
    _controller_route("POST", "/v1/teams/:team_id/chat/stream", "chat-stream", _HOSTED_CONTROLLER_ONLY),
    _controller_route("GET", "/v1/teams/:team_id/status", "team-status", _HOSTED_CONTROLLER_ONLY),
    _controller_route("GET", "/v1/teams/:team_id/logs", "team-logs", _HOSTED_CONTROLLER_ONLY),
    _controller_route("POST", "/v1/teams/:team_id/stop", "team-stop", _HOSTED_CONTROLLER_ONLY),
    _controller_route("POST", "/v1/teams/:team_id/start", "team-start", _HOSTED_CONTROLLER_ONLY),
    _controller_route("POST", "/v1/teams/:team_id/restart", "team-restart", _HOSTED_CONTROLLER_ONLY),
    # Team Routines (ADR-0086): Admin's scheduler claims and delivers under the Team bearer, its routine identity runs
    # one leased run, and a Supervisor session manages Routines and answers or ends their runs.
    _controller_route("POST", "/v1/routines/claim", "routine-claim", _LOCAL_CONTROLLER_ONLY),
    _controller_route("GET", "/v1/routines/notices", "routine-notices", _LOCAL_CONTROLLER_ONLY),
    _controller_route("POST", "/v1/routines/notices/ack", "routine-notice-ack", _LOCAL_CONTROLLER_ONLY),
    _controller_route("GET", "/v1/teams/:team_id/routines", "routine-list", _LOCAL_CONTROLLER_ONLY),
    # One page of a Routine revision's steps (ADR-0092 amendment, 2026-10-05, scale).
    _controller_route(
        "GET",
        "/v1/teams/:team_id/routines/:routine_id/revisions/:revision/steps/:offset",
        "routine-steps",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route("DELETE", "/v1/teams/:team_id/routines/:routine_id", "routine-delete", _LOCAL_CONTROLLER_ONLY),
    # A recorded Routine's card (ADR-0101): Criar rotina confirms it, Cancelar revokes it.
    _controller_route(
        "POST",
        "/v1/teams/:team_id/routines/proposals/:proposal_id",
        "routine-proposal-confirm",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route(
        "DELETE",
        "/v1/teams/:team_id/routines/proposals/:proposal_id",
        "routine-proposal-revoke",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route(
        "POST", "/v1/teams/:team_id/routines/runs/:run_id/segment", "routine-run", _LOCAL_CONTROLLER_ONLY
    ),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/routines/runs/:run_id/challenge",
        "routine-challenge-open",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route(
        "POST", "/v1/teams/:team_id/routines/runs/:run_id/human", "routine-human-submit", _LOCAL_CONTROLLER_ONLY
    ),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/routines/runs/:run_id/integrations",
        "routine-integration-submit",
        _LOCAL_CONTROLLER_ONLY,
    ),
    # A held run's recovery card (ADR-0092, ADR-0101): open it, then answer it once with Rodar.
    _controller_route(
        "POST",
        "/v1/teams/:team_id/routines/incidents/:incident_id/card",
        "routine-card-open",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/routines/incidents/:incident_id/answer",
        "routine-card-answer",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route(
        "POST", "/v1/teams/:team_id/routines/:routine_id/resume", "routine-resume", _LOCAL_CONTROLLER_ONLY
    ),
    _controller_route("POST", "/v1/teams/:team_id/routines/:routine_id/pause", "routine-pause", _LOCAL_CONTROLLER_ONLY),
    _controller_route(
        "GET",
        "/v1/teams/:team_id/routines/runs/:run_id/diagnostics",
        "routine-diagnostics",
        _LOCAL_CONTROLLER_ONLY,
    ),
    # What one run did step by step, a page of one snapshot of its records (ADR-0092 amendment, 2026-10-05, scale).
    _controller_route(
        "GET",
        "/v1/teams/:team_id/routines/runs/:run_id/steps/:snapshot/:offset",
        "routine-run-steps",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route("GET", "/healthz", "health", _LOCAL_CONTROLLER_ONLY),
    _controller_route("GET", "/v1/activity", "activity", _LOCAL_CONTROLLER_ONLY),
    _controller_route("GET", "/v1/local-assistants", "local-assistant-list", _LOCAL_CONTROLLER_ONLY),
    _controller_route(
        "GET",
        "/v1/local-assistants/:image_hash/icon",
        "local-assistant-icon",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route(
        "GET",
        "/v1/local-assistants/:image_hash/summary/:locale",
        "local-assistant-summary",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route("GET", "/v1/assistants", "registry-list", _LOCAL_CONTROLLER_ONLY),
    _controller_route("DELETE", "/v1/space/bootstrap", "space-bootstrap-reset", _LOCAL_CONTROLLER_ONLY),
    _controller_route("DELETE", "/v1/space", "space-reset", _LOCAL_CONTROLLER_ONLY),
    _controller_route("GET", "/v1/teams/:team_id/assistants", "assistant-list"),
    _controller_route(
        "GET",
        "/v1/teams/:team_id/assistants/:assistant_id/icon",
        "assistant-icon",
    ),
    _controller_route(
        "GET",
        "/v1/teams/:team_id/assistants/:assistant_id/summary/:locale",
        "assistant-summary",
    ),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/assistants/local/fresh",
        "local-assistant-fresh-install",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/assistants/:assistant_id/action-labels",
        "assistant-action-labels",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route("POST", "/v1/teams/:team_id/assistants", "assistant-install"),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/assistants/local",
        "local-assistant-install",
        _LOCAL_CONTROLLER_ONLY,
    ),
    _controller_route(
        "DELETE",
        "/v1/teams/:team_id/assistants/:assistant_id",
        "assistant-uninstall",
    ),
    _controller_route(
        "POST",
        "/v1/teams/:team_id/assistants/:assistant_id/actions/:action_id",
        "assistant-invoke",
        _LOCAL_CONTROLLER_ONLY,
    ),
)


def resolve_controller_route(profile: str, method: str, parts: tuple[str, ...]) -> ControllerRouteMatch | None:
    """Resolve one exact origin-form path without wildcard suffixes or method fallthrough."""
    if profile not in {HOSTED_CONTROLLER, LOCAL_CONTROLLER}:
        raise ValueError("unknown Controller routing profile")
    for route in CONTROLLER_ROUTES:
        if profile not in route.profiles or method != route.method or len(parts) != len(route.pattern):
            continue
        params: dict[str, str] = {}
        for expected, actual in zip(route.pattern, parts, strict=True):
            if expected.startswith(":"):
                params[expected[1:]] = actual
            elif expected != actual:
                break
        else:
            return ControllerRouteMatch(route.operation, params)
    return None


def capture_body(
    headers: object,
    stream: BinaryIO,
    operation: str,
    json_limits: Mapping[str, int],
    file_max_bytes: int,
) -> CapturedBody:
    """Capture the body an operation admits exactly once: file metadata, one bounded JSON document, or nothing.

    A file's content stays unread until the caller authorizes it; JSON and the empty body bind their exact bytes.
    """
    if operation == "file-upload":
        metadata = file_upload_metadata(headers, max_bytes=file_max_bytes)
        binding = {
            "kind": "file",
            "length": metadata.length,
            "filename": metadata.filename,
            "media_type": metadata.media_type,
        }
        return CapturedBody(binding, file=metadata)
    limit = json_limits.get(operation)
    if limit is not None:
        raw, document = read_json_document(headers, stream, max_bytes=limit)
        binding = {"kind": "json", "length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        return CapturedBody(binding, raw, document)
    reject_body(headers)
    return CapturedBody({"kind": "none", "length": 0, "sha256": _EMPTY_SHA256})
