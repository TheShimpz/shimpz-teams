"""Narrow controller-owned orchestration for Assistant OAuth integrations.

This module composes the one-use PKCE challenge store, the fixed-endpoint OAuth
HTTP adapter, and the encrypted token store.  It deliberately owns no routes,
cookies, browser state, Assistant runtime calls, or Brain-visible data.
"""

from __future__ import annotations

import functools
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from assistant_human import (
    assistant_integration_challenges,
    oauth_broker_client,
    oauth_http_client,
    oauth_integration_store,
    oauth_pkce_challenges,
    oauth_providers,
)

_CLIENT_ID = re.compile(r"[A-Za-z0-9._~-]{8,256}\Z")
_CLIENT_SECRET = re.compile(r"[!-~]{16,1024}\Z")
_COMPONENT_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_TEAM_ID = re.compile(r"[a-z0-9_]{1,40}\Z")
_PENDING_ID = re.compile(r"[0-9a-f]{32}\Z")
_REDIRECT_URIS = frozenset(
    {
        oauth_http_client.LOCAL_REDIRECT_URI,
        oauth_http_client.HOSTED_REDIRECT_URI,
    }
)
MAX_REQUIREMENTS = 32
MAX_INTEGRATIONS_PER_REQUIREMENT = 16


class OAuthIntegrationServiceError(RuntimeError):
    """An OAuth integration could not be started or safely completed."""


class OAuthIntegrationUnavailableError(OAuthIntegrationServiceError):
    """No pending integration currently requires provider authorization."""


class OAuthIntegrationDeclarationError(RuntimeError):
    """The trusted installed-Assistant resolver could not return a declaration."""


@dataclass(frozen=True, slots=True)
class OAuthIntegrationCompletion:
    """Public completion identifiers; no authorization material is retained."""

    team_id: str
    assistant_id: str
    integration_id: str
    provider: str
    scopes: tuple[str, ...]
    generation: int


@dataclass(frozen=True, slots=True)
class _Candidate:
    team_id: str
    assistant_id: str
    integration_id: str
    provider: str
    scopes: tuple[str, ...]


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or _COMPONENT_ID.fullmatch(value) is None:
        raise OAuthIntegrationServiceError(f"pending OAuth {label} is unavailable")
    return value


def _declaration(value: object) -> tuple[str, tuple[str, ...]]:
    if isinstance(value, Mapping) and set(value) == {"provider", "scopes"}:
        provider = value.get("provider")
        scopes = value.get("scopes")
    else:
        try:
            provider = value.provider  # type: ignore[attr-defined]
            scopes = value.scopes  # type: ignore[attr-defined]
        except (AttributeError, TypeError) as exc:
            raise OAuthIntegrationServiceError("OAuth integration declaration is unavailable") from exc
    try:
        intent = oauth_providers.integration_intent(provider, scopes)
    except oauth_providers.OAuthProviderError as exc:
        raise OAuthIntegrationServiceError("OAuth integration declaration is unavailable") from exc
    return intent.provider.id, intent.scopes


def _candidates(
    pending: object,
) -> tuple[_Candidate, ...]:
    if not isinstance(pending, assistant_integration_challenges.PendingIntegrationChallenge):
        raise OAuthIntegrationServiceError("pending OAuth integration is unavailable")
    if (
        not isinstance(pending.requirements, tuple)
        or not 1 <= len(pending.requirements) <= MAX_REQUIREMENTS
        or not isinstance(pending.team_id, str)
        or _TEAM_ID.fullmatch(pending.team_id) is None
        or not isinstance(pending.id, str)
        or _PENDING_ID.fullmatch(pending.id) is None
        or not isinstance(pending.expires_at, int | float)
        or isinstance(pending.expires_at, bool)
        or pending.expires_at <= time.monotonic()
    ):
        raise OAuthIntegrationServiceError("pending OAuth integration is unavailable")
    candidates: list[_Candidate] = []
    seen: set[tuple[str, str]] = set()
    for requirement in pending.requirements:
        if (
            not isinstance(requirement, assistant_integration_challenges.IntegrationRequirement)
            or not isinstance(requirement.integrations, tuple)
            or not 1 <= len(requirement.integrations) <= MAX_INTEGRATIONS_PER_REQUIREMENT
        ):
            raise OAuthIntegrationServiceError("pending OAuth integration is unavailable")
        assistant_id = _identifier(requirement.assistant_id, "Assistant")
        for raw_integration in requirement.integrations:
            if not isinstance(raw_integration, tuple) or len(raw_integration) != 3:
                raise OAuthIntegrationServiceError("pending OAuth integration is unavailable")
            integration_id, raw_provider, raw_scopes = raw_integration
            integration_id = _identifier(integration_id, "integration")
            provider, scopes = _declaration({"provider": raw_provider, "scopes": raw_scopes})
            binding = (assistant_id, integration_id)
            if binding in seen:
                raise OAuthIntegrationServiceError("pending OAuth integration is unavailable")
            seen.add(binding)
            candidates.append(
                _Candidate(
                    team_id=pending.team_id,
                    assistant_id=assistant_id,
                    integration_id=integration_id,
                    provider=provider,
                    scopes=scopes,
                )
            )
    return tuple(sorted(candidates, key=lambda item: (item.assistant_id, item.integration_id)))


