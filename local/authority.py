"""Verify one-use request-bound Local Supervisor assertions."""

from __future__ import annotations

import base64
import binascii
import grp
import hmac
import json
import os
import stat
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from email.message import Message
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from protocol.http.v1 import supervisor as contract

PUBLIC_KEY_FILE = Path(
    os.environ.get(
        "SHIMPZ_LOCAL_SUPERVISOR_PUBLIC_KEY_FILE",
        "/run/shimpz-local-supervisor/public.pem",
    )
)
PUBLIC_KEY_GROUP = "shimpzsupervisor-key"
MAX_ASSERTION_BYTES = 8192
MAX_REPLAY_ENTRIES = 16 * 1024


class SupervisorDeniedError(RuntimeError):
    """The request did not carry valid Local Supervisor evidence."""


class SupervisorUnavailableError(RuntimeError):
    """Local Supervisor evidence could not be evaluated safely."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """Attributable human evidence accepted for one exact Local request."""

    supervisor_id: str
    session_digest: str
    assertion_id: str
    expires_at: int


class ReplayGuard:
    """Bounded one-use assertion guard; restart exposure is limited by the 15-second TTL."""

    def __init__(self, *, capacity: int = MAX_REPLAY_ENTRIES) -> None:
        if not 1 <= capacity <= MAX_REPLAY_ENTRIES:
            raise ValueError("invalid Local Supervisor replay capacity")
        self._capacity = capacity
        self._lock = threading.Lock()
        self._seen: dict[str, int] = {}

    def consume(self, assertion_id: str, expires_at: int, *, now: int) -> None:
        with self._lock:
            self._seen = {
                stored_id: stored_expiry for stored_id, stored_expiry in self._seen.items() if stored_expiry >= now
            }
            if assertion_id in self._seen:
                raise SupervisorDeniedError("Local Supervisor assertion was replayed")
            if len(self._seen) >= self._capacity:
                raise SupervisorUnavailableError("Local Supervisor replay protection is saturated")
            self._seen[assertion_id] = expires_at


_REPLAY_GUARD = ReplayGuard()


def _one_assertion(headers: Message) -> str:
    values = headers.get_all(contract.ASSERTION_HEADER, failobj=[])
    if len(values) != 1 or not values[0].startswith("Bearer "):
        raise SupervisorDeniedError("Local Supervisor assertion is required")
    encoded = values[0].removeprefix("Bearer ")
    if (
        not encoded
        or len(encoded) > MAX_ASSERTION_BYTES
        or not encoded.isascii()
        or any(character.isspace() for character in encoded)
    ):
        raise SupervisorDeniedError("Local Supervisor assertion is invalid")
    return encoded


def credential_state(headers: Message) -> str:
    """Classify only the assertion header shape for metadata-only audit."""
    try:
        _one_assertion(headers)
    except SupervisorDeniedError:
        return "assertion_absent_or_malformed"
    return "assertion_present"


def _public_key() -> Ed25519PublicKey:
    descriptor: int | None = None
    try:
        expected_gid = grp.getgrnam(PUBLIC_KEY_GROUP).gr_gid
        descriptor = os.open(PUBLIC_KEY_FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_gid != expected_gid
            or stat.S_IMODE(metadata.st_mode) != 0o440
            or not 1 <= metadata.st_size <= 512
        ):
            raise OSError("invalid Local Supervisor public-key metadata")
        raw = os.read(descriptor, 513)
    except (KeyError, OSError) as exc:
        raise SupervisorUnavailableError("Local Supervisor public key is unavailable") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
    if len(raw) != metadata.st_size:
        raise SupervisorUnavailableError("Local Supervisor public key is unavailable")
    try:
        key = load_pem_public_key(raw)
    except ValueError as exc:
        raise SupervisorUnavailableError("Local Supervisor public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SupervisorUnavailableError("Local Supervisor public key is invalid")
    return key


def _encode_segment(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_segment(encoded: str) -> bytes:
    if not encoded.isascii() or "=" in encoded:
        raise SupervisorDeniedError("Local Supervisor assertion is malformed")
    try:
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise SupervisorDeniedError("Local Supervisor assertion is malformed") from exc
    if _encode_segment(raw) != encoded:
        raise SupervisorDeniedError("Local Supervisor assertion is malformed")
    return raw


def _json_segment(encoded: str) -> object:
    raw = _decode_segment(encoded)
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SupervisorDeniedError("Local Supervisor assertion is malformed") from exc
    try:
        canonical = contract.canonical_json(value)
    except contract.SupervisorAssertionError as exc:
        raise SupervisorDeniedError("Local Supervisor assertion is malformed") from exc
    if not hmac.compare_digest(_encode_segment(canonical), encoded):
        raise SupervisorDeniedError("Local Supervisor assertion is not canonical")
    return value


def _verified_claims(encoded: str, key: Ed25519PublicKey) -> dict[str, object]:
    parts = encoded.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise SupervisorDeniedError("Local Supervisor assertion is malformed")
    header = _json_segment(parts[0])
    untrusted_claims = _json_segment(parts[1])
    if header != contract.JWT_HEADER:
        raise SupervisorDeniedError("Local Supervisor assertion header is invalid")
    try:
        claims = contract.canonical_claims(untrusted_claims)
    except contract.SupervisorAssertionError as exc:
        raise SupervisorDeniedError("Local Supervisor assertion claims are invalid") from exc
    signature = _decode_segment(parts[2])
    try:
        key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"))
    except InvalidSignature as exc:
        raise SupervisorDeniedError("Local Supervisor assertion signature is invalid") from exc
    return claims


def verify(
    headers: Message,
    *,
    method: str,
    path: str,
    body: dict[str, object],
    model: dict[str, str] | None,
    replay_guard: ReplayGuard | None = None,
    now: int | None = None,
) -> Evidence:
    """Verify and atomically consume evidence for one exact request."""
    encoded = _one_assertion(headers)
    key = _public_key()
    claims = _verified_claims(encoded, key)
    current = int(time.time()) if now is None else now
    issued_at = claims["iat"]
    expires_at = claims["exp"]
    if (
        not isinstance(issued_at, int)
        or not isinstance(expires_at, int)
        or issued_at > current + contract.ASSERTION_CLOCK_SKEW_SECONDS
        or expires_at < current
    ):
        raise SupervisorDeniedError("Local Supervisor assertion is outside its valid time")
    expected: dict[str, object] = {
        "method": method,
        "path": path,
        "body": body,
    }
    actual = {field: claims[field] for field in expected}
    if actual != expected or claims.get("model") != model:
        raise SupervisorDeniedError("Local Supervisor assertion does not match the request")
    guard = _REPLAY_GUARD if replay_guard is None else replay_guard
    assertion_id = claims["jti"]
    if not isinstance(assertion_id, str):
        raise SupervisorDeniedError("Local Supervisor assertion is invalid")
    guard.consume(assertion_id, expires_at, now=current)
    return Evidence(
        supervisor_id=str(claims["sub"]),
        session_digest=str(claims["session_sha256"]),
        assertion_id=assertion_id,
        expires_at=expires_at,
    )
