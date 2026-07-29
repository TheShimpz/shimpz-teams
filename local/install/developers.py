"""Bounded public Developers client for a locally owned Space."""

from __future__ import annotations

import http.client
import json
import re
from typing import Any

from install.contract import ContractValidationError, ContractValidator

_HOST = "developers.shimpz.com"
_PORT = 443
_TIMEOUT_SECONDS = 10
_MAX_RESPONSE_BYTES = 1024 * 1024
_SOURCE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTRACTS = ContractValidator()


class DevelopersError(RuntimeError):
    """Developers could not provide a trustworthy current publication."""


class PublicationNotInstallableError(DevelopersError):
    """The exact publication is not currently installable."""


class DevelopersClient:
    def resolve(self, source_digest: str) -> dict[str, Any]:
        if _SOURCE_DIGEST.fullmatch(source_digest) is None:
            raise PublicationNotInstallableError("publication digest is invalid")
        connection = http.client.HTTPSConnection(_HOST, _PORT, timeout=_TIMEOUT_SECONDS)
        try:
            connection.request(
                "GET",
                f"/api/v1/assistant-publications/{source_digest}",
                headers={"Accept": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise DevelopersError("Developers is unavailable") from exc
        finally:
            connection.close()
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise DevelopersError("Developers response is too large")
        if response.status == 404:
            raise PublicationNotInstallableError("publication is not installable")
        if response.status != 200:
            raise DevelopersError("Developers resolution is unavailable")
        try:
            value = json.loads(raw)
            _CONTRACTS.validate("resolve-response.schema.json", value)
        except (UnicodeError, json.JSONDecodeError, ContractValidationError) as exc:
            raise DevelopersError("Developers response violates its contract") from exc
        if not isinstance(value, dict) or value.get("source_digest") != source_digest:
            raise DevelopersError("Developers response does not match the requested digest")
        return value
