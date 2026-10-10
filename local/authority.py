"""Verify one-use request-bound Local Supervisor assertions."""

import grp
import hashlib
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
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_public_key

from core import base64url
from protocol.http.v1 import supervisor as contract
from storage import private_state

PUBLIC_KEY_FILE = Path(
    os.environ.get(
        "SHIMPZ_LOCAL_SUPERVISOR_PUBLIC_KEY_FILE",
        "/run/shimpz-local-supervisor/public.pem",
    )
)
# The Routine identity's public key sits beside the Supervisor's, in the same Admin-owned directory (ADR-0086).
ROUTINE_PUBLIC_KEY_FILE = PUBLIC_KEY_FILE.with_name("routine.pem")
PUBLIC_KEY_GROUP = "shimpzsupervisor-key"
# Team's own pin of the Supervisor verification key, in Team's private settings volume. Team pins the key Admin
# publishes the first time it verifies; from then on only a rotation signed by the pinned key changes it, and only a
# bootstrap reset, which proves Admin publishes no key, removes it.
VERIFIER_FILE = Path("/var/lib/shimpz-local/inference/supervisor.pem")
_VERIFIER_LOCK = threading.Lock()
MAX_ASSERTION_BYTES = 8192
MAX_REPLAY_ENTRIES = 16 * 1024


class SupervisorDeniedError(RuntimeError):
    """The request did not carry valid Local Supervisor evidence."""


class SupervisorUnavailableError(RuntimeError):
    """Local Supervisor evidence could not be evaluated safely."""


class SupervisorEstablishedError(RuntimeError):
    """A valid Local Supervisor identity already exists."""


@dataclass(frozen=True, slots=True)
class Evidence:
    """Attributable human evidence accepted for one exact Local request."""

    supervisor_id: str
    authority_kind: str
    authority_digest: str
    assertion_id: str
    expires_at: int
    # The SHA-256 of the raw pinned key that verified the assertion.
    key_sha256: str = ""


@dataclass(frozen=True, slots=True)
class RequestBinding:
    """Exact HTTP request and optional credential bindings expected by Team."""

    method: str
    path: str
    body: dict[str, object]
    model: dict[str, str] | None
    assurance: dict[str, str] | None
    authority_kinds: frozenset[str]
    decision: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class RoutineEvidence:
    """Machine evidence that Admin's Routine identity drives one exact run under one lease."""

    key_fingerprint: str
    lease_sha256: str
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


def _one_assertion(headers: Message, header: str = contract.ASSERTION_HEADER) -> str:
    values = headers.get_all(header, failobj=[])
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


def _read_public_key(descriptor: int, expected_gid: int) -> bytes:
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
    if len(raw) != metadata.st_size:
        raise OSError("truncated Local Supervisor public key")
    return raw


