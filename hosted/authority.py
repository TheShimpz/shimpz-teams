"""Request-bound Account authority consumer for Hosted Team."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import re
import stat
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from jsonschema import Draft202012Validator

ACCOUNT_URL = os.environ.get("SHIMPZ_ACCOUNT_URL", "http://account:7079")
CAPABILITY_FILE = Path(
    os.environ.get(
        "SHIMPZ_ACCOUNT_TEAM_AUTHORITY_TOKEN_FILE",
        "/run/shimpz-account-team-authority/token",
    )
)
EVALUATE_PATH = "/v1/internal/authority/evaluate"
EVALUATION_TIMEOUT_SECONDS = 5
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 8 * 1024
MAX_BINDING_BYTES = 8 * 1024
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_ACCOUNT_ID = re.compile(r"[0-9a-f]{32}\Z")
_CAPABILITY = re.compile(rb"[0-9a-f]{64}\Z")
_PROTOCOL = Path(__file__).resolve().parents[1] / "protocol" / "account" / "authority" / "v1"
_REQUEST_VALIDATOR = Draft202012Validator(json.loads((_PROTOCOL / "evaluation-request.schema.json").read_bytes()))
_RESPONSE_VALIDATOR = Draft202012Validator(json.loads((_PROTOCOL / "evaluation-response.schema.json").read_bytes()))


class AuthorityDeniedError(RuntimeError):
    """Account denied the human authority request."""


class AuthorityUnavailableError(RuntimeError):
    """Account authority could not be evaluated exactly and synchronously."""


@dataclass(frozen=True, slots=True)
class Evaluation:
    account_id: str
    supervisor: bool
    binding_digest: str
    owner_account_id: str | None
    assurance: dict[str, str] | None = None

    @property
    def principal(self) -> tuple[str, str]:
        return ("supervisor" if self.supervisor else "account", self.account_id)


def binding_digest(binding: object) -> str:
    """Validate and digest one exact request binding independently of Account."""
    request = {"version": 1, "session_token": "x", "binding": binding}
    if isinstance(binding, dict) and "assurance" in binding:
        request["assurance_handle"] = "A" * 43
    errors = tuple(_REQUEST_VALIDATOR.iter_errors(request))
    if errors:
        raise AuthorityUnavailableError("Account authority binding is invalid")
    try:
        encoded = json.dumps(
            binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AuthorityUnavailableError("Account authority binding is invalid") from exc
    if len(encoded) > MAX_BINDING_BYTES:
        raise AuthorityUnavailableError("Account authority binding is invalid")
    return hashlib.sha256(encoded).hexdigest()


def _endpoint() -> tuple[str, int]:
    try:
        parsed = urlparse(ACCOUNT_URL)
        port = parsed.port or 7079
    except ValueError as exc:
        raise AuthorityUnavailableError("Account authority endpoint is unavailable") from exc
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65535
    ):
        raise AuthorityUnavailableError("Account authority endpoint is unavailable")
    return parsed.hostname, port


def _capability() -> str:
    descriptor: int | None = None
    try:
        descriptor = os.open(CAPABILITY_FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o440:
            raise OSError("invalid capability metadata")
        raw = os.read(descriptor, 65)
    except (OSError, ValueError) as exc:
        raise AuthorityUnavailableError("Account authority capability is unavailable") from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
    if _CAPABILITY.fullmatch(raw) is None:
        raise AuthorityUnavailableError("Account authority capability is unavailable")
    return raw.decode("ascii")


def session_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 2048
        or not value.isascii()
        or any(not 0x20 <= ord(character) <= 0x7E for character in value)
    ):
        raise AuthorityDeniedError("Account session is invalid")
    return value


def _payload(
    session_token: str,
    binding: dict[str, object],
    assurance_handle: object | None,
) -> bytes:
    request = {"version": 1, "session_token": session_token, "binding": binding}
    if assurance_handle is not None:
        request["assurance_handle"] = assurance_handle
    if tuple(_REQUEST_VALIDATOR.iter_errors(request)):
        raise AuthorityUnavailableError("Account authority request is invalid")
    try:
        encoded = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AuthorityUnavailableError("Account authority request is invalid") from exc
    if len(encoded) > MAX_REQUEST_BYTES:
        raise AuthorityUnavailableError("Account authority request is invalid")
    return encoded


def _response_body(response: http.client.HTTPResponse) -> dict[str, object]:
    content_type = (response.getheader("Content-Type") or "").partition(";")[0].strip().lower()
    raw_length = response.getheader("Content-Length")
    if (
        content_type != "application/json"
        or raw_length is None
        or not raw_length.isascii()
        or not raw_length.isdecimal()
        or not 1 <= int(raw_length) <= MAX_RESPONSE_BYTES
    ):
        raise AuthorityUnavailableError("Account authority response is invalid")
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) != int(raw_length):
        raise AuthorityUnavailableError("Account authority response is invalid")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise AuthorityUnavailableError("Account authority response is invalid") from exc
    if not isinstance(body, dict):
        raise AuthorityUnavailableError("Account authority response is invalid")
    return body


def _evaluation(response: dict[str, object], binding: dict[str, object], expected_digest: str) -> Evaluation:
    if tuple(_RESPONSE_VALIDATOR.iter_errors(response)):
        raise AuthorityUnavailableError("Account authority response is invalid")
    response_digest = response["binding_digest"]
    if not isinstance(response_digest, str) or not hmac.compare_digest(response_digest, expected_digest):
        raise AuthorityUnavailableError("Account authority response binding is invalid")
    account_id = response["account_id"]
    supervisor = response["supervisor"]
    if not isinstance(account_id, str) or _ACCOUNT_ID.fullmatch(account_id) is None or type(supervisor) is not bool:
        raise AuthorityUnavailableError("Account authority response is invalid")
    owner = response.get("owner_account_id")
    if binding["operation"] == "team-create":
        requested_owner = binding.get("owner_account_id")
        expected_owner = requested_owner if requested_owner is not None else account_id
        if requested_owner is not None and requested_owner != account_id and not supervisor:
            raise AuthorityUnavailableError("Account authority Supervisor evidence is invalid")
        if owner != expected_owner:
            raise AuthorityUnavailableError("Account authority Owner evidence is invalid")
    elif owner is not None:
        raise AuthorityUnavailableError("Account authority Owner evidence is invalid")
    assurance = response.get("assurance")
    if assurance != binding.get("assurance"):
        raise AuthorityUnavailableError("Account authority assurance evidence is invalid")
    return Evaluation(
        account_id,
        supervisor,
        response_digest,
        owner if isinstance(owner, str) else None,
        assurance if isinstance(assurance, dict) else None,
    )


def evaluate(
    presented_session: object,
    binding: dict[str, object],
    assurance_handle: object | None = None,
) -> Evaluation:
    """Evaluate one exact request without caching revocable Account evidence."""
    token = session_token(presented_session)
    digest = binding_digest(binding)
    body = _payload(token, binding, assurance_handle)
    connection = None
    started = time.monotonic()
    try:
        host, port = _endpoint()
        connection = http.client.HTTPConnection(host, port, timeout=EVALUATION_TIMEOUT_SECONDS)
        connection.request(
            "POST",
            EVALUATE_PATH,
            body=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {_capability()}",
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        response_body = _response_body(response)
    except AuthorityDeniedError, AuthorityUnavailableError:
        raise
    except (OSError, UnicodeError, http.client.HTTPException) as exc:
        raise AuthorityUnavailableError("Account authority is unavailable") from exc
    finally:
        if connection is not None:
            with suppress(OSError):
                connection.close()
    if time.monotonic() - started > EVALUATION_TIMEOUT_SECONDS:
        raise AuthorityUnavailableError("Account authority evidence arrived too late")
    if response.status != 200:
        if response.status in {401, 403, 404}:
            raise AuthorityDeniedError("Account authority was denied")
        raise AuthorityUnavailableError("Account authority is unavailable")
    return _evaluation(response_body, binding, digest)
