"""Arrange-only fixtures for the encrypted OAuth Integration store: the demo Cloudflare grant of one Assistant."""

from collections.abc import Callable
from pathlib import Path

from integrations import store as integration_store
from integrations.http import OAuthTokenSet

ACCESS = "access-token-private-material-123456789"
REFRESH = "refresh-token-private-material-987654321"
SCOPES = ("dns.read", "offline_access", "zone.read")
DECLARATIONS = {"cloudflare": {"provider": "cloudflare", "scopes": SCOPES}}
ACCOUNT = {"id": "2244994945", "username": "Cloudflare", "name": "Cloudflare"}
ASSISTANT = "shimpz-cloudflare"


def tokens(
    *,
    access: str = ACCESS,
    refresh: str | None = REFRESH,
    scopes: tuple[str, ...] = SCOPES,
    expires_in: int = 3600,
    broker_lease: str | None = None,
) -> OAuthTokenSet:
    return OAuthTokenSet(access, refresh, scopes, expires_in, broker_lease)


def open_store(
    root: Path, *, clock: Callable[[], int] = lambda: 1_000_000_000
) -> integration_store.OAuthIntegrationStore:
    return integration_store.OAuthIntegrationStore(
        root / "state" / "integrations.json", root / "key" / "aes256.key", clock=clock
    )


def put(
    store: integration_store.OAuthIntegrationStore,
    *,
    team: str = "team_1",
    integration: str = "cloudflare",
    account: dict[str, str] | None = ACCOUNT,
    **token_changes: object,
) -> integration_store.OAuthIntegrationMetadata:
    """Store the demo Assistant's Cloudflare grant over SCOPES, with these token changes."""
    return store.put(team, ASSISTANT, integration, "cloudflare", SCOPES, tokens(**token_changes), account)


def resolve(
    store: integration_store.OAuthIntegrationStore,
    refresh: Callable[..., object],
    *,
    integration: str = "cloudflare",
) -> str:
    """Resolve the demo Assistant's Cloudflare access token for Team team_1 over SCOPES."""
    return store.resolve("team_1", ASSISTANT, integration, "cloudflare", SCOPES, refresh)
