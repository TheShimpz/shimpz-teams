"""Local OAuth completion holds the Team lifecycle lock from declaration check through exchange and sealing.

An uninstall or Team destruction deletes an Assistant's credentials under the same lock. It either finishes before
the completion checks the declaration, which then fails, or waits for the seal and deletes the new grant too; it never
finishes in the middle of the broker exchange so that the completion recreates what it deleted.
"""

from __future__ import annotations

import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from integrations import broker as integration_broker
from integrations import challenges as integration_challenges
from integrations import pkce as integration_pkce
from integrations import service as integration_service
from local import app as local_app
from local.chat import private as local_chat_private
from tests import integration_store_fixtures

TEAM = "team_1"
ASSISTANT = "shimpz-cloudflare"
SCOPES = ("dns.read", "offline_access", "zone.read")
SESSION = "browser-session-private-123456789"
CLAIM = "a" * 64
LEASE = f"l2.1999999999.{'b' * 43}.{'c' * 43}.{'d' * 43}.{'e' * 43}"
DECLARATION = types.SimpleNamespace(provider="cloudflare", scopes=SCOPES)


class BlockingBroker:
    """A broker whose claim waits until the test lets the exchange finish."""

    def __init__(self) -> None:
        self.exchanging = threading.Event()
        self.finish = threading.Event()

    def request(self, **request) -> integration_broker.BrokerHTTPResponse:
        if urlsplit(str(request["url"])).path.endswith("/claim"):
            self.exchanging.set()
            if not self.finish.wait(5):
                raise AssertionError("the exchange was never released")
        payload = {
            "access_token": "access-token-private-123456789",
            "refresh_token": "refresh-token-private-123456789",
            "expires_in": 3600,
            "scopes": list(SCOPES),
            "broker_lease": LEASE,
        }
        return integration_broker.BrokerHTTPResponse(200, "application/json", json.dumps(payload).encode())


class LocalOAuthCompletionLockTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = integration_store_fixtures.open_store(root)
        self.broker = BlockingBroker()
        self.pkce = integration_pkce.OAuthPKCEChallengeStore()
        self.service = integration_service.BrokeredOAuthIntegrationService(
            challenge=self.pkce,
            store=self.store,
            broker=integration_broker.OAuthBrokerClient(self.broker),
        )
        self.lock = threading.RLock()
        self.installed = True
        spec = types.SimpleNamespace(assistant_id=ASSISTANT, integrations={"cloudflare": DECLARATION})
        container = types.SimpleNamespace(reload=lambda: None, attrs={"Config": {}})
        self.subject = types.SimpleNamespace(
            _lock=lambda _team_id: self.lock,
            oauth_pkce=self.pkce,
            oauth_service=self.service,
            assistant_lifecycle=types.SimpleNamespace(
                _resolve=lambda *_args: spec,
                _assistant_specs=lambda *_args, **_kwargs: (spec,) if self.installed else (),
                _assistant_container=lambda *_args: container,
                _has_current_assistant_artifact=lambda *_args: True,
            ),
        )
        self.subject._current_integration_declaration = lambda *args: (
            local_chat_private._current_integration_declaration(self.subject, *args)
        )

    def _state(self) -> str:
        requirement = integration_challenges.IntegrationRequirement(
            assistant_id=ASSISTANT,
            assistant_name="Shimpz Cloudflare",
            action_ids=("list-zones",),
            integrations=(("cloudflare", "cloudflare", SCOPES),),
        )
        pending = integration_challenges.IntegrationChallengeStore().create(TEAM, (requirement,), {"private": 1})
        url = self.service.authorization_url(
            pending, SESSION, assistant_id=ASSISTANT, integration_id="cloudflare", callback_mode="hosted"
        )
        return parse_qs(urlsplit(url).query, strict_parsing=True)["state"][0]

    def _uninstall(self) -> None:
        # The uninstall's credential deletion, under the Team lifecycle lock as the real one runs it.
        with self.lock:
            self.installed = False
            self.store.delete_assistant(TEAM, ASSISTANT)

    def _status(self) -> str:
        return self.store.metadata(TEAM, ASSISTANT, {"cloudflare": {"provider": "cloudflare", "scopes": SCOPES}})[
            0
        ].status

    def test_an_uninstall_during_the_exchange_leaves_no_credential(self) -> None:
        state = self._state()
        outcome: list[object] = []

        def complete() -> None:
            try:
                outcome.append(
                    local_chat_private.complete_cloudflare_oauth_callback(
                        self.subject, state=state, claim=CLAIM, session_binding=SESSION
                    )
                )
            except local_app.ApiProblem as exc:
                outcome.append(exc)

        completion = threading.Thread(target=complete)
        completion.start()
        self.assertTrue(self.broker.exchanging.wait(5))
        uninstall = threading.Thread(target=self._uninstall)
        # The completion holds the Team lock while it exchanges, so the uninstall must wait for the seal.
        held = not self.lock.acquire(blocking=False)
        if held:
            uninstall.start()
        else:
            self.lock.release()
            uninstall.run()
        self.broker.finish.set()
        completion.join(5)
        if held:
            uninstall.join(5)

        self.assertFalse(self.installed)
        self.assertEqual(self._status(), "missing", "the completion restored a credential the uninstall deleted")
        self.assertTrue(held, "the Team lock was released during the broker exchange")
        self.assertEqual(outcome[0]["connected"], True)

    def test_an_uninstall_that_finished_first_refuses_the_exchange(self) -> None:
        state = self._state()
        self._uninstall()

        with self.assertRaises(local_app.ApiProblem) as refused:
            local_chat_private.complete_cloudflare_oauth_callback(
                self.subject, state=state, claim=CLAIM, session_binding=SESSION
            )

        self.assertEqual(refused.exception.code, "assistant-integration-oauth-unavailable")
        self.assertFalse(self.broker.exchanging.is_set())
        self.assertEqual(self._status(), "missing")

    def test_an_unknown_callback_is_refused_before_any_exchange(self) -> None:
        with self.assertRaises(local_app.ApiProblem) as refused:
            local_chat_private.complete_cloudflare_oauth_callback(
                self.subject, state="s" * 43, claim=CLAIM, session_binding=SESSION
            )

        self.assertEqual(refused.exception.code, "assistant-integration-oauth-unavailable")
        self.assertFalse(self.broker.exchanging.is_set())

    def test_a_declaration_for_another_team_is_refused_under_the_locked_team(self) -> None:
        state = self._state()
        refusals: list[BaseException] = []

        def complete(_state, _claim, _session_binding, resolver):
            try:
                resolver("team_2", ASSISTANT, "cloudflare")
            except integration_service.OAuthIntegrationDeclarationError as exc:
                refusals.append(exc)
            raise integration_service.OAuthIntegrationServiceError("declaration unavailable")

        self.subject.oauth_service = types.SimpleNamespace(complete=complete)

        with self.assertRaises(local_app.ApiProblem):
            local_chat_private.complete_cloudflare_oauth_callback(
                self.subject, state=state, claim=CLAIM, session_binding=SESSION
            )

        self.assertEqual(len(refusals), 1)


if __name__ == "__main__":
    unittest.main()
