"""Pure Local Supervisor assertion contract shared by Team and Admin."""

from __future__ import annotations

import hashlib
import json
import re

ASSERTION_HEADER = "X-Shimpz-Supervisor"
ASSERTION_AUDIENCE = "team-local"
ASSERTION_KEY_ID = "local-supervisor-v1"
ASSERTION_MAX_TTL_SECONDS = 15
ASSERTION_CLOCK_SKEW_SECONDS = 5
ASSERTION_MAX_BYTES = 4096
JWT_HEADER = {"alg": "EdDSA", "kid": ASSERTION_KEY_ID, "typ": "JWT"}
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_PATH = re.compile(r"^/[a-z0-9_/-]+$")
_PROVIDER = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+\-]*/[a-z0-9][a-z0-9!#$&^_.+\-]*$")
_METHODS = frozenset({"DELETE", "GET", "POST", "PUT"})


class SupervisorAssertionError(ValueError):
    """A Local Supervisor assertion does not match the closed wire contract."""


def _integer(value: object, *, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 9_999_999_999:
        raise SupervisorAssertionError(f"invalid {label}")
    return value


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _HEX_64.fullmatch(value) is None:
        raise SupervisorAssertionError(f"invalid {label}")
    return value


def _body(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SupervisorAssertionError("invalid assertion body")
    kind = value.get("kind")
    if kind == "none":
        if set(value) != {"kind", "length", "sha256"} or value["length"] != 0:
            raise SupervisorAssertionError("invalid empty assertion body")
        digest = _digest(value["sha256"], label="empty body digest")
        if digest != EMPTY_SHA256:
            raise SupervisorAssertionError("invalid empty body digest")
        return {
            "kind": "none",
            "length": 0,
            "sha256": digest,
        }
    if kind == "json":
        length = value.get("length")
        if set(value) != {"kind", "length", "sha256"} or type(length) is not int or not 2 <= length <= 24 * 1024:
            raise SupervisorAssertionError("invalid JSON assertion body")
        return {
            "kind": "json",
            "length": length,
            "sha256": _digest(value["sha256"], label="JSON body digest"),
        }
    if kind == "file":
        length = value.get("length")
        filename = value.get("filename")
        media_type = value.get("media_type")
        try:
            encoded_filename = filename.encode("utf-8") if isinstance(filename, str) else b""
        except UnicodeError as exc:
            raise SupervisorAssertionError("invalid file assertion body") from exc
        if (
            set(value) != {"kind", "length", "filename", "media_type"}
            or type(length) is not int
            or not 1 <= length <= 25 * 1024 * 1024
            or not isinstance(filename, str)
            or not filename
            or filename != filename.strip()
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or len(encoded_filename) > 255
            or not isinstance(media_type, str)
            or not 1 <= len(media_type) <= 127
            or _MEDIA_TYPE.fullmatch(media_type) is None
        ):
            raise SupervisorAssertionError("invalid file assertion body")
        return {
            "kind": "file",
            "length": length,
            "filename": filename,
            "media_type": media_type,
        }
    raise SupervisorAssertionError("invalid assertion body")


def _model(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"provider", "key_sha256"}:
        raise SupervisorAssertionError("invalid model credential binding")
    provider = value["provider"]
    if not isinstance(provider, str) or _PROVIDER.fullmatch(provider) is None:
        raise SupervisorAssertionError("invalid model provider binding")
    return {
        "provider": provider,
        "key_sha256": _digest(value["key_sha256"], label="model credential digest"),
    }


def canonical_claims(value: object) -> dict[str, object]:
    """Return one closed canonical assertion claim set."""
    if not isinstance(value, dict):
        raise SupervisorAssertionError("invalid Supervisor assertion claims")
    required = {
        "v",
        "aud",
        "sub",
        "session_sha256",
        "jti",
        "iat",
        "exp",
        "method",
        "path",
        "body",
    }
    if not required <= set(value) or set(value) - required - {"model"}:
        raise SupervisorAssertionError("invalid Supervisor assertion claims")
    if type(value["v"]) is not int or value["v"] != 1 or value["aud"] != ASSERTION_AUDIENCE:
        raise SupervisorAssertionError("unsupported Supervisor assertion")
    subject = value["sub"]
    nonce = value["jti"]
    if not isinstance(subject, str) or _HEX_32.fullmatch(subject) is None:
        raise SupervisorAssertionError("invalid Supervisor subject")
    if not isinstance(nonce, str) or _HEX_32.fullmatch(nonce) is None:
        raise SupervisorAssertionError("invalid Supervisor assertion nonce")
    issued_at = _integer(value["iat"], label="assertion issued time")
    expires_at = _integer(value["exp"], label="assertion expiry")
    if not 1 <= expires_at - issued_at <= ASSERTION_MAX_TTL_SECONDS:
        raise SupervisorAssertionError("invalid Supervisor assertion lifetime")
    method = value["method"]
    path = value["path"]
    if not isinstance(method, str) or method not in _METHODS:
        raise SupervisorAssertionError("invalid assertion method")
    if (
        not isinstance(path, str)
        or len(path.encode("ascii", "ignore")) != len(path)
        or len(path) > 512
        or _PATH.fullmatch(path) is None
        or "//" in path
        or path.endswith("/")
    ):
        raise SupervisorAssertionError("invalid assertion path")
    result: dict[str, object] = {
        "v": 1,
        "aud": ASSERTION_AUDIENCE,
        "sub": subject,
        "session_sha256": _digest(value["session_sha256"], label="session digest"),
        "jti": nonce,
        "iat": issued_at,
        "exp": expires_at,
        "method": method,
        "path": path,
        "body": _body(value["body"]),
    }
    if "model" in value:
        result["model"] = _model(value["model"])
    return result


def canonical_json(value: object) -> bytes:
    """Encode one value with the assertion contract's deterministic representation."""
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SupervisorAssertionError("assertion JSON is invalid") from exc
    if len(encoded) > ASSERTION_MAX_BYTES:
        raise SupervisorAssertionError("assertion JSON exceeds its limit")
    return encoded


def claims_json(value: object) -> bytes:
    """Validate and encode one canonical claim set."""
    return canonical_json(canonical_claims(value))