def _parse_public_key(raw: bytes) -> Ed25519PublicKey:
    try:
        key = load_pem_public_key(raw)
    except ValueError as exc:
        raise SupervisorUnavailableError("Local Supervisor public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SupervisorUnavailableError("Local Supervisor public key is invalid")
    return key


def _public_key(path: Path | None = None) -> Ed25519PublicKey:
    descriptor = -1
    try:
        expected_gid = grp.getgrnam(PUBLIC_KEY_GROUP).gr_gid
        descriptor = os.open(path or PUBLIC_KEY_FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        raw = _read_public_key(descriptor, expected_gid)
    except (KeyError, OSError) as exc:
        raise SupervisorUnavailableError("Local Supervisor public key is unavailable") from exc
    finally:
        with suppress(OSError):
            os.close(descriptor)
    return _parse_public_key(raw)


def key_sha256(key: Ed25519PublicKey) -> str:
    """The SHA-256 of one Ed25519 verification key's 32 raw bytes, in lowercase hex."""
    return hashlib.sha256(key.public_bytes(Encoding.Raw, PublicFormat.Raw)).hexdigest()


def _read_pin() -> Ed25519PublicKey | None:
    """Team's pinned Supervisor key, or None before the first verification; a damaged pin is unavailable."""
    try:
        descriptor = os.open(VERIFIER_FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SupervisorUnavailableError("Team's Supervisor key is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 1 <= metadata.st_size <= 512
        ):
            raise SupervisorUnavailableError("Team's Supervisor key has unsafe metadata")
        raw = os.read(descriptor, 513)
    except OSError as exc:
        raise SupervisorUnavailableError("Team's Supervisor key is unavailable") from exc
    finally:
        os.close(descriptor)
    return _parse_public_key(raw)


def _write_pin(key: Ed25519PublicKey) -> None:
    """Atomically and durably replace Team's pin: a crash leaves the complete earlier key or the complete new one."""
    try:
        VERIFIER_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_state.replace_durably(VERIFIER_FILE, key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    except OSError as exc:
        raise SupervisorUnavailableError("Team's Supervisor key could not be saved") from exc


def supervisor_key() -> Ed25519PublicKey:
    """The key every Supervisor assertion must verify under: Team's pin, first taken from Admin's published key."""
    with _VERIFIER_LOCK:
        pinned = _read_pin()
        if pinned is not None:
            return pinned
        published = _public_key()
        _write_pin(published)
        return published


def rotate_supervisor_key(body: object, signed_by: str) -> dict[str, object]:
    """Replace Team's pin with the new key of a rotation whose assertion the current pin verified.

    ``signed_by`` is the SHA-256 of the key that verified the rotation's assertion. A retry of a rotation that already
    took effect, signed by either key, succeeds unchanged; a rotation verified by a key that is no longer pinned is
    refused, so two rotations signed by one key never both take effect. From the switch on, the earlier key's
    assertions are refused.
    """
    admitted = contract.canonical_key_rotation(body)
    if admitted is None:
        raise SupervisorDeniedError("Supervisor key rotation is invalid")
    try:
        new = Ed25519PublicKey.from_public_bytes(base64url.decode(admitted["public_key"]))
    except ValueError as exc:
        raise SupervisorDeniedError("Supervisor key rotation is invalid") from exc
    fingerprint = key_sha256(new)
    with _VERIFIER_LOCK:
        current = _read_pin()
        if current is None:
            raise SupervisorUnavailableError("Team's Supervisor key is unavailable")
        pinned = key_sha256(current)
        if pinned != fingerprint:
            if not hmac.compare_digest(pinned, signed_by):
                raise SupervisorDeniedError("Supervisor key rotation was signed by a key that is no longer current")
            _write_pin(new)
    return {"rotated": True, "key_sha256": fingerprint}


def forget_supervisor_key() -> None:
    """Remove Team's pin after a bootstrap reset proved that Admin publishes no Supervisor key; absence is success."""
    with _VERIFIER_LOCK:
        try:
            VERIFIER_FILE.unlink(missing_ok=True)
            private_state.fsync_directory(VERIFIER_FILE.parent)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SupervisorUnavailableError("Team's Supervisor key could not be removed") from exc


def _public_key_directory(expected_gid: int) -> int:
    descriptor = os.open(
        PUBLIC_KEY_FILE.parent,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_gid != expected_gid
            or stat.S_IMODE(metadata.st_mode) != 0o2770
        ):
            raise OSError("invalid Local Supervisor public-key directory")
    except OSError:
        with suppress(OSError):
            os.close(descriptor)
        raise
    return descriptor


def require_supervisor_absent() -> None:
    """Require safe proof that no Local Supervisor identity has been established."""
    directory_descriptor = -1
    key_descriptor = -1
    try:
        expected_gid = grp.getgrnam(PUBLIC_KEY_GROUP).gr_gid
        directory_descriptor = _public_key_directory(expected_gid)
        try:
            key_descriptor = os.open(
                PUBLIC_KEY_FILE.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=directory_descriptor,
            )
        except FileNotFoundError:
            return
        raw = _read_public_key(key_descriptor, expected_gid)
    except (KeyError, OSError) as exc:
        raise SupervisorUnavailableError("Local Supervisor state is unavailable") from exc
    finally:
        with suppress(OSError):
            os.close(key_descriptor)
        with suppress(OSError):
            os.close(directory_descriptor)
    _parse_public_key(raw)
    raise SupervisorEstablishedError("Local Supervisor is already established")


def _decode_segment(encoded: str) -> bytes:
    try:
        return base64url.decode(encoded)
    except ValueError as exc:
        raise SupervisorDeniedError("Local Supervisor assertion is malformed") from exc


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
    if not hmac.compare_digest(base64url.encode(canonical), encoded):
        raise SupervisorDeniedError("Local Supervisor assertion is not canonical")
    return value


def _verified_claims(
    encoded: str,
    key: Ed25519PublicKey,
    *,
    jwt_header: dict[str, str] = contract.JWT_HEADER,
    audience: str = contract.ASSERTION_AUDIENCE,
) -> dict[str, object]:
    parts = encoded.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise SupervisorDeniedError("Local Supervisor assertion is malformed")
    header = _json_segment(parts[0])
    untrusted_claims = _json_segment(parts[1])
    if header != jwt_header:
        raise SupervisorDeniedError("Local Supervisor assertion header is invalid")
    try:
        claims = contract.canonical_claims(untrusted_claims, audience=audience)
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
    request: RequestBinding,
    replay_guard: ReplayGuard | None = None,
    now: int | None = None,
) -> Evidence:
    """Verify and atomically consume evidence for one exact request under Team's pinned Supervisor key."""
    encoded = _one_assertion(headers)
    key = supervisor_key()
    claims = _verified_claims(encoded, key)
    assertion_id, expires_at = _bound(claims, request, replay_guard, now)
    return Evidence(
        supervisor_id=str(claims["sub"]),
        authority_kind=str(claims["authority"]),
        authority_digest=str(claims["authority_sha256"]),
        assertion_id=assertion_id,
        expires_at=expires_at,
        key_sha256=key_sha256(key),
    )


def routine_key_fingerprint() -> str:
    """The SHA-256 of the current Routine public key; a lease records it, so a new key fences every older lease."""
    return key_sha256(_public_key(ROUTINE_PUBLIC_KEY_FILE))


def verify_routine(
    headers: Message,
    *,
    request: RequestBinding,
    replay_guard: ReplayGuard | None = None,
    now: int | None = None,
) -> RoutineEvidence:
    """Verify and consume a Routine assertion for one exact request; the caller then checks its run lease."""
    key = _public_key(ROUTINE_PUBLIC_KEY_FILE)
    claims = _verified_claims(
        _one_assertion(headers, contract.ROUTINE_ASSERTION_HEADER),
        key,
        jwt_header=contract.ROUTINE_JWT_HEADER,
        audience=contract.ROUTINE_AUDIENCE,
    )
    routine_request = RequestBinding(
        request.method,
        request.path,
        request.body,
        request.model,
        None,
        frozenset({contract.ROUTINE_AUTHORITY}),
    )
    assertion_id, expires_at = _bound(claims, routine_request, replay_guard, now)
    return RoutineEvidence(
        key_fingerprint=key_sha256(key),
        lease_sha256=str(claims["authority_sha256"]),
        assertion_id=assertion_id,
        expires_at=expires_at,
    )


def _bound(
    claims: dict[str, object], request: RequestBinding, replay_guard: ReplayGuard | None, now: int | None
) -> tuple[str, int]:
    """Check the assertion's time and exact request binding, then consume its one-use nonce."""
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
        "method": request.method,
        "path": request.path,
        "body": request.body,
    }
    actual = {field: claims[field] for field in expected}
    if (
        actual != expected
        or claims.get("model") != request.model
        or claims.get("decision") != request.decision
        or claims.get("assurance") != request.assurance
        or claims.get("authority") not in request.authority_kinds
    ):
        raise SupervisorDeniedError("Local Supervisor assertion does not match the request")
    guard = _REPLAY_GUARD if replay_guard is None else replay_guard
    assertion_id = claims["jti"]
    if not isinstance(assertion_id, str):
        raise SupervisorDeniedError("Local Supervisor assertion is invalid")
    guard.consume(assertion_id, expires_at, now=current)
    return assertion_id, expires_at