def _missing_candidate(
    pending: assistant_integration_challenges.PendingIntegrationChallenge,
    store: oauth_integration_store.OAuthIntegrationStore,
) -> _Candidate:
    candidates = _candidates(pending)
    metadata_by_binding: dict[
        tuple[str, str],
        oauth_integration_store.OAuthIntegrationMetadata,
    ] = {}
    by_assistant: dict[str, dict[str, dict[str, object]]] = {}
    for candidate in candidates:
        by_assistant.setdefault(candidate.assistant_id, {})[candidate.integration_id] = {
            "provider": candidate.provider,
            "scopes": candidate.scopes,
        }
    for assistant_id, declarations in by_assistant.items():
        for item in store.metadata(pending.team_id, assistant_id, declarations):
            metadata_by_binding[(assistant_id, item.id)] = item
    selected = next(
        (
            candidate
            for candidate in candidates
            if metadata_by_binding[(candidate.assistant_id, candidate.integration_id)].status
            in {"missing", "refresh-required", "reauthorization-required"}
        ),
        None,
    )
    if selected is None:
        raise OAuthIntegrationUnavailableError("all pending OAuth integrations are already configured")
    return selected


def _authorization_url(
    challenge: oauth_pkce_challenges.OAuthPKCEChallengeStore,
    store: oauth_integration_store.OAuthIntegrationStore,
    build_url: Callable[..., str],
    pending: assistant_integration_challenges.PendingIntegrationChallenge,
    session_binding: object,
) -> str:
    try:
        selected = _missing_candidate(pending, store)
        public = challenge.create(
            session_binding=session_binding,
            team_id=selected.team_id,
            assistant_id=selected.assistant_id,
            integration_id=selected.integration_id,
            provider_id=selected.provider,
            scopes=selected.scopes,
        )
        return build_url(
            provider_id=public.provider_id,
            state=public.state,
            code_challenge=public.code_challenge,
            scopes=public.scopes,
        )
    except OAuthIntegrationUnavailableError:
        raise
    except (
        assistant_integration_challenges.IntegrationChallengeError,
        oauth_integration_store.OAuthIntegrationStoreError,
        oauth_broker_client.OAuthBrokerClientError,
        oauth_http_client.OAuthHTTPError,
        oauth_pkce_challenges.OAuthChallengeError,
        oauth_providers.OAuthProviderError,
        OAuthIntegrationServiceError,
        KeyError,
        TypeError,
    ):
        raise OAuthIntegrationServiceError("OAuth integration could not be started") from None


