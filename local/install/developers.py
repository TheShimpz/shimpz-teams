"""Bounded public Developers client for a locally owned Space."""

from __future__ import annotations

import http.client
import json
import re
import ssl
from dataclasses import dataclass
from typing import Any

from install.contract import ContractValidationError, ContractValidator

_HOST = "developers.shimpz.com"
_PORT = 443
_RELEASE_PROXY_HOST = "shimpz-assistant-release"
_RELEASE_PROXY_PORT = 8888
_TIMEOUT_SECONDS = 10
_MAX_RESPONSE_BYTES = 1024 * 1024
_SOURCE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ASSISTANT_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_SEMANTIC_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_TLS_CONTEXT = ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")
_CONTRACTS = ContractValidator()


class DevelopersError(RuntimeError):
    """Developers could not provide a trustworthy current publication."""


class PublicationNotInstallableError(DevelopersError):
    """The exact publication is not currently installable."""


@dataclass(frozen=True, slots=True)
class CatalogPublication:
    assistant_id: str
    assistant_version: str
    source_digest: str


class DevelopersClient:
    def catalog(self) -> tuple[CatalogPublication, ...]:
        status, raw = self._request("/api/v1/assistants")
        if status != 200:
            raise DevelopersError("Developers catalog is unavailable")
        try:
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DevelopersError("Developers catalog violates its contract") from exc
        if not isinstance(value, dict) or set(value) != {"version", "assistants"} or value["version"] != 1:
            raise DevelopersError("Developers catalog violates its contract")
        assistants = value["assistants"]
        if not isinstance(assistants, list) or len(assistants) > 1000:
            raise DevelopersError("Developers catalog violates its contract")
        publications = tuple(_catalog_publication(item) for item in assistants)
        identities = tuple(publication.assistant_id for publication in publications)
        if len(identities) != len(set(identities)):
            raise DevelopersError("Developers catalog violates its contract")
        return tuple(sorted(publications, key=lambda publication: publication.assistant_id))

    def resolve(self, source_digest: str) -> dict[str, Any]:
        if _SOURCE_DIGEST.fullmatch(source_digest) is None:
            raise PublicationNotInstallableError("publication digest is invalid")
        status, raw = self._request(f"/api/v1/assistant-publications/{source_digest}")
        value = _resolution(status, raw)
        if value["source_digest"] != source_digest:
            raise DevelopersError("Developers response does not match the requested digest")
        return value

    def latest(self, source_digest: str) -> dict[str, Any]:
        """Resolve the newest visibility-bounded candidate for one installed digest."""
        if _SOURCE_DIGEST.fullmatch(source_digest) is None:
            raise PublicationNotInstallableError("publication digest is invalid")
        status, raw = self._request(f"/api/v1/assistant-publications/{source_digest}/latest")
        return _resolution(status, raw)

    @staticmethod
    def _request(path: str) -> tuple[int, bytes]:
        connection = http.client.HTTPSConnection(
            _RELEASE_PROXY_HOST,
            _RELEASE_PROXY_PORT,
            timeout=_TIMEOUT_SECONDS,
            context=_TLS_CONTEXT,
        )
        connection.set_tunnel(_HOST, _PORT)
        try:
            connection.request(
                "GET",
                path,
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
        return response.status, raw


def _resolution(status: int, raw: bytes) -> dict[str, Any]:
    if status == 404:
        raise PublicationNotInstallableError("publication is not installable")
    if status != 200:
        raise DevelopersError("Developers resolution is unavailable")
    try:
        value = json.loads(raw)
        _CONTRACTS.validate("resolve-response.schema.json", value)
    except (UnicodeError, json.JSONDecodeError, ContractValidationError) as exc:
        raise DevelopersError("Developers response violates its contract") from exc
    if not isinstance(value, dict):
        raise DevelopersError("Developers response violates its contract")
    return value


def _catalog_publication(value: object) -> CatalogPublication:
    if not isinstance(value, dict):
        raise DevelopersError("Developers catalog violates its contract")
    assistant_id = value.get("assistant_id")
    assistant_version = value.get("assistant_version")
    source_digest = value.get("source_digest")
    if (
        not isinstance(assistant_id, str)
        or len(assistant_id) > 40
        or _ASSISTANT_ID.fullmatch(assistant_id) is None
        or not isinstance(assistant_version, str)
        or _SEMANTIC_VERSION.fullmatch(assistant_version) is None
        or not isinstance(source_digest, str)
        or _SOURCE_DIGEST.fullmatch(source_digest) is None
    ):
        raise DevelopersError("Developers catalog violates its contract")
    return CatalogPublication(assistant_id, assistant_version, source_digest)
