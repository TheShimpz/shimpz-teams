from __future__ import annotations

import json
import sys
import tempfile
import threading
import types
import unittest
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from assistant import spec as assistant_registry
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from integrations import challenges as integration_challenges
from integrations import flow as integration_flow
from integrations import http as integration_http
from integrations import pkce as integration_pkce
from integrations import store as integration_store

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import hosted_assistant_fixture as harness

app = harness.app
hosted_chat_api = harness.hosted_chat_api
hosted_assistants = harness.hosted_assistants
action_human = hosted_assistants.action_human
assistant_lifecycle = harness.assistant_lifecycle
hosted_chat_segment = harness.hosted_chat_segment
hosted_lifecycle = harness.hosted_lifecycle
hosted_resources = harness.hosted_resources
runtime_state = harness.runtime_state

TEAM_ID = "team_1"
ASSISTANT_ID = "shimpz-cloudflare"
SCOPES = ("dns.read", "zone.read")
ACCESS_TOKEN = "-".join(("hosted", "access", "token", "value", "123456789"))
ANCHOR_ID = "a" * 64
ZONE_INPUT = {"page": 1, "per_page": 25}


def _zones(name: str = "example.com") -> dict[str, object]:
    return {
        "zones": [
            {
                "id": "a" * 32,
                "name": name,
                "status": "active",
                "type": "full",
                "paused": False,
                "integration": {"id": "b" * 32, "name": "Shimpz"},
            }
        ],
        "pagination": {"page": 1, "per_page": 25, "count": 1, "total_count": 1, "total_pages": 1},
    }


class HostedOAuthIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.store = integration_store.OAuthIntegrationStore(
            root / "state" / "integrations.json",
            root / "key" / "aes256.key",
        )
        trusted = harness.HOSTED_SPEC.contract
        self.contract = replace(
            trusted,
            actions={
                action_id: replace(
                    action,
                    integrations=("cloudflare",) if action_id == "list-zones" else (),
                )
                for action_id, action in trusted.actions.items()
            },
            integrations={"cloudflare": assistant_registry.IntegrationSpec("cloudflare", SCOPES)},
        )
        self.container = types.SimpleNamespace(id="b" * 64)
        self.active = hosted_assistants._ActiveAssistant(
            ASSISTANT_ID,
            self.contract,
            self.container,
            harness.HOSTED_SPEC.image,
            harness.HOSTED_SPEC.version,
            harness.HOSTED_SPEC.summary,
        )

    def _connect(self) -> None:
        self.store.put(
            TEAM_ID,
            ASSISTANT_ID,
            "cloudflare",
            "cloudflare",
            SCOPES,
            integration_http.OAuthTokenSet(ACCESS_TOKEN, "refresh-token-value-123456789", SCOPES, 3600),
        )

    def test_refresh_uses_the_configured_hosted_oauth_client(self) -> None:
        token_set = integration_http.OAuthTokenSet(ACCESS_TOKEN, "new-refresh-token", SCOPES, 3600)
        oauth_http = mock.Mock()
        oauth_http.refresh.return_value = token_set
        client_secret = "-".join(("hosted", "client", "secret", "value"))
        refresh_token = "-".join(("old", "refresh", "token", "value"))

        with mock.patch.multiple(
            runtime_state,
            _oauth_http=oauth_http,
            _cloudflare_oauth_client_id="client-id",
            _cloudflare_oauth_client_secret=client_secret,
        ):
            result = hosted_assistants._refresh_oauth_integration("cloudflare", SCOPES, refresh_token, None)

        self.assertIs(result, token_set)
        oauth_http.refresh.assert_called_once_with(
            provider_id="cloudflare",
            client_id="client-id",
            client_secret=client_secret,
            refresh_token=refresh_token,
            scopes=SCOPES,
        )

    def test_inventory_is_status_only_and_private_token_reaches_only_declared_action(self) -> None:
        self._connect()
        captured: list[dict[str, object]] = []
        inspected = []
        inspect_memo: dict[str, dict[str, dict]] = {}
        turn_token = "turn-token"

        def rpc(_team_id, _token, _container, _action_id, payload):
            captured.append(payload)
            return {"type": "result", "result": _zones()}

        def installed(_team_id, _assistant_id, current_inspect_memo=None):
            inspected.append(current_inspect_memo)
            return ASSISTANT_ID, self.contract, self.container

        with (
            mock.patch.object(runtime_state, "_assistant_integrations", self.store),
            mock.patch.multiple(
                harness.hosted_assistants,
                _installed_assistant=installed,
                _assistant_rpc=rpc,
            ),
        ):
            result = hosted_assistants._invoke_assistant_action(
                hosted_assistants.ActionInvocationRequest(
                    team_id=TEAM_ID,
                    token=turn_token,
                    assistant_id=ASSISTANT_ID,
                    contract=self.contract,
                    container=self.container,
                    action="list-zones",
                    payload=ZONE_INPUT,
                    inspect_memo=inspect_memo,
                )
            )
            payload = integration_flow.inventory_payload(
                TEAM_ID,
                [hosted_assistants._hosted_integration_spec(self.active)],
                self.store,
            )

        self.assertEqual(result["result"]["zones"][0]["name"], "example.com")
        self.assertEqual(len(inspected), 1)
        self.assertIs(inspected[0], inspect_memo)
        self.assertEqual(
            captured,
            [
                {
                    "input": ZONE_INPUT,
                    "integrations": {"cloudflare": ACCESS_TOKEN},
                }
            ],
        )
        serialized = json.dumps(payload)
        self.assertNotIn(ACCESS_TOKEN, serialized)
        self.assertNotIn("refresh-token", serialized)
        self.assertNotIn("generation", serialized)
        self.assertEqual(payload["integrations"][0]["status"], "connected")
        self.assertEqual(payload["integrations"][0]["assistant_version"], "0.4.1")
        self.assertEqual(payload["integrations"][0]["assistant_summary"], "Cloudflare test fixture")

    def test_fresh_action_evidence_reaches_only_its_immediate_rpc(self) -> None:
        turn_token = "-".join(("turn", "token"))
        integration_values = {
            "cloudflare": {
                "type": "oauth2-bearer",
                "access_token": ACCESS_TOKEN,
            }
        }
        rpc = mock.Mock(return_value={"type": "result", "result": _zones()})
        with (
            mock.patch.object(runtime_state, "_assistant_integrations", self.store),
            mock.patch.object(hosted_assistants, "_assistant_rpc", rpc),
            mock.patch.object(
                hosted_assistants,
                "_installed_assistant",
                side_effect=AssertionError("validated Assistant must not be inspected again"),
            ),
            mock.patch.object(
                hosted_assistants,
                "_resolve_action_integrations",
                side_effect=AssertionError("fresh integration values must not be decrypted again"),
            ),
        ):
            result = hosted_assistants._invoke_assistant_action(
                hosted_assistants.ActionInvocationRequest(
                    team_id=TEAM_ID,
                    token=turn_token,
                    assistant_id=ASSISTANT_ID,
                    contract=self.contract,
                    container=self.container,
                    action="list-zones",
                    payload=ZONE_INPUT,
                    validated_assistant=self.active,
                    integration_values=integration_values,
                )
            )

        self.assertEqual(result["result"]["zones"][0]["name"], "example.com")
        self.assertEqual(rpc.call_args.args[-1]["integrations"], {"cloudflare": ACCESS_TOKEN})

    def test_hosted_rpc_admits_a_declared_human_request_frame(self) -> None:
        turn_token = "-".join(("turn", "token"))
        request = {
            "kind": "approval",
            "ordinal": 0,
            "title": "Publish zone",
            "description": "Publish this reviewed DNS zone.",
        }
        request["fingerprint"] = action_human._fingerprint(request)
        contract = replace(
            self.contract,
            actions={
                action_id: replace(action, human_requests=("approval",))
                for action_id, action in self.contract.actions.items()
            },
        )
        active = hosted_assistants._ActiveAssistant(
            ASSISTANT_ID,
            contract,
            self.container,
            harness.HOSTED_SPEC.image,
        )

        with (
            mock.patch.object(
                hosted_assistants,
                "_assistant_rpc",
                return_value={"type": "request", "request": request},
            ),
            self.assertRaises(action_human.HumanRequestSuspensionError) as caught,
        ):
            hosted_assistants._invoke_assistant_action(
                hosted_assistants.ActionInvocationRequest(
                    team_id=TEAM_ID,
                    token=turn_token,
                    assistant_id=ASSISTANT_ID,
                    contract=contract,
                    container=self.container,
                    action="list-zones",
                    payload=ZONE_INPUT,
                    validated_assistant=active,
                    integration_values={},
                )
            )

        self.assertEqual(caught.exception.request.kind, "approval")
        self.assertEqual(caught.exception.request.ordinal, 0)

    def test_integration_token_exposure_is_rejected_without_echoing_it(self) -> None:
        self._connect()
        turn_token = "turn-token"
        with (
            mock.patch.object(runtime_state, "_assistant_integrations", self.store),
            mock.patch.multiple(
                harness.hosted_assistants,
                _installed_assistant=lambda *_args: (ASSISTANT_ID, self.contract, self.container),
                _assistant_rpc=lambda *_args, **_kwargs: _zones(ACCESS_TOKEN),
            ),
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            hosted_assistants._invoke_assistant_action(
                hosted_assistants.ActionInvocationRequest(
                    team_id=TEAM_ID,
                    token=turn_token,
                    assistant_id=ASSISTANT_ID,
                    contract=self.contract,
                    container=self.container,
                    action="list-zones",
                    payload=ZONE_INPUT,
                )
            )

        self.assertEqual(caught.exception.status, HTTPStatus.BAD_GATEWAY)
        self.assertNotIn(ACCESS_TOKEN, caught.exception.message)

    def test_admitted_contract_prunes_removed_integrations_and_cancels_paused_turn(self) -> None:
        self._connect()
        challenge_store = integration_challenges.IntegrationChallengeStore()
        requirement = integration_challenges.IntegrationRequirement(
            ASSISTANT_ID,
            "Shimpz Cloudflare",
            ("list-zones",),
            (("cloudflare", "cloudflare", SCOPES),),
        )
        challenge_store.create(TEAM_ID, (requirement,), object())
        without_integrations = replace(
            harness.HOSTED_SPEC,
            contract=replace(self.contract, integrations={}),
        )

        with (
            mock.patch.object(runtime_state, "_assistant_integrations", self.store),
            mock.patch.object(runtime_state, "_integration_challenges", challenge_store),
        ):
            assistant_lifecycle._retain_admitted_assistant_integrations(TEAM_ID, ASSISTANT_ID, without_integrations)

        self.assertIsNone(challenge_store.current(TEAM_ID))
        self.assertEqual(self.store.metadata(TEAM_ID, ASSISTANT_ID, {}), ())
        self.assertNotIn(ACCESS_TOKEN, self.store.state_path.read_text(encoding="utf-8"))

    def test_authorize_and_callback_expose_no_oauth_private_material(self) -> None:
        challenge_store = integration_challenges.IntegrationChallengeStore()
        continuation = chat_orchestrator.ChatContinuation(
            brain_runtime_client.RuntimeTurn("action-required", "", ()),
            (),
            (),
            0,
        )
        pending = hosted_assistants._PendingHostedChat(
            continuation,
            (ASSISTANT_ID,),
            (),
            "integration_1",
            ("identity",),
        )
        challenge = challenge_store.create(
            TEAM_ID,
            (
                integration_challenges.IntegrationRequirement(
                    ASSISTANT_ID,
                    "Shimpz Cloudflare",
                    ("list-zones",),
                    (("cloudflare", "cloudflare", SCOPES),),
                ),
            ),
            pending,
        )
        fake_service = types.SimpleNamespace(
            authorization_url=lambda current, session, *, resource_binding: (
                "https://x.com/i/oauth2/authorize?state=opaque"
                if current is challenge
                and session == "browser-session-binding-value"
                and resource_binding == ("integration_1", ANCHOR_ID)
                else None
            ),
            complete=lambda state, code, session, resolver: types.SimpleNamespace(
                team_id=TEAM_ID,
                assistant_id=ASSISTANT_ID,
                integration_id="cloudflare",
                provider="cloudflare",
                scopes=SCOPES,
                generation=9,
                resource_binding=("integration_1", ANCHOR_ID),
            ),
            disconnect=lambda *_args: True,
        )
        callback_binding = integration_pkce.OAuthCallbackBinding(
            TEAM_ID,
            ASSISTANT_ID,
            "cloudflare",
            ("integration_1", ANCHOR_ID),
        )
        fake_pkce = types.SimpleNamespace(inspect_callback=lambda **_kwargs: callback_binding)
        lease = hosted_resources._AuthorizationLease(
            TEAM_ID,
            ANCHOR_ID,
            "integration_1",
            ("integration", "integration_1"),
        )
        with (
            mock.patch.multiple(
                runtime_state,
                _integration_challenges=challenge_store,
                _integration_pkce=fake_pkce,
                _oauth_integrations=fake_service,
            ),
            mock.patch.multiple(
                hosted_resources,
                _cleanup_record=lambda _team_id: None,
                _require_current_authorization=lambda *_args, **_kwargs: object(),
                _authorize=lambda *_args, **_kwargs: lease,
            ),
        ):
            started = hosted_chat_api._start_oauth_integration(
                TEAM_ID,
                challenge.id,
                "browser-session-binding-value",
                lease,
            )
            completed, callback_owner = hosted_chat_api._complete_oauth_integration(
                {
                    "state": "provider-state-value",
                    "code": "provider-code-value",
                    "session_binding": "browser-session-binding-value",
                },
            )
            with self.assertRaises(runtime_state.ApiError) as extra_field:
                hosted_chat_api._complete_oauth_integration(
                    {
                        "state": "provider-state-value",
                        "code": "provider-code-value",
                        "session_binding": "browser-session-binding-value",
                        "redirect": "https://attacker.test",
                    },
                )

        self.assertEqual(started, {"authorization_url": "https://x.com/i/oauth2/authorize?state=opaque"})
        self.assertEqual(callback_owner, "integration_1")
        self.assertEqual(extra_field.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertEqual(
            completed,
            {
                "connected": True,
                "team_id": TEAM_ID,
                "assistant_id": ASSISTANT_ID,
                "integration_id": "cloudflare",
                "provider": "cloudflare",
                "scopes": list(SCOPES),
                "challenge_id": challenge.id,
            },
        )
        serialized = json.dumps({"started": started, "completed": completed})
        for forbidden in (
            "provider-code-value",
            "browser-session-binding-value",
            "access_token",
            "refresh_token",
            "code_verifier",
            "client_id",
            "generation",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_team_teardown_cancels_integration_turn_and_purges_tokens(self) -> None:
        self._connect()
        challenges = integration_challenges.IntegrationChallengeStore()
        pkce = types.SimpleNamespace(cancel_team=mock.Mock(return_value=1))
        challenges.create(
            TEAM_ID,
            (
                integration_challenges.IntegrationRequirement(
                    ASSISTANT_ID,
                    "Shimpz Cloudflare",
                    ("list-zones",),
                    (("cloudflare", "cloudflare", SCOPES),),
                ),
            ),
            object(),
        )
        with (
            mock.patch.object(runtime_state, "_assistant_integrations", self.store),
            mock.patch.object(runtime_state, "_integration_challenges", challenges),
            mock.patch.object(runtime_state, "_integration_pkce", pkce),
        ):
            self.assertTrue(hosted_lifecycle._teardown_assistant_integrations(TEAM_ID))

        pkce.cancel_team.assert_called_once_with(TEAM_ID)
        self.assertIsNone(challenges.current(TEAM_ID))
        self.assertEqual(self.store.metadata(TEAM_ID, ASSISTANT_ID, self.contract.integrations)[0].status, "missing")

    def test_callback_revalidates_owner_and_container_before_token_exchange(self) -> None:
        binding = integration_pkce.OAuthCallbackBinding(
            TEAM_ID,
            ASSISTANT_ID,
            "cloudflare",
            ("a" * 32, ANCHOR_ID),
        )
        complete = mock.Mock()
        service = types.SimpleNamespace(complete=complete)
        body = {"state": "state", "code": "code", "session_binding": "browser-binding"}
        cases = (
            hosted_resources._AuthorizationLease(TEAM_ID, "b" * 64, "a" * 32, ("account", "a" * 32)),
            hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, "b" * 32, ("account", "a" * 32)),
        )
        for lease in cases:
            with (
                self.subTest(lease=lease),
                mock.patch.multiple(
                    runtime_state,
                    _integration_pkce=types.SimpleNamespace(inspect_callback=lambda **_kwargs: binding),
                    _oauth_integrations=service,
                ),
                mock.patch.object(hosted_resources, "_authorize", return_value=lease),
                mock.patch.object(hosted_resources, "_cleanup_record", return_value=None),
                self.assertRaises(runtime_state.ApiError) as caught,
            ):
                hosted_chat_api._complete_oauth_integration(body)
            self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)
        complete.assert_not_called()

    def test_expired_callback_is_a_conflict_without_an_authority_or_exchange_oracle(self) -> None:
        pkce = types.SimpleNamespace(
            inspect_callback=mock.Mock(
                side_effect=hosted_chat_api.integration_pkce.OAuthChallengeNotFoundError("missing")
            )
        )
        with (
            mock.patch.object(runtime_state, "_integration_pkce", pkce),
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            hosted_chat_api._complete_oauth_integration(
                {"state": "state", "code": "code", "session_binding": "browser-binding"}
            )

        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)

    def test_callback_rejects_pending_teardown_before_exchange(self) -> None:
        owner = "a" * 32
        binding = integration_pkce.OAuthCallbackBinding(
            TEAM_ID,
            ASSISTANT_ID,
            "cloudflare",
            (owner, ANCHOR_ID),
        )
        complete = mock.Mock()
        with (
            mock.patch.multiple(
                runtime_state,
                _integration_pkce=types.SimpleNamespace(inspect_callback=lambda **_kwargs: binding),
                _oauth_integrations=types.SimpleNamespace(complete=complete),
            ),
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=object()),
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            hosted_chat_api._complete_oauth_integration(
                {"state": "state", "code": "code", "session_binding": "browser-binding"}
            )

        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)
        complete.assert_not_called()

    def test_callback_holds_the_team_lifecycle_lock_through_exchange_and_store(self) -> None:
        owner = "a" * 32
        binding = integration_pkce.OAuthCallbackBinding(
            TEAM_ID,
            ASSISTANT_ID,
            "cloudflare",
            (owner, ANCHOR_ID),
        )
        entered = threading.Event()
        release = threading.Event()
        result: list[object] = []

        def complete(*_args):
            entered.set()
            release.wait(timeout=2)
            return hosted_chat_api.integration_service.OAuthIntegrationCompletion(
                TEAM_ID,
                ASSISTANT_ID,
                "cloudflare",
                "cloudflare",
                SCOPES,
                1,
                (owner, ANCHOR_ID),
            )

        lease = hosted_resources._AuthorizationLease(
            TEAM_ID,
            ANCHOR_ID,
            owner,
            ("account", owner),
        )
        with (
            mock.patch.multiple(
                runtime_state,
                _integration_pkce=types.SimpleNamespace(inspect_callback=lambda **_kwargs: binding),
                _oauth_integrations=types.SimpleNamespace(complete=complete),
                _integration_challenges=types.SimpleNamespace(current=lambda _team_id: None),
            ),
            mock.patch.object(hosted_resources, "_authorize", return_value=lease),
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=None),
        ):
            thread = threading.Thread(
                target=lambda: result.append(
                    hosted_chat_api._complete_oauth_integration(
                        {"state": "state", "code": "code", "session_binding": "browser-binding"}
                    )
                )
            )
            thread.start()
            self.assertTrue(entered.wait(timeout=1))
            lifecycle_lock = runtime_state._lock_for(TEAM_ID)
            self.assertFalse(lifecycle_lock.acquire(blocking=False))
            release.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0][1], owner)

    def test_callback_compensation_failure_is_audited_and_fails_explicitly(self) -> None:
        owner = "a" * 32
        binding = integration_pkce.OAuthCallbackBinding(
            TEAM_ID,
            ASSISTANT_ID,
            "cloudflare",
            (owner, ANCHOR_ID),
        )
        mismatched = hosted_chat_api.integration_service.OAuthIntegrationCompletion(
            TEAM_ID,
            "other-assistant",
            "cloudflare",
            "cloudflare",
            SCOPES,
            1,
            (owner, ANCHOR_ID),
        )
        service = types.SimpleNamespace(
            complete=lambda *_args: mismatched,
            disconnect=mock.Mock(
                side_effect=hosted_chat_api.integration_service.OAuthIntegrationServiceError("failed")
            ),
        )
        lease = hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, owner, ("account", owner))
        with (
            mock.patch.multiple(
                runtime_state,
                _integration_pkce=types.SimpleNamespace(inspect_callback=lambda **_kwargs: binding),
                _oauth_integrations=service,
            ),
            mock.patch.object(hosted_resources, "_authorize", return_value=lease),
            mock.patch.object(hosted_resources, "_cleanup_record", return_value=None),
            mock.patch.object(hosted_chat_api.audit, "log") as audit_log,
            self.assertRaises(runtime_state.ApiError) as caught,
        ):
            hosted_chat_api._complete_oauth_integration(
                {"state": "state", "code": "code", "session_binding": "browser-binding"}
            )

        self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        service.disconnect.assert_called_once_with(TEAM_ID, "other-assistant", "cloudflare")
        self.assertEqual(audit_log.call_args.kwargs["result"], "error")
        self.assertEqual(audit_log.call_args.kwargs["principal_class"], "machine")


if __name__ == "__main__":
    unittest.main()
