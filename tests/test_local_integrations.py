from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from local_assistant_fixture import assistant_spec
from local_controller_harness import CURRENT_ASSISTANT_IMAGE, LocalContractCase, TestPublicationRegistry

from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from inference import client as brain_runtime_client
from inference import config as inference_config
from integrations import broker as integration_broker
from integrations import challenges as integration_challenges
from integrations import service as integration_service
from integrations import store as integration_store
from local import app as local_app
from local.chat.segment import SegmentRequest
from local.chat.types import ActiveAssistant, PendingLocalChat
from local.install.runtime import AssistantSpec

TEST_ACCESS_TOKEN = "oauth-access-test-token-123456789"
TEST_REFRESH_TOKEN = "oauth-refresh-test-token-123456789"


class LocalOAuthArtifactCurrencyTests(LocalContractCase):
    def test_callback_requires_the_target_assistant_to_run_its_current_artifact(self) -> None:
        controller, container, inspections = self._lifecycle_controller()
        spec = controller.registry["shimpz-cloudflare"]
        declaration = SimpleNamespace(
            provider="cloudflare",
            scopes=("dns.read", "offline_access", "zone.read"),
        )
        spec.integrations = {"cloudflare": declaration}

        class Service:
            @staticmethod
            def complete(_state, _claim, _session_binding, resolver):
                try:
                    current = resolver("team_1", spec.assistant_id, "cloudflare")
                except integration_service.OAuthIntegrationDeclarationError as exc:
                    raise integration_service.OAuthIntegrationServiceError(
                        "OAuth integration declaration is unavailable"
                    ) from exc
                if current is not declaration:
                    raise AssertionError("callback resolved an unexpected declaration")
                return integration_service.OAuthIntegrationCompletion(
                    "team_1",
                    spec.assistant_id,
                    "cloudflare",
                    declaration.provider,
                    declaration.scopes,
                    1,
                    None,
                )

        controller.oauth_service = Service()
        controller.chat_turn_service.oauth_service = controller.oauth_service
        callback = {
            "state": "s" * 43,
            "claim": "a" * 64,
            "session_binding": "browser-session-private-123456789",
        }

        with self.assertRaises(local_app.ApiProblem) as outdated:
            controller.chat_turn_service.complete_cloudflare_oauth_callback(**callback)
        self.assertEqual(
            (outdated.exception.status, outdated.exception.code),
            (HTTPStatus.BAD_GATEWAY, "assistant-integration-oauth-unavailable"),
        )

        config = container.attrs["Config"]
        config["Image"] = CURRENT_ASSISTANT_IMAGE
        config["Labels"][local_app.IMAGE_LABEL] = CURRENT_ASSISTANT_IMAGE
        self.assertEqual(
            controller.chat_turn_service.complete_cloudflare_oauth_callback(**callback),
            {
                "connected": True,
                "team_id": "team_1",
                "assistant_id": spec.assistant_id,
                "integration_id": "cloudflare",
            },
        )
        self.assertEqual(inspections, ["reload", "reload"])