def _complete(
    challenge: oauth_pkce_challenges.OAuthPKCEChallengeStore,
    store: oauth_integration_store.OAuthIntegrationStore,
    exchange_tokens: Callable[..., object],
    state: object,
    claim_or_code: object,
    session_binding: object,
    resolver: Callable[[str, str, str], object],
) -> OAuthIntegrationCompletion:
    if not callable(resolver):
        raise OAuthIntegrationServiceError("OAuth declaration resolver is unavailable")
    try:
        exchange = challenge.claim_callback(
            state=state,
            session_binding=session_binding,
        )
        try:
            current = resolver(
                exchange.team_id,
                exchange.assistant_id,
                exchange.integration_id,
            )
        except OAuthIntegrationDeclarationError:
            raise OAuthIntegrationServiceError("OAuth integration declaration is unavailable") from None
        provider, scopes = _declaration(current)
        if provider != exchange.provider_id or scopes != exchange.scopes:
            raise OAuthIntegrationServiceError("OAuth integration declaration changed")
        token_set = exchange_tokens(
            provider_id=provider,
            credential=claim_or_code,
            state=state,
            code_verifier=exchange.code_verifier,
            scopes=scopes,
        )
        metadata = store.put(
            exchange.team_id,
            exchange.assistant_id,
            exchange.integration_id,
            provider,
            scopes,
            token_set,
            None,
        )
        return OAuthIntegrationCompletion(
            team_id=exchange.team_id,
            assistant_id=exchange.assistant_id,
            integration_id=exchange.integration_id,
            provider=metadata.provider,
            scopes=metadata.scopes,
            generation=metadata.generation,
        )
    except (
        oauth_integration_store.OAuthIntegrationStoreError,
        oauth_broker_client.OAuthBrokerClientError,
        oauth_http_client.OAuthHTTPError,
        oauth_pkce_challenges.OAuthChallengeError,
        oauth_providers.OAuthProviderError,
        OAuthIntegrationServiceError,
    ):
        raise OAuthIntegrationServiceError("OAuth integration could not be completed") from None


def _exchange_code(
    http: oauth_http_client.OAuthHTTPClient,
    client_configuration: tuple[str, str, str],
    *,
    provider_id: object,
    credential: object,
    state: object,
    code_verifier: object,
    scopes: object,
) -> object:
    del state
    client_id, client_secret, redirect_uri = client_configuration
    return http.exchange_code(
        provider_id=provider_id,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        code=credential,
        code_verifier=code_verifier,
        scopes=scopes,
    )


def _claim_broker(
    broker: oauth_broker_client.OAuthBrokerClient,
    *,
    provider_id: object,
    credential: object,
    state: object,
    code_verifier: object,
    scopes: object,
) -> object:
    return broker.claim(
        provider_id=provider_id,
        claim=credential,
        state=state,
        code_verifier=code_verifier,
        scopes=scopes,
    )


class OAuthIntegrationService:
    """Start and complete only controller-reviewed OAuth Authorization Code flows."""

    def __init__(
        self,
        *,
        client_id: object,
        client_secret: object,
        redirect_uri: object,
        challenge: oauth_pkce_challenges.OAuthPKCEChallengeStore,
        store: oauth_integration_store.OAuthIntegrationStore,
        http: oauth_http_client.OAuthHTTPClient,
    ) -> None:
        if (
            not isinstance(challenge, oauth_pkce_challenges.OAuthPKCEChallengeStore)
            or not isinstance(store, oauth_integration_store.OAuthIntegrationStore)
            or not isinstance(http, oauth_http_client.OAuthHTTPClient)
            or redirect_uri not in _REDIRECT_URIS
        ):
            raise OAuthIntegrationServiceError("OAuth integration service configuration is invalid")
        # An Admin may boot before its Cloudflare OAuth client is configured.
        # Validation is deliberately lazy so only starting/completing OAuth fails.
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = str(redirect_uri)
        self._challenge = challenge
        self._store = store
        self._http = http

    def __repr__(self) -> str:
        return "<OAuthIntegrationService configured>"

    def _client_configuration(self) -> tuple[str, str, str]:
        if (
            not isinstance(self._client_id, str)
            or _CLIENT_ID.fullmatch(self._client_id) is None
            or not isinstance(self._client_secret, str)
            or _CLIENT_SECRET.fullmatch(self._client_secret) is None
        ):
            raise OAuthIntegrationServiceError("OAuth integration client is not configured")
        return self._client_id, self._client_secret, self._redirect_uri

    def authorization_url(
        self,
        pending: assistant_integration_challenges.PendingIntegrationChallenge,
        session_binding: object,
    ) -> str:
        """Create one trusted URL for the first deterministic missing integration."""
        client_id, _client_secret, redirect_uri = self._client_configuration()
        build_url = functools.partial(
            oauth_http_client.authorization_url,
            client_id=client_id,
            redirect_uri=redirect_uri,
        )
        return _authorization_url(self._challenge, self._store, build_url, pending, session_binding)

    def complete(
        self,
        state: object,
        code: object,
        session_binding: object,
        current_declaration_callback: Callable[[str, str, str], object],
    ) -> OAuthIntegrationCompletion:
        """Claim once, revalidate the installed declaration, exchange, and seal tokens."""
        client_configuration = self._client_configuration()
        exchange_tokens = functools.partial(_exchange_code, self._http, client_configuration)
        return _complete(
            self._challenge,
            self._store,
            exchange_tokens,
            state,
            code,
            session_binding,
            current_declaration_callback,
        )

    def disconnect(self, team_id: object, assistant_id: object, integration_id: object) -> bool:
        """Revoke each upstream token before atomically deleting local custody."""

        def revoke(
            provider: str,
            access_token: str,
            refresh_token: str | None,
            _broker_lease: str | None,
        ) -> None:
            client_id, client_secret, _ = self._client_configuration()
            tokens = tuple(dict.fromkeys(token for token in (refresh_token, access_token) if token))
            for token in tokens:
                self._http.revoke(
                    provider_id=provider,
                    client_id=client_id,
                    client_secret=client_secret,
                    token=token,
                )

        try:
            return self._store.revoke_then_delete(
                team_id,
                assistant_id,
                integration_id,
                revoke,
            )
        except (
            oauth_integration_store.OAuthIntegrationStoreError,
            oauth_http_client.OAuthHTTPError,
            OAuthIntegrationServiceError,
        ):
            raise OAuthIntegrationServiceError("OAuth integration could not be disconnected") from None


