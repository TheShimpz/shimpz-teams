"""Team-made provider calls of one Action attempt: admission, credential injection, transport, and audit.

The Assistant never holds a credential. Its Action asks for each provider call on its exec channel, and Team admits the
call against the reviewed declarations, injects every credential the Action declares for that host, sends it through
the Assistant's own egress policy, and returns only a complete, credential-free response (ADR-0106).
"""

import base64
import binascii
import hashlib
import hmac
import http.client
import json
import ssl
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from action import dispatch as action_dispatch
from action import failure as action_failure
from action import human as action_human
from assistant import manifest as assistant_manifest
from integrations import http as integration_http
from integrations import providers as integration_providers
from protocol.http.v1 import strict_json

MAX_CALLS = 16
MAX_URL_CHARACTERS = 8192
MAX_HEADERS = 32
MAX_HEADER_BYTES = 8 * 1024
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_HEADER_BYTES = 64 * 1024
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
PROXY_HOST = "shimpz-assistant-egress"
PROXY_PORT = 8889
# Team-wide: at most this many provider calls are in flight, which bounds their buffers within Team's memory limit.
MAX_CONCURRENT_CALLS = 4
_FRAME_FIELDS = frozenset({"type", "method", "url", "headers", "body", "timeout_ms"})
_STOP_POLL_SECONDS = 0.25
_CHUNK_BYTES = 64 * 1024
_TOKEN_CHARACTERS = frozenset("!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_TLS = ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")
_CAPACITY = threading.BoundedSemaphore(MAX_CONCURRENT_CALLS)


class CallRefusedError(RuntimeError):
    """Team did not send the call; ``code`` is the closed error the Action receives."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True, repr=False)
class Credential:
    """One credential placed in calls to its host: a header or a query parameter, never both."""

    id: str
    host: str
    header: str | None
    query: str | None
    value: str
    # Every raw and placed form a response must never echo.
    protected: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class CallScope:
    """Everything one attempt's calls are admitted against; no value comes from the Action."""

    team_id: str
    assistant_id: str
    action_id: str
    operation_id: str
    credentials: tuple[Credential, ...]
    # Hosts that a declared credential of this Action goes to while Team does not hold it.
    missing: frozenset[str]
    # False while the Action declares an authorization capability its transcript does not yet hold.
    authorized: bool
    # The Assistant's admitted egress policy token and hosts, read only once the attempt makes its first call; it
    # raises ``CallRefusedError`` when the policy is unavailable.
    route: Callable[[], tuple[str, frozenset[str]]]
    # Writes one metadata-only audit record; it raises when the record cannot be written.
    audit: Callable[[Mapping[str, object]], None]
    stopped: Callable[[], bool] = field(default=lambda: False)


@dataclass(frozen=True, slots=True, repr=False)
class _Call:
    method: str
    host: str
    target: str
    headers: tuple[tuple[str, str], ...]
    body: bytes | None
    timeout: float | None


@dataclass(frozen=True, slots=True)
class Attempt:
    """The one Action attempt a broker serves."""

    team_id: str
    assistant_id: str
    action_id: str
    operation_id: str


def call_scope(
    attempt: Attempt,
    spec: object,
    action: object,
    private: object,
    route: Callable[[], tuple[str, frozenset[str]]],
    audit: Callable[[Mapping[str, object]], None],
) -> CallScope:
    """Admit one attempt's calls against its reviewed declarations and the private values Team resolved for it.

    ``spec`` carries the Assistant's reviewed ``stored_inputs`` and ``integrations``; ``private`` the attempt's held
    Stored Input values, Integration envelopes, and transcript. The scope observes the dispatching turn's Stop.
    """
    credentials, missing = action_credentials(
        action,
        spec.stored_inputs,
        private.stored_inputs,
        private.integrations,
        {integration_id: integration.provider for integration_id, integration in spec.integrations.items()},
    )
    declared = [kind for kind in action.human_requests if kind in action_human.AUTHORIZATION_KINDS]
    return CallScope(
        team_id=attempt.team_id,
        assistant_id=attempt.assistant_id,
        action_id=attempt.action_id,
        operation_id=attempt.operation_id,
        credentials=credentials,
        missing=missing,
        authorized=not declared or any(response.kind == declared[0] for response in private.transcript.responses),
        route=route,
        audit=audit,
        stopped=action_dispatch.current_stop(),
    )