class LocalOAuthIntegrationTests(unittest.TestCase):
    @staticmethod
    def _registry() -> dict[str, AssistantSpec]:
        image = "example.invalid/cloudflare@sha256:" + ("b" * 64)
        return TestPublicationRegistry({"shimpz-cloudflare": assistant_spec(image)})

    def test_controller_accepts_injected_integration_state(self) -> None:
        injected_store = SimpleNamespace()
        injected_challenges = integration_challenges.IntegrationChallengeStore()
        controller = local_app.LocalController(
            SimpleNamespace(
                info=lambda: {"SecurityOptions": ["name=seccomp"], "NCPU": 2},
                networks=SimpleNamespace(list=lambda **_kwargs: []),
            ),
            "local-space",
            self._registry(),
            SimpleNamespace(),
            local_app.LocalControllerDependencies(
                inference_store=SimpleNamespace(),
                brain_runtime=SimpleNamespace(),
                power_state=SimpleNamespace(),
                assistant_integrations=injected_store,
                integration_challenges=injected_challenges,
                oauth_service=SimpleNamespace(),
                developers=SimpleNamespace(),
                artifact_trust=SimpleNamespace(),
            ),
        )

        self.assertIs(controller.assistant_integrations, injected_store)
        self.assertIs(controller.integration_challenges, injected_challenges)

    def test_team_integration_teardown_prevents_same_id_resurrection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller.assistant_integrations = integration_store.OAuthIntegrationStore(
                Path(directory) / "state" / "integrations.json",
                Path(directory) / "key" / "aes256.key",
            )
            controller.assistant_integrations.put(
                "team_1",
                "shimpz-cloudflare",
                "cloudflare",
                "cloudflare",
                ("zone.read",),
                SimpleNamespace(
                    access_token=TEST_ACCESS_TOKEN,
                    refresh_token=TEST_REFRESH_TOKEN,
                    scopes=("zone.read",),
                    expires_in=3600,
                ),
            )
            controller._wire_collaborators()

            controller.chat_turn_service._delete_team_integration_state("team_1")
            recreated = controller.assistant_integrations.metadata(
                "team_1",
                "shimpz-cloudflare",
                {"cloudflare": {"provider": "cloudflare", "scopes": ("zone.read",)}},
            )

        self.assertEqual(recreated[0].status, "missing")

    def test_integration_inventory_is_exact_and_never_contains_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller._locks = tuple(threading.RLock() for _ in range(64))
            controller.registry = self._registry()
            controller.assistant_integrations = integration_store.OAuthIntegrationStore(
                Path(directory) / "state" / "integrations.json",
                Path(directory) / "key" / "aes256.key",
            )
            controller._wire_collaborators()
            controller.assistant_lifecycle._assistant_ids = lambda _team: ("shimpz-cloudflare",)

            payload = controller.chat_turn_service.list_assistant_integrations("team_1")

        self.assertEqual(set(payload), {"team_id", "integrations"})
        self.assertEqual(payload["team_id"], "team_1")
        self.assertEqual(
            payload["integrations"],
            [
                {
                    "assistant_id": "shimpz-cloudflare",
                    "assistant_name": "Shimpz Cloudflare",
                    "id": "cloudflare",
                    "provider": "cloudflare",
                    "name": "Cloudflare",
                    "summary": (
                        "Connect your Cloudflare integration so this Assistant can use only "
                        "its reviewed read permissions."
                    ),
                    "scopes": ["dns.read", "offline_access", "zone.read"],
                    "status": "missing",
                    "integration": None,
                    "expires_at": None,
                }
            ],
        )
        encoded = repr(payload)
        self.assertNotIn("access_token", encoded)
        self.assertNotIn("refresh_token", encoded)
        self.assertNotIn("generation", encoded)

    def test_integration_inventory_route_has_one_exact_internal_shape(self) -> None:
        expected = {"team_id": "team_1", "integrations": []}
        handler = object.__new__(local_app.Handler)
        handler.command = "GET"
        handler.server = SimpleNamespace(
            controller=SimpleNamespace(
                chat_turn_service=SimpleNamespace(list_assistant_integrations=lambda team_id: expected)
            )
        )

        route = handler._assistant_integration_route(["v1", "teams", "team_1", "assistant-integrations"])

        self.assertEqual(
            route,
            (HTTPStatus.OK, expected, "assistant-integration-list", "team_1", None),
        )

    def test_local_controller_builds_only_the_hosted_broker_boundary(self) -> None:
        transport = SimpleNamespace()
        broker = SimpleNamespace()
        service = SimpleNamespace()
        pkce = SimpleNamespace()
        integrations = SimpleNamespace()

        with (
            mock.patch.dict(
                os.environ,
                {
                    "SHIMPZ_OAUTH_BROKER_PROXY_HOST": "oauth-broker-proxy",
                    "SHIMPZ_OAUTH_BROKER_PROXY_CAPABILITY_FILE": "/run/shimpz-account-egress/token",
                    "SHIMPZ_OAUTH_CALLBACK_MODE": "loopback",
                },
            ),
            mock.patch.object(integration_broker, "FixedBrokerTransport", return_value=transport) as transport_type,
            mock.patch.object(integration_broker, "OAuthBrokerClient", return_value=broker) as broker_type,
            mock.patch.object(
                integration_service,
                "BrokeredOAuthIntegrationService",
                return_value=service,
            ) as service_type,
        ):
            controller = local_app.LocalController(
                SimpleNamespace(
                    info=lambda: {"SecurityOptions": ["name=seccomp"], "NCPU": 2},
                    networks=SimpleNamespace(list=lambda **_kwargs: []),
                ),
                "local-space",
                self._registry(),
                SimpleNamespace(),
                local_app.LocalControllerDependencies(
                    inference_store=SimpleNamespace(),
                    brain_runtime=SimpleNamespace(),
                    power_state=SimpleNamespace(),
                    assistant_integrations=integrations,
                    integration_challenges=SimpleNamespace(),
                    oauth_pkce=pkce,
                    developers=SimpleNamespace(),
                    artifact_trust=SimpleNamespace(),
                ),
            )

        transport_type.assert_called_once_with(
            proxy_host="oauth-broker-proxy",
            proxy_capability_file="/run/shimpz-account-egress/token",
        )
        broker_type.assert_called_once_with(transport=transport, callback_mode="loopback")
        service_type.assert_called_once_with(challenge=pkce, store=integrations, broker=broker)
        self.assertIs(controller.oauth_broker, broker)
        self.assertIs(controller.oauth_service, service)

    def test_authorization_and_callback_delegate_to_one_brokered_service(self) -> None:
        requirement = integration_challenges.IntegrationRequirement(
            assistant_id="shimpz-cloudflare",
            assistant_name="Shimpz Cloudflare",
            power_ids=("list-zones",),
            integrations=(("cloudflare", "cloudflare", ("dns.read", "offline_access", "zone.read")),),
        )
        challenges = integration_challenges.IntegrationChallengeStore()
        pending = challenges.create("team_1", (requirement,), {"private": "continuation"})
        calls: list[tuple[str, object]] = []

        class Service:
            def authorization_url(self, challenge, session_binding):
                calls.append(("start", (challenge, session_binding)))
                return (
                    "https://shimpz.com/api/oauth/cloudflare/start?state="
                    + "s" * 43
                    + "&code_challenge="
                    + "c" * 43
                    + "&scope=dns.read+offline_access+zone.read"
                )

            def complete(self, state, claim, session_binding, resolver):
                calls.append(("complete", (state, claim, session_binding, resolver)))
                return integration_service.OAuthIntegrationCompletion(
                    "team_1",
                    "shimpz-cloudflare",
                    "cloudflare",
                    "cloudflare",
                    ("dns.read", "offline_access", "zone.read"),
                    1,
                    None,
                )

        controller = object.__new__(local_app.LocalController)
        controller.integration_challenges = challenges
        controller.oauth_service = Service()
        controller._wire_collaborators()
        controller.chat_turn_service._current_integration_declaration = lambda *_args: None

        started = controller.chat_turn_service.start_assistant_integration_authorization(
            "team_1",
            pending.id,
            "browser-session-private-123456789",
        )
        completed = controller.chat_turn_service.complete_cloudflare_oauth_callback(
            state="s" * 43,
            claim="a" * 64,
            session_binding="browser-session-private-123456789",
        )

        self.assertEqual(set(started), {"authorization_url"})
        self.assertEqual(
            completed,
            {
                "connected": True,
                "team_id": "team_1",
                "assistant_id": "shimpz-cloudflare",
                "integration_id": "cloudflare",
            },
        )
        self.assertEqual([call[0] for call in calls], ["start", "complete"])

    def test_internal_oauth_routes_are_closed_and_exact(self) -> None:
        chat_turn_service = SimpleNamespace(
            start_assistant_integration_authorization=lambda team, challenge, binding: {
                "authorization_url": f"https://shimpz.com/{team}/{challenge}/{binding}"
            },
            complete_cloudflare_oauth_callback=lambda **_values: {
                "connected": True,
                "team_id": "team_1",
                "assistant_id": "shimpz-cloudflare",
                "integration_id": "cloudflare",
            },
            disconnect_assistant_integration=lambda *_values: {"disconnected": True},
        )
        handler = object.__new__(local_app.Handler)
        handler.server = SimpleNamespace(controller=SimpleNamespace(chat_turn_service=chat_turn_service))
        handler._body = lambda **_kwargs: {"session_binding": "browser-session-private-123456789"}
        handler.command = "POST"

        authorize = handler._assistant_integration_route(
            [
                "v1",
                "teams",
                "team_1",
                "assistant-integrations",
                "challenges",
                "a" * 32,
                "authorize",
            ]
        )
        self.assertEqual(authorize[0], HTTPStatus.OK)
        self.assertEqual(authorize[2], "assistant-integration-authorize")

        handler._body = lambda **_kwargs: {
            "state": "s" * 43,
            "claim": "a" * 64,
            "session_binding": "browser-session-private-123456789",
        }
        callback = handler._fixed_route(["v1", "oauth", "cloudflare", "callback"])
        self.assertEqual(callback[0], HTTPStatus.OK)
        self.assertEqual(callback[2], "assistant-integration-complete")

        handler.command = "DELETE"
        disconnected = handler._assistant_integration_route(
            [
                "v1",
                "teams",
                "team_1",
                "assistant-integrations",
                "shimpz-cloudflare",
                "cloudflare",
            ]
        )
        self.assertEqual(disconnected[1], {"disconnected": True})
        self.assertEqual(disconnected[2], "assistant-integration-disconnect")

    def test_chat_pauses_before_any_power_when_integration_is_missing(self) -> None:
        spec = self._registry()["shimpz-cloudflare"]
        request = brain_runtime_client.PowerRequest(
            interrupt_id="call-1",
            assistant_id=spec.assistant_id,
            power="list-zones",
            input={"page": 1, "per_page": 25},
        )

        class Runtime:
            def start(self, _context, _message):
                return brain_runtime_client.RuntimeTurn("power-required", "", (request,))

            def resume(self, _context, _results):  # pragma: no cover - must stay unreachable
                raise AssertionError("Power batch must not execute before OAuth consent")

        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller.space_id = "local-space"
            controller.brain_runtime = Runtime()
            controller.power_state = SimpleNamespace()
            controller.storage = SimpleNamespace(
                metadata_connection=lambda _team_id, _files: contextlib.nullcontext(None),
            )
            controller.assistant_integrations = integration_store.OAuthIntegrationStore(
                Path(directory) / "state" / "integrations.json",
                Path(directory) / "key" / "aes256.key",
            )
            controller._wire_collaborators()
            active = ActiveAssistant(spec, "b" * 64)
            setup = (
                "Team One",
                "c" * 64,
                (active,),
                [],
                inference_config.InferenceConfig("openai", "gpt-5-nano"),
            )
            controller.chat_turn_service._chat_setup = lambda *_args: setup
            controller.assistant_lifecycle._active_assistant_genesis = lambda _active: "Use reviewed Powers only."
            controller.chat_turn_service._chat_cancelled = lambda _token: False
            controller.chat_turn_service._invoke_chat_power = lambda *_args: (_ for _ in ()).throw(
                AssertionError("Power must not execute before OAuth consent")
            )
            turn_token = "turn-token"

            result = controller.chat_turn_service._run_chat_segment(
                SegmentRequest(
                    team_id="team_1",
                    file_ids=[],
                    assistant_ids=(spec.assistant_id,),
                    provider="openai",
                    api_key="test-api-key",
                    token=turn_token,
                    message="List my Cloudflare zones",
                )
            )

        self.assertIsInstance(result.outcome, chat_orchestrator.ChatSuspension)
        self.assertEqual(len(result.integrations), 1)
        self.assertEqual(result.integrations[0].integrations[0][0], "cloudflare")

    def test_integration_resume_is_one_use_and_returns_completed_turn(self) -> None:
        registry = self._registry()
        spec = registry["shimpz-cloudflare"]
        request = brain_runtime_client.PowerRequest(
            interrupt_id="call-1",
            assistant_id=spec.assistant_id,
            power="list-zones",
            input={"page": 1, "per_page": 25},
        )
        continuation = chat_orchestrator.ChatContinuation(
            turn=brain_runtime_client.RuntimeTurn("power-required", "", (request,)),
            seen_interrupts=(),
            invoked=(),
            round_index=0,
        )
        requirements = (
            integration_challenges.IntegrationRequirement(
                assistant_id=spec.assistant_id,
                assistant_name=spec.name,
                power_ids=("list-zones",),
                integrations=(("cloudflare", "cloudflare", spec.integrations["cloudflare"].scopes),),
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            controller = object.__new__(local_app.LocalController)
            controller.registry = registry
            controller._locks = tuple(threading.RLock() for _ in range(64))
            controller.integration_challenges = integration_challenges.IntegrationChallengeStore()
            controller.chat_continuations = SimpleNamespace(delete=lambda *_args: False)
            controller.oauth_pkce = SimpleNamespace(cancel_team=lambda _team: 0)
            controller.assistant_integrations = integration_store.OAuthIntegrationStore(
                Path(directory) / "state" / "integrations.json",
                Path(directory) / "key" / "aes256.key",
            )
            controller._wire_collaborators()
            config = inference_config.InferenceConfig("openai", "gpt-5-nano")
            active = ActiveAssistant(spec, "b" * 64)
            setup = ("Team One", "c" * 64, (active,), [], config)
            identity = controller.chat_turn_service._chat_identity(*setup)
            controller.chat_turn_service._chat_setup = lambda *_args: setup
            pending = PendingLocalChat(
                continuation=continuation,
                assistant_ids=(spec.assistant_id,),
                file_ids=(),
                provider="openai",
                identity=identity,
            )
            challenge = controller.integration_challenges.create("team_1", requirements, pending)
            controller.assistant_integrations.put(
                "team_1",
                spec.assistant_id,
                "cloudflare",
                "cloudflare",
                spec.integrations["cloudflare"].scopes,
                SimpleNamespace(
                    access_token="a" * 32,
                    refresh_token="r" * 32,
                    scopes=spec.integrations["cloudflare"].scopes,
                    expires_in=3600,
                ),
            )
            controller.chat_turn_service._run_chat_segment = lambda *_args, **_kwargs: chat_turn_engine.SegmentResult(
                "Team One",
                identity,
                chat_orchestrator.ChatOutcome("Done", ()),
                (),
            )

            response = controller.chat_turn_service.resume_chat_integrations(
                "team_1",
                {"challenge_id": challenge.id},
                "openai",
                "test-api-key",
            )

            self.assertEqual(response, {"team_id": "team_1", "team_name": "Team One", "reply": "Done"})
            self.assertIsNone(controller.integration_challenges.current("team_1"))
            with self.assertRaises(local_app.ApiProblem) as replay:
                controller.chat_turn_service.resume_chat_integrations(
                    "team_1",
                    {"challenge_id": challenge.id},
                    "openai",
                    "test-api-key",
                )
            self.assertEqual(replay.exception.code, "assistant-integration-challenge-expired")

    def test_chat_integration_routes_are_exact(self) -> None:
        pending = {"team_id": "team_1", "status": "integrations-required"}
        completed = {"team_id": "team_1", "team_name": "Team One", "reply": "Done"}
        chat_turn_service = SimpleNamespace(
            pending_chat_integrations=lambda team_id: pending,
            resume_chat_integrations=lambda team_id, body, provider, api_key: completed,
        )
        handler = object.__new__(local_app.Handler)
        handler.server = SimpleNamespace(controller=SimpleNamespace(chat_turn_service=chat_turn_service))
        handler._model_credential_headers = lambda: ("openai", "test-api-key")
        handler._body = lambda **_kwargs: {"challenge_id": "a" * 32}

        handler.command = "GET"
        self.assertEqual(
            handler._chat_route(["v1", "teams", "team_1", "chat", "integrations"]),
            (HTTPStatus.OK, pending, "chat-integration-pending", "team_1", None),
        )
        handler.command = "POST"
        self.assertEqual(
            handler._chat_route(["v1", "teams", "team_1", "chat", "integrations"]),
            (HTTPStatus.OK, completed, "chat-integration-submit", "team_1", None),
        )


if __name__ == "__main__":
    unittest.main()
