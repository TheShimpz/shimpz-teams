from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from integrations import broker as integration_broker
from integrations import challenges as integration_challenges
from integrations import http as integration_http
from integrations import pkce as integration_pkce
from integrations import service as integration_service
from integrations import store as integration_store

SCOPES = ("dns.read", "offline_access", "zone.read")
SESSION = "browser-session-private-123456789"
CLAIM = "a" * 64
ACCESS = "access-token-private-123456789"
REFRESH = "refresh-token-private-123456789"
LEASE = f"l2.1999999999.{'b' * 43}.{'c' * 43}.{'d' * 43}.{'e' * 43}"
DECLARATION = {"provider": "cloudflare", "scopes": SCOPES}


class Transport:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def request(self, **request) -> integration_broker.BrokerHTTPResponse:
        self.requests.append(request)
        operation = urlsplit(str(request["url"])).path.rsplit("/", 1)[-1]
        payload = (
            {"revoked": True}
            if operation == "revoke"
            else {
                "access_token": ACCESS,
                "refresh_token": REFRESH,
                "expires_in": 3600,
                "scopes": list(SCOPES),
                "broker_lease": LEASE,
            }
        )
        return integration_broker.BrokerHTTPResponse(
            200,
            "application/json",
            json.dumps(payload, separators=(",", ":")).encode(),
        )


def pending() -> integration_challenges.PendingIntegrationChallenge:
    requirement = integration_challenges.IntegrationRequirement(
        assistant_id="shimpz-cloudflare",
        assistant_name="Shimpz Cloudflare",
        power_ids=("list-zones",),
        integrations=(("cloudflare", "cloudflare", SCOPES),),
    )
    return integration_challenges.IntegrationChallengeStore().create(
        "team_1",
        (requirement,),
        {"private": "continuation"},
    )


class BrokeredOAuthIntegrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = integration_store.OAuthIntegrationStore(
            root / "state" / "integrations.json",
            root / "key" / "aes256.key",
            clock=lambda: 1_000_000_000,
        )
        self.transport = Transport()
        self.challenge = integration_pkce.OAuthPKCEChallengeStore()
        self.service = integration_service.BrokeredOAuthIntegrationService(
            challenge=self.challenge,
            store=self.store,
            broker=integration_broker.OAuthBrokerClient(self.transport),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_full_flow_uses_broker_and_never_requires_client_credentials(self) -> None:
        url = self.service.authorization_url(pending(), SESSION, callback_mode="hosted")
        self.assertEqual(parse_qs(urlsplit(url).query, strict_parsing=True)["callback"], ["hosted"])
        state = parse_qs(urlsplit(url).query, strict_parsing=True)["state"][0]

        completion = self.service.complete(
            state,
            CLAIM,
            SESSION,
            lambda _team, _assistant, _integration: DECLARATION,
        )

        self.assertEqual(
            (completion.team_id, completion.assistant_id, completion.integration_id),
            ("team_1", "shimpz-cloudflare", "cloudflare"),
        )
        self.assertEqual(len(self.transport.requests), 1)
        claim = json.loads(self.transport.requests[0]["body"])
        self.assertEqual(set(claim), {"claim", "state", "code_verifier"})
        self.assertNotIn("client", repr(claim))
        self.assertNotIn("secret", repr(claim))
        self.assertEqual(
            self.store.metadata(
                "team_1",
                "shimpz-cloudflare",
                {"cloudflare": DECLARATION},
            )[0].status,
            "connected",
        )

        self.assertTrue(
            self.service.disconnect(
                "team_1",
                "shimpz-cloudflare",
                "cloudflare",
            )
        )
        self.assertEqual(
            [urlsplit(str(item["url"])).path for item in self.transport.requests],
            ["/api/oauth/cloudflare/claim", "/api/oauth/cloudflare/revoke"],
        )
        revoked = json.loads(self.transport.requests[-1]["body"])
        self.assertEqual(revoked, {"token": REFRESH, "broker_lease": LEASE})

    def test_wrong_session_and_declaration_drift_consume_no_broker_claim(self) -> None:
        for declaration in (
            DECLARATION,
            {"provider": "cloudflare", "scopes": ("zone.read",)},
        ):
            start_session = SESSION if declaration is DECLARATION else "second-browser-session-private-123456789"
            url = self.service.authorization_url(pending(), start_session, callback_mode="loopback")
            state = parse_qs(urlsplit(url).query, strict_parsing=True)["state"][0]
            session = "other-browser-session-private-123" if declaration is DECLARATION else start_session
            with self.assertRaises(integration_service.OAuthIntegrationServiceError):
                self.service.complete(
                    state,
                    CLAIM,
                    session,
                    lambda _team, _assistant, _integration, value=declaration: value,
                )
        self.assertEqual(self.transport.requests, [])

    def test_reconsent_revokes_the_old_broker_grant_before_claiming_the_replacement(self) -> None:
        first = self.store.put(
            "team_1",
            "shimpz-cloudflare",
            "cloudflare",
            "cloudflare",
            SCOPES,
            integration_http.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 3600, LEASE),
        )
        self.store._demote_for_reauthorization("team_1", "shimpz-cloudflare", "cloudflare")
        url = self.service.authorization_url(pending(), SESSION, callback_mode="hosted")
        state = parse_qs(urlsplit(url).query, strict_parsing=True)["state"][0]

        completed = self.service.complete(
            state,
            CLAIM,
            SESSION,
            lambda _team, _assistant, _integration: DECLARATION,
        )

        self.assertEqual((first.generation, completed.generation), (1, 2))
        self.assertEqual(
            [urlsplit(str(item["url"])).path for item in self.transport.requests],
            ["/api/oauth/cloudflare/revoke", "/api/oauth/cloudflare/claim"],
        )

    def test_replacement_normalizes_broker_revocation_failures_for_store_compensation(self) -> None:
        broker = self.service._broker
        with (
            mock.patch.object(
                broker,
                "revoke",
                side_effect=integration_broker.OAuthBrokerClientError("unavailable"),
            ),
            self.assertRaises(integration_store.OAuthIntegrationRevocationError),
        ):
            integration_service._replace_revoke_broker(
                broker,
                "cloudflare",
                ACCESS,
                REFRESH,
                LEASE,
            )

    def test_invalid_callback_mode_creates_no_pkce_challenge(self) -> None:
        with (
            mock.patch.object(self.challenge, "create", wraps=self.challenge.create) as create,
            self.assertRaisesRegex(
                integration_service.OAuthIntegrationServiceError,
                "callback mode is invalid",
            ),
        ):
            self.service.authorization_url(
                pending(),
                SESSION,
                callback_mode="https://attacker.example",
            )

        create.assert_not_called()

    def test_out_of_band_challenge_can_be_cancelled_without_a_broker_claim(self) -> None:
        url = self.service.authorization_url(pending(), SESSION, callback_mode="out-of-band")
        state = parse_qs(urlsplit(url).query, strict_parsing=True)["state"][0]

        self.assertTrue(self.service.cancel(SESSION))
        self.assertFalse(self.service.cancel(SESSION))
        with self.assertRaises(integration_service.OAuthIntegrationServiceError):
            self.service.complete(
                state,
                CLAIM,
                SESSION,
                lambda _team, _assistant, _integration: DECLARATION,
            )
        self.assertEqual(self.transport.requests, [])

    def test_refresh_required_grant_does_not_start_new_authorization(self) -> None:
        now = [1_000_000_000]
        root = Path(self.temporary.name)
        store = integration_store.OAuthIntegrationStore(
            root / "refresh-state" / "integrations.json",
            root / "refresh-key" / "aes256.key",
            clock=lambda: now[0],
        )
        store.put(
            "team_1",
            "shimpz-cloudflare",
            "cloudflare",
            "cloudflare",
            SCOPES,
            integration_http.OAuthTokenSet(
                access_token=ACCESS,
                refresh_token=REFRESH,
                scopes=SCOPES,
                expires_in=30,
                broker_lease=LEASE,
            ),
        )
        now[0] += 31
        service = integration_service.BrokeredOAuthIntegrationService(
            challenge=integration_pkce.OAuthPKCEChallengeStore(),
            store=store,
            broker=integration_broker.OAuthBrokerClient(self.transport),
        )

        with self.assertRaisesRegex(
            integration_service.OAuthIntegrationUnavailableError,
            "already configured",
        ):
            service.authorization_url(pending(), SESSION, callback_mode="hosted")

        self.assertEqual(self.transport.requests, [])

    def test_configuration_repr_and_broker_failures_are_closed(self) -> None:
        self.assertEqual(repr(self.service), "<BrokeredOAuthIntegrationService shimpz.com>")
        with self.assertRaisesRegex(integration_service.OAuthIntegrationServiceError, "configuration"):
            integration_service.BrokeredOAuthIntegrationService(
                challenge=object(),
                store=self.store,
                broker=integration_broker.OAuthBrokerClient(self.transport),
            )
        with self.assertRaisesRegex(integration_service.OAuthIntegrationServiceError, "cancelled"):
            self.service.cancel(None)
        with self.assertRaisesRegex(integration_service.OAuthIntegrationServiceError, "refreshed"):
            self.service.refresh("missing", SCOPES, REFRESH, LEASE)
        with self.assertRaisesRegex(integration_service.OAuthIntegrationServiceError, "disconnected"):
            self.service.disconnect("../team", "assistant", "integration")


if __name__ == "__main__":
    unittest.main()