class BrokeredOAuthIntegrationService:
    """Controller orchestration that never owns an OAuth Client Secret."""

    def __init__(
        self,
        *,
        challenge: oauth_pkce_challenges.OAuthPKCEChallengeStore,
        store: oauth_integration_store.OAuthIntegrationStore,
        broker: oauth_broker_client.OAuthBrokerClient,
    ) -> None:
        if (
            not isinstance(challenge, oauth_pkce_challenges.OAuthPKCEChallengeStore)
            or not isinstance(store, oauth_integration_store.OAuthIntegrationStore)
            or not isinstance(broker, oauth_broker_client.OAuthBrokerClient)
        ):
            raise OAuthIntegrationServiceError("brokered OAuth integration service configuration is invalid")
        self._challenge = challenge
        self._store = store
        self._broker = broker

    def __repr__(self) -> str:
        return "<BrokeredOAuthIntegrationService shimpz.com>"

    def authorization_url(
        self,
        pending: assistant_integration_challenges.PendingIntegrationChallenge,
        session_binding: object,
    ) -> str:
        return _authorization_url(
            self._challenge,
            self._store,
            self._broker.authorization_url,
            pending,
            session_binding,
        )

    def complete(
        self,
        state: object,
        claim: object,
        session_binding: object,
        current_declaration_callback: Callable[[str, str, str], object],
    ) -> OAuthIntegrationCompletion:
        exchange_tokens = functools.partial(_claim_broker, self._broker)
        return _complete(
            self._challenge,
            self._store,
            exchange_tokens,
            state,
            claim,
            session_binding,
            current_declaration_callback,
        )

    def refresh(
        self,
        provider: object,
        scopes: object,
        refresh_token: object,
        broker_lease: object,
    ) -> object:
        try:
            return self._broker.refresh(
                provider_id=provider,
                refresh_token=refresh_token,
                broker_lease=broker_lease,
                scopes=scopes,
            )
        except oauth_broker_client.OAuthBrokerClientError:
            raise OAuthIntegrationServiceError("OAuth integration could not be refreshed") from None

    def disconnect(
        self,
        team_id: object,
        assistant_id: object,
        integration_id: object,
    ) -> bool:
        def revoke(
            provider: str,
            access_token: str,
            refresh_token: str | None,
            broker_lease: str | None,
        ) -> None:
            self._broker.revoke(
                provider_id=provider,
                token=refresh_token or access_token,
                broker_lease=broker_lease,
            )

        try:
            return self._store.revoke_then_delete(
                team_id,
                assistant_id,
                integration_id,
                revoke,
            )
        except (
            oauth_integration_store.OAuthIntegrationStoreError,
            oauth_broker_client.OAuthBrokerClientError,
        ):
            raise OAuthIntegrationServiceError("OAuth integration could not be disconnected") from None
