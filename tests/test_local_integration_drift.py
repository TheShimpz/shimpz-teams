"""A Local turn paused for an OAuth Integration ends exactly once its Team context drifts.

Resuming or messaging a paused turn whose provider or Team context changed answers team-context-changed and ends
that turn: its live challenge, its persisted continuation, and the Team's OAuth state started from it. The next message
then runs a new turn instead of returning the stale Integration gate until Stop or expiry.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LocalContractCase, chat_body
from test_local_turn_lifecycle import PausingRuntime

from inference import config as inference_config
from integrations import broker as integration_broker
from integrations import service as integration_service
from local import app as local_app

CHAT_BODY = chat_body("List zones", assistant_ids=["shimpz-cloudflare"])
OPENAI_KEY = "sk-test-0123456789"
ANTHROPIC_KEY = "sk-ant-test-0123456789"


class LocalIntegrationDriftTests(LocalContractCase):
    def _paused(self, directory: str) -> tuple[local_app.LocalController, dict[str, object]]:
        """A turn paused for the Cloudflare Integration, with the OAuth authorization the Supervisor started for it."""
        controller = self._chat_controller(
            directory, PausingRuntime("a drifted paused turn must not resume the Brain", fresh=True)
        )
        controller.assistant_integrations.delete_assistant("team_1", "shimpz-cloudflare")
        service = controller.chat_turn_service
        paused = service.chat("team_1", dict(CHAT_BODY), "openai", OPENAI_KEY)
        self.assertEqual(paused["status"], "integrations-required")
        service.oauth_service = integration_service.BrokeredOAuthIntegrationService(
            challenge=service.oauth_pkce,
            store=controller.assistant_integrations,
            broker=integration_broker.OAuthBrokerClient(integration_broker.FixedBrokerTransport()),
        )
        started = service.start_assistant_integration_authorization(
            "team_1",
            paused["challenge_id"],
            "shimpz-cloudflare",
            "cloudflare",
            "browser-session-private-123456789",
            "hosted",
        )
        self.assertEqual(set(started), {"authorization_url"})
        return controller, paused

    @staticmethod
    def _configure(controller: local_app.LocalController, provider: str, effort: str | None = None) -> None:
        # A permitted model change while the turn is paused; the turn still names the context it paused with.
        controller.inference_store.save("team_1", inference_config.normalize(provider, None, effort))

    def _assert_ended(self, controller: local_app.LocalController, raised: local_app.ApiProblem) -> None:
        service = controller.chat_turn_service
        self.assertEqual(raised.code, "team-context-changed")
        self.assertIsNone(service.integration_challenges.current("team_1"))
        self.assertIsNone(controller.chat_continuations.current("team_1"), "the drifted continuation stayed durable")
        self.assertEqual(service.oauth_pkce.cancel_team("team_1"), 0, "OAuth state outlived its drifted challenge")
        # Nothing of the ended turn comes back when the Controller restarts.
        restarted = local_app.ChatTurnService(
            local_app.ChatTurnDependencies(
                action_state=controller.action_state,
                integration_challenges=local_app.integration_challenges.IntegrationChallengeStore(),
                human_challenges=local_app.action_challenges.HumanChallengeStore(),
                chat_continuations=local_app.local_chat_continuation_store.EncryptedContinuationStore(
                    controller.chat_continuations.state_path,
                    controller.chat_continuations.key_path,
                ),
            )
        )
        restarted._restore_all_chat_continuations()
        self.assertIsNone(restarted.integration_challenges.current("team_1"))

    def _assert_new_turn(self, controller: local_app.LocalController, paused: dict[str, object], *credential) -> None:
        # The next message runs a new turn with the configured provider instead of returning the stale gate.
        fresh = controller.chat_turn_service.chat("team_1", dict(CHAT_BODY), *credential)
        self.assertEqual(fresh["status"], "integrations-required")
        self.assertNotEqual(fresh["challenge_id"], paused["challenge_id"])
        self.assertEqual(controller.chat_continuations.current("team_1").challenge_id, fresh["challenge_id"])

    def _resume(self, controller: local_app.LocalController, paused: dict[str, object], *credential) -> None:
        with self.assertRaises(local_app.ApiProblem) as raised:
            controller.chat_turn_service.resume_chat_integrations(
                "team_1", {"challenge_id": paused["challenge_id"]}, *credential
            )
        self._assert_ended(controller, raised.exception)

    def test_resuming_with_the_changed_provider_ends_the_paused_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused = self._paused(directory)
            self._configure(controller, "anthropic")

            self._resume(controller, paused, "anthropic", ANTHROPIC_KEY)

            self._assert_new_turn(controller, paused, "anthropic", ANTHROPIC_KEY)

    def test_resuming_with_the_paused_provider_after_a_change_ends_the_paused_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused = self._paused(directory)
            self._configure(controller, "anthropic")

            self._resume(controller, paused, "openai", OPENAI_KEY)

            self._assert_new_turn(controller, paused, "anthropic", ANTHROPIC_KEY)

    def test_resuming_after_a_model_change_ends_the_paused_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused = self._paused(directory)
            self._configure(controller, "openai", "high")

            self._resume(controller, paused, "openai", OPENAI_KEY)

            self._assert_new_turn(controller, paused, "openai", OPENAI_KEY)

    def test_a_fresh_message_after_a_provider_change_ends_the_paused_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused = self._paused(directory)
            self._configure(controller, "anthropic")

            with self.assertRaises(local_app.ApiProblem) as raised:
                controller.chat_turn_service.chat("team_1", dict(CHAT_BODY), "anthropic", ANTHROPIC_KEY)

            self._assert_ended(controller, raised.exception)
            self._assert_new_turn(controller, paused, "anthropic", ANTHROPIC_KEY)

    def test_a_fresh_message_after_a_model_change_ends_the_paused_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused = self._paused(directory)
            self._configure(controller, "openai", "high")

            with self.assertRaises(local_app.ApiProblem) as raised:
                controller.chat_turn_service.chat("team_1", dict(CHAT_BODY), "openai", OPENAI_KEY)

            self._assert_ended(controller, raised.exception)
            self._assert_new_turn(controller, paused, "openai", OPENAI_KEY)

    def test_a_failed_continuation_deletion_still_cancels_the_oauth_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused = self._paused(directory)
            service = controller.chat_turn_service
            self._configure(controller, "anthropic")
            unavailable = local_app.local_chat_continuation_store.ContinuationStoreError("state is unavailable")

            with (
                mock.patch.object(controller.chat_continuations, "delete", side_effect=unavailable),
                self.assertRaises(local_app.ApiProblem) as raised,
            ):
                service.resume_chat_integrations(
                    "team_1", {"challenge_id": paused["challenge_id"]}, "anthropic", ANTHROPIC_KEY
                )

            # The failed cleanup is reported, never claimed complete, and the ended turn's authorization is gone.
            self.assertEqual(raised.exception.code, "chat-state-unavailable")
            self.assertIsNone(service.integration_challenges.current("team_1"))
            self.assertEqual(service.oauth_pkce.cancel_team("team_1"), 0, "OAuth state outlived its drifted challenge")

    def test_a_fresh_message_in_an_unchanged_context_returns_the_paused_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused = self._paused(directory)

            pending = controller.chat_turn_service.chat("team_1", dict(CHAT_BODY), "openai", OPENAI_KEY)

            self.assertEqual(pending["status"], "integrations-required")
            self.assertEqual(pending["challenge_id"], paused["challenge_id"])
            self.assertEqual(controller.chat_continuations.current("team_1").challenge_id, paused["challenge_id"])

    def test_an_unreadable_provider_keeps_the_paused_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, paused = self._paused(directory)
            service = controller.chat_turn_service
            unreadable = local_app.inference_config.InferenceConfigError("inference state is unreadable")

            for attempt in (
                lambda: service.chat("team_1", dict(CHAT_BODY), "openai", OPENAI_KEY),
                lambda: service.resume_chat_integrations(
                    "team_1", {"challenge_id": paused["challenge_id"]}, "openai", OPENAI_KEY
                ),
            ):
                with (
                    self.subTest(attempt=attempt),
                    mock.patch.object(controller.inference_store, "load", side_effect=unreadable),
                    self.assertRaises(local_app.ApiProblem) as raised,
                ):
                    attempt()
                self.assertEqual(raised.exception.code, "inference-not-configured")

            self.assertEqual(service.integration_challenges.current("team_1").id, paused["challenge_id"])
            self.assertEqual(controller.chat_continuations.current("team_1").challenge_id, paused["challenge_id"])
            self.assertEqual(service.oauth_pkce.cancel_team("team_1"), 1)
