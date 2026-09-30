"""Tenant-scoped postgresql-service client: one persistent principal token per Team."""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

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
SAFE_TEAM_ID = re.compile(r"^[a-z0-9_]{1,40}$")


class PostgreSQLServiceError(Exception):
    """postgresql-service refused or was unreachable; lifecycle rollback must surface this."""


def _call(path: str, payload: dict, bearer: str) -> dict:
    parsed = urlparse(POSTGRESQL_SERVICE_URL)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 7072, timeout=30)
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
    if not SAFE_TEAM_ID.fullmatch(team_id):
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


def _provisioner() -> str:
    return PROVISIONER_TOKEN_FILE.read_text(encoding="utf-8").strip()


def provision_team(team_id: str) -> dict:
    principal = _principal(team_id, create=True)
    return _call(
        "/v1/teams/provision",
        {"team_id": team_id, "principal_token": principal},
        _provisioner(),
    )


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
        # drop. The provisioner's drop never drops: it succeeds only when the Service proves, without DDL, that it
        # holds no record, database, or role for this Team; anything the Service still owns keeps this failing.
        return _call("/v1/teams/drop", {"team_id": team_id}, _provisioner())


def finalize_team_drop(team_id: str) -> dict:
    """Finalize the retired PostgreSQL principal, then remove Team's cleartext copy; retry-safe."""
    result = _call(
        "/v1/teams/finalize",
        {"team_id": team_id},
        _provisioner(),
    )
    _principal_path(team_id).unlink(missing_ok=True)
    return result
