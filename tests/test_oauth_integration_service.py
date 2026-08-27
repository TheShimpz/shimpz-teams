from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from integrations import challenges as integration_challenges
from integrations import http as integration_http
from integrations import pkce as integration_pkce
from integrations import service as integration_service
from integrations import store as integration_store

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
    def __init__(self, response: integration_http.OAuthHTTPResponse | None = None) -> None:
        self.response = response or integration_http.OAuthHTTPResponse(
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

    def request(self, **request) -> integration_http.OAuthHTTPResponse:
        self.requests.append(request)
        return self.response


class SequenceTransport:
    def __init__(self, responses: list[integration_http.OAuthHTTPResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def request(self, **request) -> integration_http.OAuthHTTPResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def requirement(
    assistant: str = "shimpz-cloudflare",
    *,
    provider: str = "cloudflare",
    scopes: tuple[str, ...] = SCOPES,
) -> integration_challenges.IntegrationRequirement:
    return integration_challenges.IntegrationRequirement(
        assistant_id=assistant,
        assistant_name=assistant,
        action_ids=("list-zones",),
        integrations=(("cloudflare", provider, scopes),),
    )


def pending(
    *requirements: integration_challenges.IntegrationRequirement,
    team: str = "team_1",
) -> integration_challenges.PendingIntegrationChallenge:
    return integration_challenges.IntegrationChallengeStore().create(
        team,
        tuple(requirements or (requirement(),)),
        {"private": "paused user input"},
    )


def authorization(
    service: integration_service.OAuthIntegrationService,
    flow: integration_challenges.PendingIntegrationChallenge,
    session: str,
    *,
    assistant_id: str = "shimpz-cloudflare",
    integration_id: str = "cloudflare",
) -> str:
    return service.authorization_url(
        flow,
        session,
        assistant_id=assistant_id,
        integration_id=integration_id,
    )


class OAuthIntegrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = integration_store.OAuthIntegrationStore(
            root / "state" / "integrations.json",
            root / "key" / "aes256.key",
            clock=lambda: 1_000_000_000,
        )
        self.challenges = integration_pkce.OAuthPKCEChallengeStore()
        self.transport = SyntheticTransport()
        self.http = integration_http.OAuthHTTPClient(self.transport)
        self.service = integration_service.OAuthIntegrationService(
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            redirect_uri=integration_http.HOSTED_REDIRECT_URI,
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

    def test_trusted_url_selects_the_exact_requested_unconfigured_requirement(self) -> None:
        self.store.put(
            "team_1",
            "a-assistant",
            "cloudflare",
            "cloudflare",
            SCOPES,
            integration_http.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 3600),
        )
        flow = pending(requirement("z-assistant"), requirement("a-assistant"))

        url = authorization(self.service, flow, SESSION, assistant_id="z-assistant")
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, strict_parsing=True)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", "dash.cloudflare.com", "/oauth2/auth"))
        self.assertEqual(query["redirect_uri"], [integration_http.HOSTED_REDIRECT_URI])
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

    def test_authorization_rejects_a_pair_outside_the_pending_challenge(self) -> None:
        flow = pending(requirement("a-assistant"), requirement("z-assistant"))

        with self.assertRaisesRegex(
            integration_service.OAuthIntegrationUnavailableError,
            "requested pending OAuth integration is unavailable",
        ):
            authorization(
                self.service,
                flow,
                SESSION,
                assistant_id="missing-assistant",
            )

        self.assertEqual(self.challenges.cancel_all(), 0)
        url = authorization(self.service, flow, SESSION, assistant_id="z-assistant")
        completed = self._complete(self._state(url))
        self.assertEqual(completed.assistant_id, "z-assistant")

    def test_wrong_session_does_not_consume_but_success_and_replay_are_one_use(self) -> None:
        state = self._state(authorization(self.service, pending(requirement()), SESSION))
        with self.assertRaises(integration_service.OAuthIntegrationServiceError):
            self._complete(state, session=OTHER_SESSION)
        self.assertEqual(self.transport.requests, [])

        completed = self._complete(state)
        self.assertEqual(completed.integration_id, "cloudflare")
        self.assertEqual(len(self.transport.requests), 1)
        with self.assertRaises(integration_service.OAuthIntegrationServiceError):
            self._complete(state)
        self.assertEqual(len(self.transport.requests), 1)

    def test_reconsent_revokes_both_old_tokens_before_exchanging_the_replacement(self) -> None:
        first = self.store.put(
            "team_1",
            "shimpz-cloudflare",
            "cloudflare",
            "cloudflare",
            SCOPES,
            integration_http.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 3600),
        )
        self.store._demote_for_reauthorization("team_1", "shimpz-cloudflare", "cloudflare")
        state = self._state(authorization(self.service, pending(requirement()), SESSION))

        completed = self._complete(state)

        self.assertEqual((first.generation, completed.generation), (1, 2))
        self.assertEqual(
            [urlsplit(str(item["url"])).path for item in self.transport.requests],
            ["/oauth2/revoke", "/oauth2/revoke", "/oauth2/token"],
        )
        revoked = [parse_qs(item["body"].decode())["token"] for item in self.transport.requests[:2]]
        self.assertEqual(revoked, [[REFRESH], [ACCESS]])

    def test_replacement_normalizes_direct_revocation_failures_for_store_compensation(self) -> None:
        with (
            mock.patch.object(self.http, "revoke", side_effect=integration_http.OAuthHTTPError("unavailable")),
            self.assertRaises(integration_store.OAuthIntegrationRevocationError),
        ):
            integration_service._replace_revoke_direct(
                self.http,
                (CLIENT_ID, CLIENT_CREDENTIAL, integration_http.HOSTED_REDIRECT_URI),
                "cloudflare",
                ACCESS,
                REFRESH,
                None,
            )

    def test_install_or_scope_drift_consumes_state_before_any_exchange(self) -> None:
        drifted = (
            None,
            {"provider": "cloudflare", "scopes": ("dns.read",)},
        )
        for current in drifted:
            with self.subTest(current=current):
                state = self._state(authorization(self.service, pending(requirement()), SESSION))
                with self.assertRaises(integration_service.OAuthIntegrationServiceError):
                    self.service.complete(
                        state,
                        CODE,
                        SESSION,
                        lambda _team, _assistant, _integration, value=current: value,
                    )
                self.assertEqual(self.transport.requests, [])
                with self.assertRaises(integration_service.OAuthIntegrationServiceError):
                    self._complete(state)

    def test_provider_scope_and_configuration_injection_fail_closed(self) -> None:
        for malicious in (
            requirement(provider="https://evil.example/token"),
            requirement(scopes=("dns.read", "https://evil.example")),
        ):
            with (
                self.subTest(malicious=malicious),
                self.assertRaises(integration_service.OAuthIntegrationServiceError),
            ):
                authorization(self.service, pending(malicious), SESSION)
        self.assertEqual(self.challenges.cancel_all(), 0)
        self.assertEqual(self.transport.requests, [])

        malformed = integration_challenges.PendingIntegrationChallenge(
            id="0" * 32,
            team_id="team_1",
            expires_at=0,
            requirements=(requirement(),),
            payload=None,
        )
        with self.assertRaises(integration_service.OAuthIntegrationServiceError):
            authorization(self.service, malformed, SESSION)

        lazy = integration_service.OAuthIntegrationService(
            client_id=None,
            client_secret=None,
            redirect_uri=integration_http.HOSTED_REDIRECT_URI,
            challenge=self.challenges,
            store=self.store,
            http=self.http,
        )
        self.assertNotIn(CLIENT_ID, repr(self.service))
        with self.assertRaisesRegex(
            integration_service.OAuthIntegrationServiceError,
            "not configured",
        ):
            authorization(lazy, pending(requirement()), SESSION)
        with self.assertRaises(integration_service.OAuthIntegrationServiceError):
            integration_service.OAuthIntegrationService(
                client_id=CLIENT_ID,
                client_secret=CLIENT_CREDENTIAL,
                redirect_uri="https://evil.example/callback",
                challenge=self.challenges,
                store=self.store,
                http=self.http,
            )

    def test_expired_refreshable_integration_does_not_start_fresh_authorization(self) -> None:
        root = Path(self.temporary.name)
        now = [1_000]
        store = integration_store.OAuthIntegrationStore(
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
            integration_http.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 30),
        )
        now[0] = 1_031
        service = integration_service.OAuthIntegrationService(
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            redirect_uri=integration_http.HOSTED_REDIRECT_URI,
            challenge=integration_pkce.OAuthPKCEChallengeStore(),
            store=store,
            http=self.http,
        )
        with self.assertRaisesRegex(
            integration_service.OAuthIntegrationUnavailableError,
            "already configured",
        ):
            authorization(service, pending(requirement()), SESSION)

    def test_provider_response_and_callback_errors_never_reflect_private_values(self) -> None:
        leaked = "provider-private-response-123456789"
        transport = SyntheticTransport(
            integration_http.OAuthHTTPResponse(
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
        service = integration_service.OAuthIntegrationService(
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            redirect_uri=integration_http.HOSTED_REDIRECT_URI,
            challenge=self.challenges,
            store=self.store,
            http=integration_http.OAuthHTTPClient(transport),
        )
        state = self._state(authorization(service, pending(requirement()), SESSION))
        with self.assertRaises(integration_service.OAuthIntegrationServiceError) as captured:
            service.complete(
                state,
                CODE,
                SESSION,
                lambda _team, _assistant, _integration: DECLARATION,
            )
        rendered = f"{captured.exception!r} {captured.exception}"
        for private in (leaked, ACCESS, REFRESH, CODE, CLIENT_ID, CLIENT_CREDENTIAL, state, "verifier"):
            self.assertNotIn(private, rendered)

        next_state = self._state(authorization(service, pending(requirement()), SESSION))
        callback_secret = "-".join(("manifest", "parser", "private", "value", "123456789"))
        with self.assertRaises(integration_service.OAuthIntegrationServiceError) as callback:
            service.complete(
                next_state,
                CODE,
                SESSION,
                lambda _team, _assistant, _integration: (_ for _ in ()).throw(
                    integration_service.OAuthIntegrationDeclarationError(callback_secret)
                ),
            )
        self.assertNotIn(callback_secret, f"{callback.exception!r} {callback.exception}")

    def test_disconnect_revokes_refresh_and_access_before_local_delete(self) -> None:
        state = self._state(authorization(self.service, pending(requirement()), SESSION))
        self._complete(state)
        requests = len(self.transport.requests)
        with self.assertRaises(integration_service.OAuthIntegrationUnavailableError):
            authorization(self.service, pending(requirement()), SESSION)
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
            integration_http.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 3600),
        )
        private_provider_body = b'{"error":"private-provider-detail-123456789"}'
        transport = SequenceTransport(
            [
                integration_http.OAuthHTTPResponse(200, "application/json", b"{}"),
                integration_http.OAuthHTTPResponse(503, "application/json", private_provider_body),
            ]
        )
        service = integration_service.OAuthIntegrationService(
            client_id=CLIENT_ID,
            client_secret=CLIENT_CREDENTIAL,
            redirect_uri=integration_http.HOSTED_REDIRECT_URI,
            challenge=self.challenges,
            store=self.store,
            http=integration_http.OAuthHTTPClient(transport),
        )

        with self.assertRaises(integration_service.OAuthIntegrationServiceError) as failed:
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
                integration_http.OAuthHTTPResponse(200, "application/json", b"{}"),
                integration_http.OAuthHTTPResponse(200, "application/json", b"{}"),
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
            integration_http.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 3600),
        )
        service = integration_service.OAuthIntegrationService(
            client_id=None,
            client_secret=None,
            redirect_uri=integration_http.HOSTED_REDIRECT_URI,
            challenge=self.challenges,
            store=self.store,
            http=self.http,
        )
        with self.assertRaises(integration_service.OAuthIntegrationServiceError):
            service.disconnect("team_1", "shimpz-cloudflare", "cloudflare")
        self.assertEqual(self.transport.requests, [])
        self.assertEqual(
            self.store.metadata("team_1", "shimpz-cloudflare", {"cloudflare": DECLARATION})[0].status,
            "connected",
        )

    def test_candidate_projection_rejects_malformed_pending_contracts(self) -> None:
        self.assertEqual(
            integration_service._declaration(SimpleNamespace(provider="cloudflare", scopes=SCOPES)),
            ("cloudflare", SCOPES),
        )
        with self.assertRaises(integration_service.OAuthIntegrationServiceError):
            integration_service._identifier("Bad", "Assistant")
        with self.assertRaises(integration_service.OAuthIntegrationServiceError):
            integration_service._declaration(object())
        with self.assertRaises(integration_service.OAuthIntegrationServiceError):
            integration_service._candidates(object())

        malformed_requirement = integration_challenges.IntegrationRequirement(
            assistant_id="assistant",
            assistant_name="Assistant",
            action_ids=("action",),
            integrations=(),
        )
        malformed_integration = integration_challenges.IntegrationRequirement(
            assistant_id="assistant",
            assistant_name="Assistant",
            action_ids=("action",),
            integrations=(("invalid",),),
        )
        duplicate = requirement("assistant")
        for requirements in (
            (malformed_requirement,),
            (malformed_integration,),
            (duplicate, duplicate),
        ):
            challenge = integration_challenges.PendingIntegrationChallenge(
                id="0" * 32,
                team_id="team_1",
                expires_at=time.monotonic() + 60,
                requirements=requirements,
                payload=None,
            )
            with (
                self.subTest(requirements=requirements),
                self.assertRaises(integration_service.OAuthIntegrationServiceError),
            ):
                integration_service._candidates(challenge)

    def test_completion_requires_a_live_declaration_resolver(self) -> None:
        with self.assertRaisesRegex(integration_service.OAuthIntegrationServiceError, "resolver"):
            integration_service._complete(
                self.challenges,
                self.store,
                mock.Mock(),
                "state",
                CODE,
                SESSION,
                None,
            )


if __name__ == "__main__":
    unittest.main()
