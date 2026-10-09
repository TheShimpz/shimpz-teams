import contextlib
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

from action import journal as action_journal
from assistant import spec as assistant_registry
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from integrations import challenges as integration_challenges
from integrations import flow as integration_flow
from integrations import http as integration_http
from integrations import pkce as integration_pkce
from integrations import store as integration_store
from tests import human_request_fixtures

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import hosted_assistant_fixture as harness
from test_hosted_lock_order import ObservedTeamLock

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
REQUIREMENT = integration_challenges.IntegrationRequirement(
    ASSISTANT_ID, "Shimpz Cloudflare", ("list-zones",), (("cloudflare", "cloudflare", SCOPES),)
)


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


def _evidence(integrations: dict[str, object]) -> object:
    """Fresh evidence for one Action invocation that already holds these private Integration values."""
    return hosted_assistants.action_execution.ActionInvocationEvidence(
        hosted_assistants.action_execution.RpcPrivateInputs(integrations, {}),
        action_human.ActionTranscript("interrupt"),
        "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
    )


def _callback_binding(owner: str) -> integration_pkce.OAuthCallbackBinding:
    """The callback binding of the Assistant's Cloudflare Integration for ``owner`` on the Team anchor."""
    return integration_pkce.OAuthCallbackBinding(TEAM_ID, ASSISTANT_ID, "cloudflare", (owner, ANCHOR_ID))


def _account_paused_pending() -> object:
    """account_1's Hosted turn paused before any Action, waiting for the Assistant's Integration."""
    continuation = chat_orchestrator.ChatContinuation(
        brain_runtime_client.RuntimeTurn("action-required", "", ()), (), (), 0
    )
    return hosted_assistants._PendingHostedChat(
        continuation, (ASSISTANT_ID,), (), "account_1", (ANCHOR_ID, "account_1")
    )


