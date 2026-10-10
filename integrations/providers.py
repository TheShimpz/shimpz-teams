"""Core-owned OAuth provider metadata for reviewed Assistant integrations.

Assistant packages may name a provider and request reviewed scopes. They cannot
choose the provider, its scopes, or its PKCE method. Adding or changing a provider
therefore requires a controller release.
"""

import re
from dataclasses import dataclass
from types import MappingProxyType

from protocol.http.v1 import payload as http_payload

MAX_REQUESTED_SCOPES = 32
_SCOPE = re.compile(r"[a-z][a-z0-9]*(?:[._:-][a-z0-9]+)*\Z")


class OAuthProviderError(RuntimeError):
    """A integration referenced an unknown provider or disallowed OAuth intent."""


@dataclass(frozen=True, slots=True)
class ProviderRoute:
    """One reviewed provider endpoint: an exact method and a pattern its canonical path must match whole."""

    method: str
    path: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    id: str
    # The reviewed API hosts Team sends this provider's bearer to in an Assistant's provider calls (ADR-0106).
    api_hosts: tuple[str, ...]
    allowed_scopes: frozenset[str]
    pkce_method: str
    # The only endpoints Team sends this provider's bearer to on its API hosts (ADR-0106 amendment, 2026-10-09).
    routes: tuple[ProviderRoute, ...] = ()

    def allows(self, method: str, path: str) -> bool:
        """Whether one call's method and path are a reviewed endpoint; a non-canonical path never matches.

        The path is matched as sent, before any query: percent-encoding, dot segments, empty segments, and a trailing
        slash never match a reviewed pattern, so Team refuses rather than normalizes them.
        """
        return any(route.method == method and route.path.fullmatch(path) is not None for route in self.routes)


@dataclass(frozen=True, slots=True)
class OAuthIntegrationIntent:
    provider: OAuthProvider
    scopes: tuple[str, ...]


_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})


def _provider(
    *,
    provider_id: str,
    api_hosts: tuple[str, ...],
    allowed_scopes: frozenset[str],
    routes: tuple[tuple[str, str], ...],
) -> OAuthProvider:
    provider = OAuthProvider(
        id=provider_id,
        api_hosts=api_hosts,
        allowed_scopes=allowed_scopes,
        pkce_method="S256",
        routes=tuple(ProviderRoute(method, re.compile(path)) for method, path in routes),
    )
    if (
        http_payload.canonical_identifier(provider.id) is None
        or not provider.api_hosts
        or not provider.allowed_scopes
        or not provider.routes
        or any(route.method not in _METHODS or not route.path.pattern.startswith("/") for route in provider.routes)
    ):
        raise RuntimeError("trusted OAuth provider registry is invalid")
    if any(_SCOPE.fullmatch(scope) is None for scope in provider.allowed_scopes):
        raise RuntimeError("trusted OAuth provider registry is invalid")
    return provider


# A Cloudflare zone or DNS-record identifier: exactly 32 lowercase hexadecimal characters.
_CLOUDFLARE_ID = "[0-9a-f]{32}"
_ZONE = "/client/v4/zones/" + _CLOUDFLARE_ID
_RECORD = _ZONE + "/dns_records/" + _CLOUDFLARE_ID

# The platform broker holds the Cloudflare client and its secret; the controller binds each grant with PKCE S256.
# The closed set admits zone discovery plus reconciliable DNS-record reads and writes; no zone or account write scope
# is admitted. Its routes are exactly the calls the reviewed Cloudflare Assistant makes: zone reads, DNS-record reads,
# and DNS-record creation, replacement, and deletion. No account, user, token, or zone-write endpoint is reachable
# with the bearer, so a workload can never use it to mint or inspect credentials.
_CLOUDFLARE = _provider(
    provider_id="cloudflare",
    api_hosts=("api.cloudflare.com",),
    allowed_scopes=frozenset({"dns.read", "dns.write", "offline_access", "zone.read"}),
    routes=(
        ("GET", "/client/v4/zones"),
        ("GET", _ZONE),
        ("GET", _ZONE + "/dns_records"),
        ("GET", _RECORD),
        ("POST", _ZONE + "/dns_records"),
        ("PUT", _RECORD),
        ("DELETE", _RECORD),
    ),
)

PROVIDERS = MappingProxyType({_CLOUDFLARE.id: _CLOUDFLARE})


def resolve(provider_id: object) -> OAuthProvider:
    """Resolve only a controller-reviewed provider identifier."""
    if http_payload.canonical_identifier(provider_id) is None:
        raise OAuthProviderError("OAuth provider is unavailable")
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise OAuthProviderError("OAuth provider is unavailable")
    return provider


def integration_intent(provider_id: object, requested_scopes: object) -> OAuthIntegrationIntent:
    """Return one deterministic least-privilege scope set for a trusted provider."""
    provider = resolve(provider_id)
    if not isinstance(requested_scopes, list | tuple) or not 1 <= len(requested_scopes) <= MAX_REQUESTED_SCOPES:
        raise OAuthProviderError("OAuth scopes are invalid")
    scopes: list[str] = []
    for scope in requested_scopes:
        if not isinstance(scope, str) or _SCOPE.fullmatch(scope) is None:
            raise OAuthProviderError("OAuth scopes are invalid")
        scopes.append(scope)
    if len(scopes) != len(set(scopes)) or not set(scopes) <= provider.allowed_scopes:
        raise OAuthProviderError("OAuth scopes are invalid")
    return OAuthIntegrationIntent(provider, tuple(sorted(scopes)))
