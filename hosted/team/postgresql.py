"""Tenant-scoped postgresql-service client: one persistent principal token and provisioning fence per Team."""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from protocol.http.v1 import payload as http_payload
from storage import private_state

POSTGRESQL_SERVICE_URL = os.environ.get("SHIMPZ_POSTGRESQL_SERVICE_URL", "http://postgresql-service:7072")
PROVISIONER_TOKEN_FILE = Path(
    os.environ.get(
        "SHIMPZ_POSTGRESQL_SERVICE_PROVISIONER_TOKEN_FILE",
        "/run/shimpz-postgresql-service/token",
    )
)
PRINCIPAL_DIR = Path(
    os.environ.get(
        "SHIMPZ_POSTGRESQL_PRINCIPAL_DIR",
        "/var/lib/team/postgresql-principals",
    )
)
SERVICE_TIMEOUT_SECONDS = 30
_FENCE = re.compile(rb"[0-9]{1,16}")


class PostgreSQLServiceError(Exception):
    """postgresql-service refused or was unreachable; lifecycle rollback must surface this."""


def _call(path: str, payload: dict, bearer: str) -> dict:
    parsed = urlparse(POSTGRESQL_SERVICE_URL)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 7072, timeout=SERVICE_TIMEOUT_SECONDS)
    try:
        conn.request(
            "POST",
            path,
            json.dumps(payload),
            {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            # The upstream body is intentionally not reflected into Team create errors. Even a
            # regressed/misconfigured postgresql-service must not smuggle SQL or a role password through it.
            raise PostgreSQLServiceError(f"postgresql-service {path} failed with status {resp.status}")
        result = json.loads(raw or b"{}")
        if not isinstance(result, dict):
            raise PostgreSQLServiceError(f"postgresql-service {path} returned a non-object response")
        return result
    finally:
        conn.close()


def _principal_path(team_id: str) -> Path:
    if not http_payload.TEAM_ID_RE.fullmatch(team_id):
        raise PostgreSQLServiceError("invalid team id for principal path")
    return PRINCIPAL_DIR / f"{team_id}.token"


def _principal(team_id: str, *, create: bool) -> str:
    path = _principal_path(team_id)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[a-f0-9]{64}", token):
            path.chmod(0o600)
            return token
        raise PostgreSQLServiceError("stored Team database principal is malformed")
    if not create:
        raise PostgreSQLServiceError("Team database principal is missing")
    PRINCIPAL_DIR.mkdir(parents=True, exist_ok=True)
    PRINCIPAL_DIR.chmod(0o700)
    token = secrets.token_hex(32)
    # This is the Team's only cleartext copy of a bearer the Service may durably bind to owned resources, so it is
    # committed whole (never partially) before the Service is called and every later cleanup can present it.
    try:
        private_state.replace_durably(path, token.encode("ascii"))
    except OSError as exc:
        raise PostgreSQLServiceError("Team database principal could not be persisted") from exc
    return token


def _fence_path(team_id: str) -> Path:
    return _principal_path(team_id).with_suffix(".fence")


def _fence(team_id: str) -> int:
    """The latest `not_after` of any provisioning request that may still reach the Service; 0 when none can."""
    try:
        raw = _fence_path(team_id).read_bytes()
    except FileNotFoundError:
        return 0
    if _FENCE.fullmatch(raw) is None:
        raise PostgreSQLServiceError("stored Team provisioning fence is malformed")
    return int(raw)


def _store_fence(team_id: str, not_after: int) -> None:
    try:
        private_state.replace_durably(_fence_path(team_id), str(not_after).encode("ascii"))
    except OSError as exc:
        raise PostgreSQLServiceError("Team provisioning fence could not be persisted") from exc


def _provisioner() -> str:
    return PROVISIONER_TOKEN_FILE.read_text(encoding="utf-8").strip()


def provision_team(team_id: str) -> dict:
    principal = _principal(team_id, create=True)
    settled = _fence(team_id)
    # The Service refuses this request once `not_after` has passed, so until it is answered the durable fence is the
    # earliest moment at which a proven absence of this Team becomes terminal. It is committed before sending.
    not_after = int(time.time()) + SERVICE_TIMEOUT_SECONDS
    _store_fence(team_id, max(settled, not_after))
    try:
        result = _call(
            "/v1/teams/provision",
            {"team_id": team_id, "principal_token": principal, "not_after": not_after},
            _provisioner(),
        )
    except PostgreSQLServiceError:
        # Any HTTP answer settles the request: the Service completed it under its mutation lock before replying.
        _store_fence(team_id, settled)
        raise
    _store_fence(team_id, settled)
    return result


def _passed_fence(team_id: str) -> int:
    """Wait (boundedly) until this Team's provisioning fence has passed, then return it for the Service to verify.

    The Service proves absence and finalizes only after the fence on its own clock; waiting avoids a needless refusal,
    and the bound keeps a refusal retryable instead of blocking teardown.
    """
    not_after = _fence(team_id)
    time.sleep(min(max(0.0, not_after + 1 - time.time()), SERVICE_TIMEOUT_SECONDS + 1))
    return not_after


def _confirm_absent(team_id: str) -> dict:
    """Ask the Service to prove that nothing of this Team exists or can still be provisioned; it never drops."""
    return _call("/v1/teams/drop", {"team_id": team_id, "not_after": _passed_fence(team_id)}, _provisioner())


def drop_team(team_id: str) -> dict:
    # The tenant endpoint retires (rather than deletes) its hashed principal, making an ambiguous
    # response safely retryable until Team runtime/volume cleanup is durably complete.
    try:
        return _call(
            "/v1/teams/drop",
            {"team_id": team_id},
            _principal(team_id, create=False),
        )
    except PostgreSQLServiceError:
        # Provisioning may have failed before the Service recorded this principal, so no principal can authorize a
        # drop. The provisioner's drop succeeds only when the Service proves, without DDL, that it holds no record,
        # database, or role for this Team after every request Team may still have in flight is fenced out.
        return _confirm_absent(team_id)


def finalize_team_drop(team_id: str) -> dict:
    """Finalize the retired PostgreSQL principal, then remove Team's cleartext copy and fence; retry-safe."""
    result = _call(
        "/v1/teams/finalize",
        {"team_id": team_id, "not_after": _passed_fence(team_id)},
        _provisioner(),
    )
    _principal_path(team_id).unlink(missing_ok=True)
    _fence_path(team_id).unlink(missing_ok=True)
    return result