def action_credentials(
    action: object,
    stored_inputs: Mapping[str, object],
    held: Mapping[str, str],
    integrations: Mapping[str, Mapping[str, object]],
    providers: Mapping[str, object],
) -> tuple[tuple[Credential, ...], frozenset[str]]:
    """The credentials one Action declares, placed for injection, and the hosts whose declared credential is missing.

    ``held`` maps each held Stored Input id to its value; ``integrations`` maps each connected Integration id to its
    bearer envelope, and ``providers`` each Integration id to its reviewed provider id.
    """
    credentials: list[Credential] = []
    missing: set[str] = set()
    for stored_input_id in getattr(action, "stored_inputs", ()):
        declaration = stored_inputs[stored_input_id]
        signed = getattr(declaration, "hmac", None)
        value = held.get(stored_input_id)
        if value is None or (signed is not None and held.get(signed) is None):
            missing.add(declaration.host)
            continue
        placed = _proof(value, held[signed]) if signed is not None else value
        if getattr(declaration, "scheme", None) is not None:
            placed = f"{declaration.scheme} {placed}"
        credentials.append(
            Credential(
                f"stored-input:{stored_input_id}",
                declaration.host,
                None if declaration.header is None else declaration.header.lower(),
                declaration.query,
                placed,
                (value, placed),
            )
        )
    for integration_id in getattr(action, "integrations", ()):
        provider = integration_providers.resolve(providers[integration_id])
        token = integrations.get(integration_id, {}).get("access_token")
        if not isinstance(token, str) or not token:
            missing.update(provider.api_hosts)
            continue
        bearer = f"Bearer {token}"
        credentials.extend(
            Credential(f"integration:{integration_id}", host, "authorization", None, bearer, (token, bearer))
            for host in provider.api_hosts
        )
    return tuple(credentials), frozenset(missing)


def _proof(key: str, message: str) -> str:
    """The lowercase hexadecimal HMAC-SHA256 keyed by one value over another, as Meta's ``appsecret_proof``."""
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