def _callback() -> dict[str, str]:
    """A fresh, well-formed OAuth callback body; each test decides which authority it lacks."""
    return {"state": "state", "code": "code", "session_binding": "browser-binding"}


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

    def _request(self, token: str, **changes: object) -> hosted_assistants.ActionInvocationRequest:
        """The Team's list-zones invocation of its Cloudflare Assistant under this chat token."""
        fields: dict[str, object] = {
            "team_id": TEAM_ID,
            "token": token,
            "assistant_id": ASSISTANT_ID,
            "contract": self.contract,
            "container": self.container,
            "action": "list-zones",
            "payload": ZONE_INPUT,
        }
        return hosted_assistants.ActionInvocationRequest(**(fields | changes))

    def _connect(self) -> None:
        self.store.put(
            TEAM_ID,
            ASSISTANT_ID,
            "cloudflare",
            "cloudflare",
            SCOPES,
            integration_http.OAuthTokenSet(ACCESS_TOKEN, "refresh-token-value-123456789", SCOPES, 3600),
        )

    def test_refresh_requires_reauthorization_without_an_oauth_client(self) -> None:
        # Hosted Team holds no OAuth client secret, so an expired grant is never refreshed in place.
        with self.assertRaises(hosted_assistants.integration_store.OAuthIntegrationReauthorizationError):
            hosted_assistants._refresh_oauth_integration("cloudflare", SCOPES, "old-refresh-token-value", None)

    def test_inventory_is_status_only_and_private_token_reaches_only_declared_action(self) -> None:
        self._connect()
        captured: list[dict[str, object]] = []
        inspected = []
        inspect_memo: dict[str, dict[str, dict]] = {}
        turn_token = "turn-token"

        brokers: list[object] = []

        def rpc(_team_id, _token, _container, _action_id, payload, broker):
            captured.append(payload)
            brokers.append(broker)
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
            result = hosted_assistants._invoke_assistant_action(self._request(turn_token, inspect_memo=inspect_memo))
            payload = integration_flow.inventory_payload(
                TEAM_ID,
                [hosted_assistants._hosted_integration_spec(self.active)],
                self.store,
            )

        self.assertEqual(result["result"]["zones"][0]["name"], "example.com")
        self.assertEqual(len(inspected), 1)
        self.assertIs(inspected[0], inspect_memo)
        # A direct invocation is one fresh logical operation; its id is the protocol's random version 4 UUID.
        self.assertTrue(action_journal.valid_operation_id(captured[0].pop("operation_id")))
        self.assertEqual(
            captured,
            [
                {
                    "input": ZONE_INPUT,
                    "stored_inputs": (),
                    "files": {},
                }
            ],
        )
        # The token never enters the invocation; only Team's broker places it, on the provider's API host (ADR-0106).
        self.assertEqual(
            [(item.host, item.value) for item in brokers[0]._scope.credentials],
            [("api.cloudflare.com", f"Bearer {ACCESS_TOKEN}")],
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
                self._request(turn_token, validated_assistant=self.active, evidence=_evidence(integration_values))
            )

        self.assertEqual(result["result"]["zones"][0]["name"], "example.com")
        self.assertNotIn(ACCESS_TOKEN, repr(rpc.call_args.args[-2]))
        self.assertEqual([item.value for item in rpc.call_args.args[-1]._scope.credentials], [f"Bearer {ACCESS_TOKEN}"])

    def test_hosted_rpc_admits_a_declared_human_request_frame(self) -> None:
        turn_token = "-".join(("turn", "token"))
        request = human_request_fixtures.descriptor(
            "approval", title="Publish zone", description="Publish this reviewed DNS zone."
        )
        contract = replace(
            self.contract,
            actions={
                action_id: replace(action, human_requests=("approval",))
                for action_id, action in self.contract.actions.items()
            },
            machine_contract={
                **self.contract.machine_contract,
                "messages": list(human_request_fixtures.CATALOG.values()),
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
                self._request(turn_token, contract=contract, validated_assistant=active, evidence=_evidence({}))
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
            hosted_assistants._invoke_assistant_action(self._request(turn_token))

        self.assertEqual(caught.exception.status, HTTPStatus.BAD_GATEWAY)
        self.assertNotIn(ACCESS_TOKEN, caught.exception.message)

    def test_admitted_contract_prunes_removed_integrations_and_cancels_paused_turn(self) -> None:
        self._connect()
        challenges, pkce, _paused = self._paused_with_oauth()
        without_integrations = replace(
            harness.HOSTED_SPEC,
            contract=replace(self.contract, integrations={}),
        )

        with (
            mock.patch.object(runtime_state, "_assistant_integrations", self.store),
            mock.patch.multiple(runtime_state, _integration_challenges=challenges, _integration_pkce=pkce),
        ):
            assistant_lifecycle._retain_admitted_assistant_integrations(TEAM_ID, ASSISTANT_ID, without_integrations)

        # The paused turn ends with the OAuth state started for an Integration no longer declared.
        self._assert_ended(challenges, pkce)
        self.assertEqual(self.store.metadata(TEAM_ID, ASSISTANT_ID, {}), ())
        self.assertNotIn(ACCESS_TOKEN, self.store.state_path.read_text(encoding="utf-8"))

    def test_a_drifted_resume_ends_the_paused_integration_turn_with_its_oauth_state(self) -> None:
        challenges, pkce, paused = self._paused_with_oauth()
        lease = hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, "account_1", ("account", "account_1"))

        @contextlib.contextmanager
        def exclusive(*_args):
            yield "token", types.SimpleNamespace(id=ANCHOR_ID)

        # The Team context changed since the pause, so the paused turn's identity no longer matches.
        drifted = ("Marketing", (), [], object(), "key", 1, ("changed",))
        with (
            mock.patch.multiple(runtime_state, _integration_challenges=challenges, _integration_pkce=pkce),
            mock.patch.object(hosted_chat_api, "_exclusive_chat_turn", exclusive),
            mock.patch.object(hosted_chat_segment, "_hosted_chat_setup", return_value=drifted),
            self.assertRaises(runtime_state.ApiError) as resumed,
        ):
            hosted_chat_api._resume_chat_integrations(TEAM_ID, paused.id, lease)

        self.assertEqual(resumed.exception.status, HTTPStatus.CONFLICT)
        self._assert_ended(challenges, pkce)

    def test_a_failed_pause_commit_ends_the_teams_pending_oauth_state(self) -> None:
        challenges, pkce, earlier = self._paused_with_oauth()
        # The earlier gate expired; the OAuth state it started is still pending when the next turn pauses.
        challenges.cancel_team(TEAM_ID)
        continuation = earlier.payload.continuation
        with (
            mock.patch.multiple(
                runtime_state,
                _integration_challenges=challenges,
                _integration_pkce=pkce,
                _commit_chat_terminal=lambda _team_id, _token: False,
            ),
            self.assertRaises(runtime_state.ApiError),
        ):
            hosted_chat_segment._pause_hosted_connection(
                TEAM_ID,
                "token",
                types.SimpleNamespace(continuation=continuation),
                earlier.requirements,
                earlier.payload,
            )

        self._assert_ended(challenges, pkce)

    def test_a_refused_disconnect_still_ends_the_paused_integration_turn_with_its_oauth_state(self) -> None:
        challenges, pkce, _paused = self._paused_with_oauth()
        lease = hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, "account_1", ("account", "account_1"))
        with (
            mock.patch.multiple(
                runtime_state,
                _integration_challenges=challenges,
                _integration_pkce=pkce,
            ),
            mock.patch.object(hosted_resources, "_require_current_authorization"),
            mock.patch.object(hosted_chat_api, "_current_integration_declaration"),
            self.assertRaises(runtime_state.ApiError) as refused,
        ):
            hosted_chat_api._refuse_oauth_disconnect(TEAM_ID, ASSISTANT_ID, "cloudflare", lease)

        self.assertEqual(refused.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self._assert_ended(challenges, pkce)

    def test_authorize_and_callback_are_refused_without_issuing_or_exchanging_oauth_state(self) -> None:
        challenge_store = integration_challenges.IntegrationChallengeStore()
        challenge = challenge_store.create(TEAM_ID, (REQUIREMENT,), _account_paused_pending())
        pkce = integration_pkce.OAuthPKCEChallengeStore()
        lease = hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, "account_1", ("account", "account_1"))
        with (
            mock.patch.multiple(runtime_state, _integration_challenges=challenge_store, _integration_pkce=pkce),
            mock.patch.object(hosted_resources, "_require_current_authorization"),
            self.assertRaises(runtime_state.ApiError) as started,
        ):
            hosted_chat_api._refuse_oauth_start(TEAM_ID, challenge.id, lease)
        self.assertEqual(started.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(pkce.cancel_all(), 0)

        # Even a callback that names live OAuth state for its own Team is refused before any authority or exchange.
        live = types.SimpleNamespace(inspect_callback=lambda **_kwargs: _callback_binding("account_1"))
        with (
            mock.patch.object(runtime_state, "_integration_pkce", live),
            mock.patch.object(hosted_resources, "_authorize") as authorize,
            self.assertRaises(runtime_state.ApiError) as completed,
        ):
            hosted_chat_api._refuse_integration_callback(_callback())
        self.assertEqual(completed.exception.status, HTTPStatus.BAD_GATEWAY)
        authorize.assert_not_called()

        with self.assertRaises(runtime_state.ApiError) as extra_field:
            hosted_chat_api._refuse_integration_callback({**_callback(), "redirect": "https://attacker.test"})
        self.assertEqual(extra_field.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_team_teardown_cancels_integration_turn_and_purges_tokens(self) -> None:
        self._connect()
        challenges = integration_challenges.IntegrationChallengeStore()
        pkce = types.SimpleNamespace(cancel_team=mock.Mock(return_value=1))
        challenges.create(
            TEAM_ID,
            (REQUIREMENT,),
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

    @staticmethod
    def _paused_with_oauth() -> tuple[object, integration_pkce.OAuthPKCEChallengeStore, object]:
        """A real Integration-paused turn whose Owner started connecting the Integration it asked for."""
        # The store the shared resume admission recognizes, whichever module copy this suite loaded.
        challenges = hosted_chat_api.chat_turn_engine.integration_challenges.IntegrationChallengeStore()
        pkce = integration_pkce.OAuthPKCEChallengeStore()
        pending = _account_paused_pending()
        paused = challenges.create(TEAM_ID, (REQUIREMENT,), pending)
        pkce.create(
            session_binding="browser-session-binding-value",
            team_id=TEAM_ID,
            assistant_id=ASSISTANT_ID,
            integration_id="cloudflare",
            provider_id="cloudflare",
            scopes=SCOPES,
            resource_binding=("account_1", ANCHOR_ID),
        )
        return challenges, pkce, paused

    def _assert_ended(self, challenges: object, pkce: integration_pkce.OAuthPKCEChallengeStore) -> None:
        self.assertIsNone(challenges.current(TEAM_ID), "the paused Integration gate outlived its end")
        self.assertEqual(pkce.cancel_team(TEAM_ID), 0, "OAuth state outlived its withdrawn gate")

    def test_a_model_change_ends_the_paused_integration_turn_with_its_oauth_state(self) -> None:
        challenges, pkce, paused = self._paused_with_oauth()
        lease = hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, "account_1", ("account", "account_1"))
        body = {"provider": "anthropic", "model": None, "effort": "low"}

        @contextlib.contextmanager
        def exclusive(*_args):
            yield "token", types.SimpleNamespace(id=ANCHOR_ID)

        # The Owner has no credential for the new provider, so a paused turn could never resolve it on resume.
        missing = runtime_state.ApiError(HTTPStatus.CONFLICT, "configure a model credential")
        with (
            mock.patch.multiple(runtime_state, _integration_challenges=challenges, _integration_pkce=pkce),
            mock.patch.object(runtime_state._inference_store, "save") as save,
            mock.patch.object(hosted_resources, "_require_current_authorization"),
            mock.patch.object(hosted_chat_api, "_exclusive_chat_turn", exclusive),
            mock.patch.object(hosted_chat_segment, "_hosted_chat_setup", side_effect=missing) as setup,
        ):
            hosted_lifecycle._configure_inference(TEAM_ID, body, lease)
            with self.assertRaises(runtime_state.ApiError) as resumed:
                hosted_chat_api._resume_chat_integrations(TEAM_ID, paused.id, lease)

        save.assert_called_once()
        self._assert_ended(challenges, pkce)
        # The resume answers that the request expired, so the next message can attempt a fresh turn.
        self.assertEqual(resumed.exception.status, HTTPStatus.CONFLICT)
        self.assertIn("expired", str(resumed.exception))
        setup.assert_not_called()

    def test_stop_ends_the_paused_integration_turn_with_its_oauth_state(self) -> None:
        challenges, pkce, _paused = self._paused_with_oauth()
        lease = hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, "account_1", ("account", "account_1"))
        running = types.SimpleNamespace(id=ANCHOR_ID, status="running", reload=lambda: None)
        with (
            mock.patch.multiple(runtime_state, _integration_challenges=challenges, _integration_pkce=pkce),
            mock.patch.object(hosted_resources, "_require_current_authorization", return_value=running),
        ):
            stopped = hosted_chat_api._stop_chat(TEAM_ID, lease)

        self.assertTrue(stopped["accepted"])
        self._assert_ended(challenges, pkce)

    def test_uninstall_ends_the_paused_integration_turn_with_its_oauth_state(self) -> None:
        challenges, pkce, _paused = self._paused_with_oauth()
        lease = hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, "account_1", ("account", "account_1"))
        with (
            mock.patch.multiple(runtime_state, _integration_challenges=challenges, _integration_pkce=pkce),
            mock.patch.object(hosted_resources, "_require_current_authorization"),
            mock.patch.object(runtime_state._dynamic_assistants, "get", return_value=None),
            mock.patch.object(runtime_state._dynamic_assistants, "delete"),
            mock.patch.object(runtime_state._assistant_integrations, "delete_assistant"),
            mock.patch.object(
                assistant_lifecycle, "_teardown_assistant", return_value=hosted_resources._CleanupResult(True, True)
            ),
        ):
            assistant_lifecycle._uninstall_assistant(TEAM_ID, ASSISTANT_ID, lease)

        self._assert_ended(challenges, pkce)

    def test_an_oauth_start_is_refused_as_a_conflict_while_a_pause_commits(self) -> None:
        challenges = integration_challenges.IntegrationChallengeStore()
        pkce = integration_pkce.OAuthPKCEChallengeStore()
        pending = _account_paused_pending()
        lease = hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, "account_1", ("account", "account_1"))

        started: list[object] = []

        def stopped_commit(_team_id: str, _token: str) -> bool:
            # Stop cancelled the turn; the Owner starts connecting its published challenge before the commit fails.
            try:
                started.append(hosted_chat_api._refuse_oauth_start(TEAM_ID, challenges.current(TEAM_ID).id, lease))
            except runtime_state.ApiError as error:
                started.append(error)
            return False

        # The executing turn holds the Team chat slot from publishing its challenge until its pause commits.
        slot = runtime_state._chat_lock_for(TEAM_ID)
        self.assertTrue(slot.acquire(blocking=False))
        try:
            with (
                mock.patch.multiple(
                    runtime_state,
                    _integration_challenges=challenges,
                    _integration_pkce=pkce,
                    _commit_chat_terminal=stopped_commit,
                ),
                mock.patch.object(hosted_resources, "_require_current_authorization"),
                self.assertRaises(runtime_state.ApiError),
            ):
                hosted_chat_segment._pause_hosted_connection(
                    TEAM_ID, "token", types.SimpleNamespace(continuation=pending.continuation), (REQUIREMENT,), pending
                )
        finally:
            slot.release()

        [refused] = started
        self.assertIsInstance(refused, runtime_state.ApiError)
        self.assertEqual(refused.status, HTTPStatus.CONFLICT)
        self._assert_ended(challenges, pkce)

    def test_an_oauth_start_never_holds_the_chat_slot_destruction_awaits(self) -> None:
        challenges, pkce, paused = self._paused_with_oauth()
        pkce.cancel_team(TEAM_ID)
        lease = hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, "account_1", ("account", "account_1"))
        team_lock = threading.Lock()
        waiting = threading.Event()

        outcome: list[object] = []

        def start() -> None:
            try:
                outcome.append(hosted_chat_api._refuse_oauth_start(TEAM_ID, paused.id, lease))
            except runtime_state.ApiError as error:
                outcome.append(error)

        slot = runtime_state._chat_lock_for(TEAM_ID)
        starting = threading.Thread(target=start, daemon=True)
        with (
            mock.patch.multiple(runtime_state, _integration_challenges=challenges, _integration_pkce=pkce),
            mock.patch.object(runtime_state, "_lock_for", return_value=ObservedTeamLock(team_lock, waiting)),
            mock.patch.object(hosted_resources, "_require_current_authorization"),
        ):
            # Destruction holds the Team lock and then awaits the chat slot; the start must not be holding it.
            with team_lock:
                starting.start()
                self.assertTrue(waiting.wait(5), "the OAuth start never reached the Team lock")
                acquired = slot.acquire(timeout=5)
            try:
                starting.join(5)
            finally:
                if acquired:
                    slot.release()

        self.assertTrue(acquired, "the OAuth start held the chat slot that destruction awaits")
        self.assertFalse(starting.is_alive())
        [refused] = outcome
        self.assertIsInstance(refused, runtime_state.ApiError)
        self.assertEqual(pkce.cancel_team(TEAM_ID), 0)

    def test_a_runtime_change_ends_the_paused_integration_turn_with_its_oauth_state(self) -> None:
        lease = hosted_resources._AuthorizationLease(TEAM_ID, ANCHOR_ID, "account_1", ("account", "account_1"))
        for op in ("stop", "start", "restart"):
            challenges, pkce, _paused = self._paused_with_oauth()
            runtime = types.SimpleNamespace(id=ANCHOR_ID, status="running", reload=lambda: None)
            with (
                self.subTest(op=op),
                mock.patch.multiple(runtime_state, _integration_challenges=challenges, _integration_pkce=pkce),
                mock.patch.object(hosted_resources, "_require_current_authorization", return_value=runtime),
                mock.patch.object(hosted_resources, "_require_team_runtime"),
                mock.patch.object(hosted_resources, "_fail_stop_team"),
                mock.patch.object(hosted_resources, "_start_team_with_isolation"),
            ):
                self.assertEqual(hosted_lifecycle._lifecycle(TEAM_ID, op, lease)["status"], "ok")
                self._assert_ended(challenges, pkce)

    def test_ending_a_paused_integration_turn_rejects_an_invalid_continuation(self) -> None:
        challenges = integration_challenges.IntegrationChallengeStore()
        batched = hosted_assistants._PendingHostedChat(
            types.SimpleNamespace(), (ASSISTANT_ID,), (), "account_1", ("identity",), paused_batch="batch"
        )
        pkce = types.SimpleNamespace(cancel_team=mock.Mock(return_value=0))
        with mock.patch.multiple(runtime_state, _integration_challenges=challenges, _integration_pkce=pkce):
            self.assertFalse(harness.hosted_chat_lifecycle.cancel_paused_integration(TEAM_ID))
            # An Integration pause precedes its batch, so a continuation holding one, or none at all, is invalid.
            for payload in (object(), batched):
                challenges.create(TEAM_ID, (REQUIREMENT,), payload)
                with self.subTest(payload=payload), self.assertRaises(AssertionError):
                    harness.hosted_chat_lifecycle.cancel_paused_integration(TEAM_ID)
                self.assertIsNone(challenges.current(TEAM_ID))
        pkce.cancel_team.assert_called_once_with(TEAM_ID)

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
            hosted_chat_api._refuse_integration_callback(_callback())

        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)


if __name__ == "__main__":
    unittest.main()
