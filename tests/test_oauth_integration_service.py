"""The OAuth orchestration every controller shares: selection, one-use state, drift, redaction, and custody."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from test_oauth_broker_client import ACCESS, LEASE, REFRESH, SCOPES, Transport

from integrations import broker as integration_broker
from integrations import challenges as integration_challenges
from integrations import http as integration_http
from integrations import pkce as integration_pkce
from integrations import service as integration_service
from tests import integration_store_fixtures

CLAIM = "a" * 64
SESSION = "browser-session-private-123456789"
OTHER_SESSION = "other-browser-session-123456789"
DECLARATION = {"provider": "cloudflare", "scopes": SCOPES}


class SequenceTransport(Transport):
    """A broker that answers each request with the next queued response."""

    def __init__(self, responses: list[integration_broker.BrokerHTTPResponse]) -> None:
        super().__init__()
        self.responses = responses

    def request(self, **request) -> integration_broker.BrokerHTTPResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def broker_response(status: int, payload: object) -> integration_broker.BrokerHTTPResponse:
    return integration_broker.BrokerHTTPResponse(status, "application/json", json.dumps(payload).encode())


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
    service: integration_service.BrokeredOAuthIntegrationService,
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
        callback_mode="hosted",
    )


class OAuthIntegrationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = integration_store_fixtures.open_store(root)
        self.challenges = integration_pkce.OAuthPKCEChallengeStore()
        self.transport = Transport()
        self.service = self._service(self.transport)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _service(self, transport: Transport) -> integration_service.BrokeredOAuthIntegrationService:
        return integration_service.BrokeredOAuthIntegrationService(
            challenge=self.challenges,
            store=self.store,
            broker=integration_broker.OAuthBrokerClient(transport),
        )

    def _put_grant(self, assistant_id: str = "shimpz-cloudflare"):
        """Store team_1's granted Cloudflare Integration for an Assistant."""
        tokens = integration_http.OAuthTokenSet(ACCESS, REFRESH, SCOPES, 3600, LEASE)
        return self.store.put("team_1", assistant_id, "cloudflare", "cloudflare", SCOPES, tokens)

    @staticmethod
    def _state(url: str) -> str:
        return parse_qs(urlsplit(url).query, strict_parsing=True)["state"][0]

    def _complete(self, state: str, *, session: str = SESSION, service=None):
        return (service or self.service).complete(
            state,
            CLAIM,
            session,
            lambda _team, _assistant, _integration: DECLARATION,
        )

    def test_trusted_url_selects_the_exact_requested_unconfigured_requirement(self) -> None:
        self._put_grant("a-assistant")
        flow = pending(requirement("z-assistant"), requirement("a-assistant"))

        url = authorization(self.service, flow, SESSION, assistant_id="z-assistant")
        query = parse_qs(urlsplit(url).query, strict_parsing=True)
        self.assertEqual(query["scope"], [" ".join(SCOPES)])

        completed = self._complete(query["state"][0])
        self.assertEqual(
            (completed.team_id, completed.assistant_id, completed.integration_id),
            ("team_1", "z-assistant", "cloudflare"),
        )
        self.assertEqual(completed.provider, "cloudflare")
        # Generations are store-wide: the pre-connected a-assistant grant consumed generation 1.
        self.assertEqual(completed.generation, 2)
        for private in (CLAIM, ACCESS, REFRESH, LEASE, query["state"][0], "verifier"):
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
                        CLAIM,
                        SESSION,
                        lambda _team, _assistant, _integration, value=current: value,
                    )
                self.assertEqual(self.transport.requests, [])
                with self.assertRaises(integration_service.OAuthIntegrationServiceError):
                    self._complete(state)

    def test_provider_and_scope_injection_fail_closed(self) -> None:
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

    def test_broker_response_and_callback_errors_never_reflect_private_values(self) -> None:
        leaked = "broker-private-response-123456789"
        transport = SequenceTransport(
            [broker_response(200, {"access_token": leaked, "unexpected": leaked, "broker_lease": LEASE})]
        )
        service = self._service(transport)
        state = self._state(authorization(service, pending(requirement()), SESSION))
        with self.assertRaises(integration_service.OAuthIntegrationServiceError) as captured:
            self._complete(state, service=service)
        rendered = f"{captured.exception!r} {captured.exception}"
        for private in (leaked, CLAIM, LEASE, state, "verifier"):
            self.assertNotIn(private, rendered)

        next_state = self._state(authorization(service, pending(requirement()), SESSION))
        callback_secret = "-".join(("manifest", "parser", "private", "value", "123456789"))
        with self.assertRaises(integration_service.OAuthIntegrationServiceError) as callback:
            service.complete(
                next_state,
                CLAIM,
                SESSION,
                lambda _team, _assistant, _integration: (_ for _ in ()).throw(
                    integration_service.OAuthIntegrationDeclarationError(callback_secret)
                ),
            )
        self.assertNotIn(callback_secret, f"{callback.exception!r} {callback.exception}")

    def test_disconnect_failure_retains_custody_and_is_safely_retryable(self) -> None:
        self._put_grant()
        private_detail = "private-broker-detail-123456789"
        transport = SequenceTransport([broker_response(503, {"error": private_detail})])
        service = self._service(transport)

        with self.assertRaises(integration_service.OAuthIntegrationServiceError) as failed:
            service.disconnect("team_1", "shimpz-cloudflare", "cloudflare")
        rendered = f"{failed.exception!r} {failed.exception}"
        for private in (ACCESS, REFRESH, LEASE, private_detail):
            self.assertNotIn(private, rendered)
        self.assertEqual(
            self.store.metadata("team_1", "shimpz-cloudflare", {"cloudflare": DECLARATION})[0].status,
            "connected",
        )

        transport.responses.append(broker_response(200, {"revoked": True}))
        self.assertTrue(service.disconnect("team_1", "shimpz-cloudflare", "cloudflare"))
        self.assertFalse(service.disconnect("team_1", "shimpz-cloudflare", "cloudflare"))
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(
            self.store.metadata("team_1", "shimpz-cloudflare", {"cloudflare": DECLARATION})[0].status,
            "missing",
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
                CLAIM,
                SESSION,
                None,
            )


if __name__ == "__main__":
    unittest.main()