class Broker:
    """Answer the provider calls of one Action attempt, strictly in order, within the attempt's deadline."""

    def __init__(self, scope: CallScope) -> None:
        self._scope = scope
        self._route: tuple[str, frozenset[str]] | None = None
        self.calls = 0

    def __call__(self, frame: object, deadline: float) -> bytes:
        """Return the encoded one-line reply to one ``fetch`` frame."""
        self.calls += 1
        ordinal = self.calls
        try:
            call = self._admit(frame)
            reply = self._send(call, ordinal, deadline)
        except _ReportedError as exc:
            reply = {"error": exc.code}
        except CallRefusedError as exc:
            self._scope.audit({"phase": "refused", "call": ordinal, "error": exc.code, "reason": exc.reason})
            reply = {"error": exc.code}
        return json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode("ascii")

    def _admit(self, frame: object) -> _Call:
        scope = self._scope
        if self.calls > MAX_CALLS:
            raise CallRefusedError("refused", "call-limit")
        if not scope.authorized:
            raise CallRefusedError("refused", "authorization-pending")
        if self._route is None:
            self._route = scope.route()
        call = _parse(frame, self._route[1])
        owned = _owned_fields(scope.credentials, call.host)
        if any(name in owned for name, _value in call.headers) or _query_names(call.target) & owned:
            raise CallRefusedError("refused", "credential-field")
        if call.host in scope.missing:
            raise CallRefusedError("credential-missing", "credential-missing")
        return _inject(call, scope.credentials)

    def _send(self, call: _Call, ordinal: int, deadline: float) -> dict[str, object]:
        scope = self._scope
        injected = sorted({credential.id for credential in scope.credentials if credential.host == call.host})
        _acquire(deadline, scope.stopped)
        try:
            scope.audit(
                {
                    "phase": "dispatch",
                    "call": ordinal,
                    "method": call.method,
                    "host": call.host,
                    "credentials": injected,
                }
            )
            started = time.monotonic()
            status, headers, body = _transport(call, (self._route or ("", frozenset()))[0], scope.stopped, deadline)
            _require_clean(headers, body, scope.credentials)
        except CallRefusedError as exc:
            scope.audit({"phase": "outcome", "call": ordinal, "error": exc.code, "reason": exc.reason})
            raise _ReportedError(exc) from None
        finally:
            _CAPACITY.release()
        scope.audit(
            {
                "phase": "outcome",
                "call": ordinal,
                "status": status,
                "request_bytes": len(call.body or b""),
                "response_bytes": len(body),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        )
        return {"status": status, "headers": [list(item) for item in headers], "body": _b64(body)}


class _ReportedError(CallRefusedError):
    """A refusal after dispatch whose outcome record is already written."""

    def __init__(self, cause: CallRefusedError) -> None:
        super().__init__(cause.code, cause.reason)


def _acquire(deadline: float, stopped: Callable[[], bool]) -> None:
    """Wait for Team-wide call capacity, observing Stop and the deadline; nothing has been sent on refusal."""
    while True:
        if stopped():
            raise CallRefusedError("refused", "stopped")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CallRefusedError("unavailable", "deadline")
        if _CAPACITY.acquire(timeout=min(_STOP_POLL_SECONDS, remaining)):
            if stopped():
                _CAPACITY.release()
                raise CallRefusedError("refused", "stopped")
            return


def _call_deadline(call: _Call, deadline: float) -> float:
    return deadline if call.timeout is None else min(deadline, time.monotonic() + call.timeout)


def _parse(frame: object, allowed_hosts: frozenset[str]) -> _Call:
    """Admit one closed ``fetch`` frame for one of the Assistant's reviewed hosts."""
    if (
        not isinstance(frame, dict)
        or frame.get("type") != "fetch"
        or not {"type", "method", "url", "headers"} <= set(frame) <= _FRAME_FIELDS
        or frame["method"] not in METHODS
    ):
        raise CallRefusedError("refused", "frame")
    host, target = _url(frame["url"], allowed_hosts)
    timeout = frame.get("timeout_ms")
    if timeout is not None and (type(timeout) is not int or not 1 <= timeout <= 30_000):
        raise CallRefusedError("refused", "timeout")
    return _Call(
        frame["method"],
        host,
        target,
        _headers(frame["headers"]),
        _body(frame.get("body")),
        None if timeout is None else timeout / 1000,
    )


def _url(value: object, allowed_hosts: frozenset[str]) -> tuple[str, str]:
    if (
        not isinstance(value, str)
        or not 9 <= len(value) <= MAX_URL_CHARACTERS
        or not value.isascii()
        or any(character <= " " or character == "\x7f" for character in value)
    ):
        raise CallRefusedError("refused", "url")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise CallRefusedError("refused", "url") from None
    if (
        parts.scheme != "https"
        or parts.fragment
        or "#" in value
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.netloc != parts.hostname
        or parts.hostname not in allowed_hosts
    ):
        raise CallRefusedError("refused", "host")
    path = parts.path or "/"
    if not path.startswith("/"):
        raise CallRefusedError("refused", "url")
    return parts.hostname, path if not parts.query else f"{path}?{parts.query}"


def _headers(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) > MAX_HEADERS:
        raise CallRefusedError("refused", "headers")
    headers: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
            or not 1 <= len(item[0]) <= 64
            or not set(item[0]) <= _TOKEN_CHARACTERS
            or not _field_value(item[1])
        ):
            raise CallRefusedError("refused", "headers")
        name = item[0].lower()
        if name in assistant_manifest.RESERVED_HEADERS:
            raise CallRefusedError("refused", "reserved-header")
        headers.append((name, item[1]))
    if len({name for name, _field in headers}) != len(headers) or (
        sum(len(name) + len(field) for name, field in headers) > MAX_HEADER_BYTES
    ):
        raise CallRefusedError("refused", "headers")
    return tuple(headers)


def _field_value(value: str) -> bool:
    """A header value without a line break or any other control character than tab."""
    return all(character == "\t" or " " <= character != "\x7f" for character in value)


