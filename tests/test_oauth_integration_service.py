from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from assistant_human import (
    assistant_integration_challenges,
    oauth_http_client,
    oauth_integration_service,
    oauth_integration_store,
    oauth_pkce_challenges,
)

CLIENT_ID = "cloudflare-client-123456789"
CLIENT_CREDENTIAL = "cloudflare-secret-private-123456789"
CODE = "authorization-code-private-123456789"
SESSION = "browser-session-private-123456789"
OTHER_SESSION = "other-browser-session-123456789"
SCOPES = ("dns.read", "offline_access", "zone.read")
DECLARATION = {"provider": "cloudflare", "scopes": SCOPES}
ACCESS = "access-token-private-123456789"
REFRESH = "refresh-token-private-987654321"


class SyntheticTransport:
    def __init__(self, response: oauth_http_client.OAuthHTTPResponse | None = None) -> None:
        self.response = response or oauth_http_client.OAuthHTTPResponse(
            200,
            "application/json",
            json.dumps(
                {
                    "access_token": ACCESS,
                    "refresh_token": REFRESH,
                    "token_type": "bearer",
                    "expires_in": 3600,
                    "scope": " ".join(SCOPES),
                }
            ).encode(),
        )
        self.requests: list[dict[str, object]] = []

    def request(self, **request) -> oauth_http_client.OAuthHTTPResponse:
        self.requests.append(request)
        return self.response


