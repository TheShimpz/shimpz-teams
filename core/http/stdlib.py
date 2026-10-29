"""Small fail-closed primitives for stdlib HTTP control-plane services."""

import hmac
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http import HTTPStatus
from typing import BinaryIO
from urllib.parse import urlsplit

# Twenty digits hold every 64-bit length, so a longer length without its leading zeros exceeds every body bound.
MAX_LENGTH_DIGITS = 20
# An RFC 9112 request target holds only visible ASCII.
_REQUEST_TARGET = re.compile(r"[!-~]*")


class HttpError(ValueError):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern[str]
    operation: str


@dataclass(frozen=True)
class HttpFailure:
    status: HTTPStatus
    public_message: str
    audit_reason: str
    result: str
    public_code: str | None = None


def bearer_token(headers: object) -> str:
    """Return one exactly framed bearer value, otherwise the empty string."""
    get_all = getattr(headers, "get_all", None)
    values = get_all("Authorization", failobj=[]) if get_all is not None else []
    if len(values) != 1:
        return ""
    scheme, separator, value = values[0].partition(" ")
    return value if separator and scheme == "Bearer" else ""


def bearer_authorized(headers: object, token: str) -> bool:
    """Accept one exact bearer header and compare it in constant time; a non-ASCII token never authorizes."""
    supplied = bearer_token(headers)
    # Headers decode as Latin-1, and compare_digest raises on non-ASCII text instead of refusing it.
    return bool(supplied) and supplied.isascii() and token.isascii() and hmac.compare_digest(supplied, token)


def _content_length(headers: object, max_bytes: int) -> int:
    """Return the one declared length: absent or empty is 0, a repeated header or a non-digit is refused."""
    get_all = getattr(headers, "get_all", None)
    values = get_all("Content-Length", failobj=[]) if get_all is not None else [headers.get("Content-Length", "")]
    if len(values) > 1:
        raise HttpError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
    raw_length = values[0] if values else ""
    if raw_length and not (raw_length.isascii() and raw_length.isdigit()):
        raise HttpError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
    digits = raw_length.lstrip("0") or "0"
    if len(digits) > MAX_LENGTH_DIGITS or int(digits) > max_bytes:
        raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large")
    return int(digits)


def read_json_body(
    headers: object, stream: BinaryIO, *, max_bytes: int, loads: Callable[[bytes], object]
) -> dict[str, object]:
    """Read one bounded JSON object, decoded by the caller's strict decoder."""
    length = _content_length(headers, max_bytes)
    if length == 0:
        return {}
    try:
        body = loads(stream.read(length))
    # Malformed text, bytes that are not UTF-8, and nesting too deep to decode are all the client's invalid body.
    except (ValueError, RecursionError) as exc:
        raise HttpError(HTTPStatus.BAD_REQUEST, "invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HttpError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
    return body


def resolve_route(routes: Iterable[Route], method: str, raw_target: str) -> str:
    """Return the operation of the route that matches the method and the whole path, ignoring any query.

    A target with a byte outside visible ASCII, or one urlsplit cannot split, is refused before any route is tried.
    """
    if _REQUEST_TARGET.fullmatch(raw_target) is None:
        raise HttpError(HTTPStatus.BAD_REQUEST, "invalid request target")
    try:
        target = urlsplit(raw_target)
    except ValueError as exc:
        raise HttpError(HTTPStatus.BAD_REQUEST, "invalid request target") from exc
    for route in routes:
        if route.method == method and route.pattern.fullmatch(target.path) is not None:
            return route.operation
    raise HttpError(HTTPStatus.NOT_FOUND, f"no route for {method} {target.path}")


def dispatch(
    action: Callable[[], None],
    *,
    classify: Callable[[Exception], HttpFailure | None],
    emit: Callable[[HttpFailure], None],
    unexpected_message: str,
) -> None:
    """Run one HTTP action and redact every unclassified ordinary exception."""
    try:
        action()
    except Exception as exc:
        failure = classify(exc)
        if failure is None:
            failure = HttpFailure(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                unexpected_message,
                "unclassified",
                "error",
            )
        emit(failure)