def _body(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > (MAX_REQUEST_BYTES + 2) // 3 * 4:
        raise CallRefusedError("refused", "body")
    try:
        body = base64.b64decode(value, validate=True)
    except binascii.Error:
        raise CallRefusedError("refused", "body") from None
    if len(body) > MAX_REQUEST_BYTES:
        raise CallRefusedError("refused", "body")
    return body


def _owned_fields(credentials: tuple[Credential, ...], host: str) -> set[str]:
    """The header and query names Team's own placements use on one host."""
    return {
        name for credential in credentials if credential.host == host for name in (credential.header, credential.query)
    } - {None}


def _query_names(target: str) -> set[str]:
    query = target.partition("?")[2]
    return {name for name, _value in parse_qsl(query, keep_blank_values=True)} | {
        unquote(pair.partition("=")[0]) for pair in query.split("&")
    }


def _inject(call: _Call, credentials: tuple[Credential, ...]) -> _Call:
    """Place every credential of the call's host; a value that cannot travel in its field refuses the call."""
    headers = list(call.headers)
    target = call.target
    for credential in credentials:
        if credential.host != call.host:
            continue
        if credential.header is not None:
            if not _field_value(credential.value):
                raise CallRefusedError("refused", "credential-value")
            headers.append((credential.header, credential.value))
        else:
            separator = "&" if "?" in target else "?"
            target = f"{target}{separator}{credential.query}={quote(credential.value, safe='')}"
    return _Call(call.method, call.host, target, tuple(headers), call.body, call.timeout)


def _transport(
    call: _Call, proxy_token: str, stopped: Callable[[], bool], deadline: float
) -> tuple[int, tuple[tuple[str, str], ...], bytes]:
    """Send one call through the Assistant's egress policy and read its complete bounded response by the deadline."""
    remaining = _call_deadline(call, deadline) - time.monotonic()
    if remaining <= 0:
        raise CallRefusedError("unavailable", "deadline")
    connection = http.client.HTTPSConnection(PROXY_HOST, PROXY_PORT, timeout=remaining, context=_TLS)
    proxy = base64.b64encode(f"{proxy_token}:".encode("ascii")).decode("ascii")
    connection.set_tunnel(call.host, 443, headers={"Proxy-Authorization": f"Basic {proxy}"})
    try:
        with integration_http.ExchangeDeadline(connection, remaining) as guard, _Observing(guard, stopped):
            try:
                connection.connect()
            except OSError:
                raise CallRefusedError("unavailable", "connect") from None
            try:
                return _exchange(connection, call, guard)
            except OSError, http.client.HTTPException:
                raise CallRefusedError("failed", "timeout" if guard.expired else "transport") from None
    finally:
        connection.close()


def _exchange(
    connection: http.client.HTTPSConnection, call: _Call, guard: integration_http.ExchangeDeadline
) -> tuple[int, tuple[tuple[str, str], ...], bytes]:
    headers = {**dict(call.headers), "accept-encoding": "identity"}
    connection.request(call.method, call.target, body=call.body, headers=headers)
    response = connection.getresponse()
    received = tuple((name.lower(), value) for name, value in response.getheaders())
    encoding = response.getheader("Content-Encoding", "identity").strip().lower()
    if encoding not in {"", "identity"} or sum(len(name) + len(value) for name, value in received) > (
        MAX_RESPONSE_HEADER_BYTES
    ):
        raise CallRefusedError("failed", "response-headers")
    body = bytearray()
    while chunk := response.read1(_CHUNK_BYTES):
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise CallRefusedError("failed", "response-size")
    if guard.expired:
        raise CallRefusedError("failed", "timeout")
    return response.status, received, bytes(body)


class _Observing:
    """Expire a call's deadline guard as soon as the turn is stopped."""

    def __init__(self, guard: integration_http.ExchangeDeadline, stopped: Callable[[], bool]) -> None:
        self._guard = guard
        self._stopped = stopped
        self._done = threading.Event()
        self._watcher = threading.Thread(target=self._watch, name="provider-call-stop", daemon=True)

    def __enter__(self) -> None:
        self._watcher.start()

    def __exit__(self, *_exc: object) -> None:
        self._done.set()
        self._watcher.join()

    def _watch(self) -> None:
        while not self._done.wait(_STOP_POLL_SECONDS):
            if self._stopped():
                self._guard.expire()
                return


def _require_clean(headers: tuple[tuple[str, str], ...], body: bytes, credentials: tuple[Credential, ...]) -> None:
    """Refuse a response that carries any injected value in a common encoding or inside a decoded JSON string."""
    secrets = tuple(dict.fromkeys(secret for credential in credentials for secret in credential.protected if secret))
    forms = tuple({form.lower() for secret in secrets for form in (secret, *action_failure.encodings(secret)) if form})
    texts = (*(value.lower() for _name, value in headers), body.decode("latin-1").lower())
    if any(form in text for form in forms for text in texts) or _json_echoes(body, secrets):
        raise CallRefusedError("failed", "credential-echo")


def _json_echoes(body: bytes, secrets: tuple[str, ...]) -> bool:
    try:
        document = strict_json.loads(body)
    except UnicodeError, ValueError, RecursionError:
        return False
    return any(secret in text for text in _strings(document) for secret in secrets)


def _strings(document: object) -> Iterator[str]:
    pending = [document]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            yield current
        elif isinstance(current, dict):
            yield from current
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