class SequenceTransport:
    def __init__(self, responses: list[oauth_http_client.OAuthHTTPResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def request(self, **request) -> oauth_http_client.OAuthHTTPResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def requirement(
    assistant: str = "shimpz-cloudflare",
    *,
    provider: str = "cloudflare",
    scopes: tuple[str, ...] = SCOPES,
) -> assistant_integration_challenges.IntegrationRequirement:
    return assistant_integration_challenges.IntegrationRequirement(
        assistant_id=assistant,
        assistant_name=assistant,
        power_ids=("list-zones",),
        integrations=(("cloudflare", provider, scopes),),
    )


def pending(
    *requirements: assistant_integration_challenges.IntegrationRequirement,
    team: str = "team_1",
) -> assistant_integration_challenges.PendingIntegrationChallenge:
    return assistant_integration_challenges.IntegrationChallengeStore().create(
        team,
        tuple(requirements or (requirement(),)),
        {"private": "paused user input"},
    )


class OAuthIntegrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = oauth_integration_store.OAuthIntegrationStore(
            root / "state" / "integrations.json",
            root / "key" / "aes256.key",
            clock=lambda: 1_000_000_000,
        )
        self.challenges = oauth_pkce_challenges.OAuthPKCEChallengeStore()
        self.transport = SyntheticTransport()
        self.http = oauth_http_client.OAuthHTTPClient(self.transport)
        self.service = oauth_integration_service.OAuthIntegrationService(
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            redirect_uri=oauth_http_client.LOCAL_REDIRECT_URI,
            challenge=self.challenges,
            store=self.store,
            http=self.http,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _state(url: str) -> str:
        return parse_qs(urlsplit(url).query, strict_parsing=True)["state"][0]

    def _complete(self, state: str, *, session: str = SESSION):
        return self.service.complete(
            state,
            CODE,
            session,
            lambda _team, _assistant, _integration: DECLARATION,
        )

    def test_trusted_url_selects_first_deterministic_unconfigured_requirement(self) -> None:
        self.store.put(
            "team_1",
            "a-assistant",
            "cloudflare",
            "cloudflare",
            SCOPES,
            oauth_http_client.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 3600),
        )
        flow = pending(requirement("z-assistant"), requirement("a-assistant"))

        url = self.service.authorization_url(flow, SESSION)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, strict_parsing=True)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", "dash.cloudflare.com", "/oauth2/auth"))
        self.assertEqual(query["redirect_uri"], [oauth_http_client.LOCAL_REDIRECT_URI])
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["scope"], [" ".join(SCOPES)])

        completed = self._complete(query["state"][0])
        self.assertEqual(
            (completed.team_id, completed.assistant_id, completed.integration_id),
            ("team_1", "z-assistant", "cloudflare"),
        )
        self.assertEqual(completed.provider, "cloudflare")
        self.assertEqual(completed.generation, 1)
        for private in (CODE, ACCESS, REFRESH, CLIENT_ID, CLIENT_CREDENTIAL, query["state"][0], "verifier"):
            self.assertNotIn(private, repr(completed))
        metadata = self.store.metadata("team_1", "z-assistant", {"cloudflare": DECLARATION})[0]
        self.assertEqual(metadata.status, "connected")
        self.assertIsNone(metadata.integration)

    def test_wrong_session_does_not_consume_but_success_and_replay_are_one_use(self) -> None:
        state = self._state(self.service.authorization_url(pending(requirement()), SESSION))
        with self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError):
            self._complete(state, session=OTHER_SESSION)
        self.assertEqual(self.transport.requests, [])

        completed = self._complete(state)
        self.assertEqual(completed.integration_id, "cloudflare")
        self.assertEqual(len(self.transport.requests), 1)
        with self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError):
            self._complete(state)
        self.assertEqual(len(self.transport.requests), 1)

    def test_install_or_scope_drift_consumes_state_before_any_exchange(self) -> None:
        drifted = (
            None,
            {"provider": "cloudflare", "scopes": ("dns.read",)},
        )
        for current in drifted:
            with self.subTest(current=current):
                state = self._state(self.service.authorization_url(pending(requirement()), SESSION))
                with self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError):
                    self.service.complete(
                        state,
                        CODE,
                        SESSION,
                        lambda _team, _assistant, _integration, value=current: value,
                    )
                self.assertEqual(self.transport.requests, [])
                with self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError):
                    self._complete(state)

    def test_provider_scope_and_configuration_injection_fail_closed(self) -> None:
        for malicious in (
            requirement(provider="https://evil.example/token"),
            requirement(scopes=("dns.read", "https://evil.example")),
        ):
            with (
                self.subTest(malicious=malicious),
                self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError),
            ):
                self.service.authorization_url(pending(malicious), SESSION)
        self.assertEqual(self.challenges.cancel_all(), 0)
        self.assertEqual(self.transport.requests, [])

        malformed = assistant_integration_challenges.PendingIntegrationChallenge(
            id="0" * 32,
            team_id="team_1",
            expires_at=0,
            requirements=(requirement(),),
            payload=None,
        )
        with self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError):
            self.service.authorization_url(malformed, SESSION)

        lazy = oauth_integration_service.OAuthIntegrationService(
            client_id=None,
            client_secret=None,
            redirect_uri=oauth_http_client.LOCAL_REDIRECT_URI,
            challenge=self.challenges,
            store=self.store,
            http=self.http,
        )
        self.assertNotIn(CLIENT_ID, repr(self.service))
        with self.assertRaisesRegex(
            oauth_integration_service.OAuthIntegrationServiceError,
            "not configured",
        ):
            lazy.authorization_url(pending(requirement()), SESSION)
        with self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError):
            oauth_integration_service.OAuthIntegrationService(
                client_id=CLIENT_ID,
                client_secret=CLIENT_CREDENTIAL,
                redirect_uri="https://evil.example/callback",
                challenge=self.challenges,
                store=self.store,
                http=self.http,
            )

    def test_expired_stored_integration_can_start_fresh_authorization(self) -> None:
        root = Path(self.temporary.name)
        now = [1_000]
        store = oauth_integration_store.OAuthIntegrationStore(
            root / "expired-state" / "integrations.json",
            root / "expired-key" / "aes256.key",
            clock=lambda: now[0],
        )
        store.put(
            "team_1",
            "shimpz-cloudflare",
            "cloudflare",
            "cloudflare",
            SCOPES,
            oauth_http_client.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 30),
        )
        now[0] = 1_031
        service = oauth_integration_service.OAuthIntegrationService(
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            redirect_uri=oauth_http_client.LOCAL_REDIRECT_URI,
            challenge=oauth_pkce_challenges.OAuthPKCEChallengeStore(),
            store=store,
            http=self.http,
        )
        url = service.authorization_url(pending(requirement()), SESSION)
        self.assertEqual(urlsplit(url).hostname, "dash.cloudflare.com")

    def test_provider_response_and_callback_errors_never_reflect_private_values(self) -> None:
        leaked = "provider-private-response-123456789"
        transport = SyntheticTransport(
            oauth_http_client.OAuthHTTPResponse(
                200,
                "application/json",
                json.dumps(
                    {
                        "access_token": leaked,
                        "token_type": "bearer",
                        "expires_in": 3600,
                        "scope": " ".join(SCOPES),
                        "unexpected": leaked,
                    }
                ).encode(),
            )
        )
        service = oauth_integration_service.OAuthIntegrationService(
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            redirect_uri=oauth_http_client.LOCAL_REDIRECT_URI,
            challenge=self.challenges,
            store=self.store,
            http=oauth_http_client.OAuthHTTPClient(transport),
        )
        state = self._state(service.authorization_url(pending(requirement()), SESSION))
        with self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError) as captured:
            service.complete(
                state,
                CODE,
                SESSION,
                lambda _team, _assistant, _integration: DECLARATION,
            )
        rendered = f"{captured.exception!r} {captured.exception}"
        for private in (leaked, ACCESS, REFRESH, CODE, CLIENT_ID, CLIENT_CREDENTIAL, state, "verifier"):
            self.assertNotIn(private, rendered)

        next_state = self._state(service.authorization_url(pending(requirement()), SESSION))
        callback_secret = "-".join(("manifest", "parser", "private", "value", "123456789"))
        with self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError) as callback:
            service.complete(
                next_state,
                CODE,
                SESSION,
                lambda _team, _assistant, _integration: (_ for _ in ()).throw(
                    oauth_integration_service.OAuthIntegrationDeclarationError(callback_secret)
                ),
            )
        self.assertNotIn(callback_secret, f"{callback.exception!r} {callback.exception}")

    def test_disconnect_revokes_refresh_and_access_before_local_delete(self) -> None:
        state = self._state(self.service.authorization_url(pending(requirement()), SESSION))
        self._complete(state)
        requests = len(self.transport.requests)
        with self.assertRaises(oauth_integration_service.OAuthIntegrationUnavailableError):
            self.service.authorization_url(pending(requirement()), SESSION)
        self.assertTrue(self.service.disconnect("team_1", "shimpz-cloudflare", "cloudflare"))
        self.assertFalse(self.service.disconnect("team_1", "shimpz-cloudflare", "cloudflare"))
        self.assertEqual(len(self.transport.requests), requests + 2)
        revoked = [
            parse_qs(bytes(request["body"]).decode(), strict_parsing=True)["token"][0]
            for request in self.transport.requests[-2:]
        ]
        self.assertEqual(revoked, [REFRESH, ACCESS])
        self.assertTrue(
            all(
                request["url"] == "https://dash.cloudflare.com/oauth2/revoke"
                for request in self.transport.requests[-2:]
            )
        )
        self.assertEqual(
            self.store.metadata("team_1", "shimpz-cloudflare", {"cloudflare": DECLARATION})[0].status,
            "missing",
        )

    def test_disconnect_failure_retains_custody_and_is_safely_retryable(self) -> None:
        self.store.put(
            "team_1",
            "shimpz-cloudflare",
            "cloudflare",
            "cloudflare",
            SCOPES,
            oauth_http_client.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 3600),
        )
        private_provider_body = b'{"error":"private-provider-detail-123456789"}'
        transport = SequenceTransport(
            [
                oauth_http_client.OAuthHTTPResponse(200, "application/json", b"{}"),
                oauth_http_client.OAuthHTTPResponse(503, "application/json", private_provider_body),
            ]
        )
        service = oauth_integration_service.OAuthIntegrationService(
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            redirect_uri=oauth_http_client.LOCAL_REDIRECT_URI,
            challenge=self.challenges,
            store=self.store,
            http=oauth_http_client.OAuthHTTPClient(transport),
        )

        with self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError) as failed:
            service.disconnect("team_1", "shimpz-cloudflare", "cloudflare")
        rendered = f"{failed.exception!r} {failed.exception}"
        for private in (ACCESS, REFRESH, CLIENT_ID, CLIENT_CREDENTIAL, "private-provider-detail"):
            self.assertNotIn(private, rendered)
        self.assertEqual(
            self.store.metadata("team_1", "shimpz-cloudflare", {"cloudflare": DECLARATION})[0].status,
            "connected",
        )

        transport.responses.extend(
            [
                oauth_http_client.OAuthHTTPResponse(200, "application/json", b"{}"),
                oauth_http_client.OAuthHTTPResponse(200, "application/json", b"{}"),
            ]
        )
        self.assertTrue(service.disconnect("team_1", "shimpz-cloudflare", "cloudflare"))
        self.assertFalse(service.disconnect("team_1", "shimpz-cloudflare", "cloudflare"))
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(
            self.store.metadata("team_1", "shimpz-cloudflare", {"cloudflare": DECLARATION})[0].status,
            "missing",
        )

    def test_disconnect_without_client_configuration_retains_local_custody(self) -> None:
        self.store.put(
            "team_1",
            "shimpz-cloudflare",
            "cloudflare",
            "cloudflare",
            SCOPES,
            oauth_http_client.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 3600),
        )
        service = oauth_integration_service.OAuthIntegrationService(
            client_id=None,
            client_secret=None,
            redirect_uri=oauth_http_client.LOCAL_REDIRECT_URI,
            challenge=self.challenges,
            store=self.store,
            http=self.http,
        )
        with self.assertRaises(oauth_integration_service.OAuthIntegrationServiceError):
            service.disconnect("team_1", "shimpz-cloudflare", "cloudflare")
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(
            self.store.metadata("team_1", "shimpz-cloudflare", {"cloudflare": DECLARATION})[0].status,
            "connected",
        )


if __name__ == "__main__":
    unittest.main()
