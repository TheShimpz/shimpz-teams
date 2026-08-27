from __future__ import annotations

import unittest
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest import mock

import hosted_assistant_fixture as harness

api = harness.hosted_chat_api
state = harness.runtime_state
assistants = harness.hosted_assistants
segment = harness.hosted_chat_segment


class HostedChatApiEdgeTests(unittest.TestCase):
    @staticmethod
    def lease(owner: str = "account_1", container_id: str = "container") -> object:
        return SimpleNamespace(owner=owner, container_id=container_id)

    @staticmethod
    def pending(owner: str = "account_1") -> object:
        return assistants._PendingHostedChat(
            SimpleNamespace(turn=SimpleNamespace(actions=())),
            (),
            (),
            owner,
            ("identity",),
            (),
        )

    def test_exclusive_turn_releases_lock_on_admission_failure_and_exit(self) -> None:
        lock = SimpleNamespace(acquire=mock.Mock(return_value=False), release=mock.Mock())
        with (
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            self.assertRaises(state.ApiError),
            api._exclusive_chat_turn("team_1", self.lease()),
        ):
            self.fail("busy lock must not enter")

        lock.acquire.return_value = True
        with (
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            mock.patch.object(
                api.hosted_resources,
                "_require_current_authorization",
                side_effect=state.ApiError(api.HTTPStatus.CONFLICT, "changed"),
            ),
            self.assertRaises(state.ApiError),
            api._exclusive_chat_turn("team_1", self.lease()),
        ):
            self.fail("failed authorization must not enter")

        container = SimpleNamespace(id="container", status="stopped", reload=mock.Mock())
        with (
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            mock.patch.object(api.hosted_resources, "_require_current_authorization", return_value=container),
            self.assertRaises(state.ApiError),
            api._exclusive_chat_turn("team_1", self.lease()),
        ):
            self.fail("stopped Team must not enter")

        container.status = "running"
        with (
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            mock.patch.object(api.hosted_resources, "_require_current_authorization", return_value=container),
            mock.patch.object(api.secrets, "token_hex", return_value="token"),
            api._exclusive_chat_turn("team_1", self.lease()) as (token, current),
        ):
            self.assertEqual(token, "token")
            self.assertIs(current, container)
            state._active_action_container_ids["team_1"] = ("token", "action")
        self.assertNotIn("team_1", state._active_chat_tokens)

    def test_chat_and_pending_paths_avoid_duplicate_execution(self) -> None:
        pending = {"status": "human-required"}
        with mock.patch.object(api, "_pending_hosted_chat", return_value=pending):
            self.assertIs(api._chat("team_1", "hello", (), (), self.lease()), pending)

        @contextmanager
        def exclusive(*_args):
            yield "token", SimpleNamespace(id="container")

        with (
            mock.patch.object(api, "_pending_hosted_chat", side_effect=(None, pending)),
            mock.patch.object(api, "_exclusive_chat_turn", exclusive),
        ):
            self.assertIs(api._chat("team_1", "hello", (), (), self.lease()), pending)

        journal = SimpleNamespace(
            purge_replayable=mock.Mock(side_effect=segment.action_journal.ActionJournalError("failed"))
        )
        with (
            mock.patch.object(api, "_pending_hosted_chat", return_value=None),
            mock.patch.object(api, "_exclusive_chat_turn", exclusive),
            mock.patch.object(state, "_action_execution_journal", return_value=journal),
            self.assertRaises(state.ApiError),
        ):
            api._chat("team_1", "hello", (), (), self.lease())

        with mock.patch.object(api.hosted_chat_human, "pending_chat_human", return_value=pending):
            self.assertIs(api._pending_hosted_chat("team_1"), pending)
        integration = object()
        with (
            mock.patch.object(api.hosted_chat_human, "pending_chat_human", return_value={"status": "none"}),
            mock.patch.object(state._integration_challenges, "current", return_value=integration),
            mock.patch.object(segment, "_hosted_integration_challenge_payload", return_value={"status": "integration"}),
        ):
            self.assertEqual(api._pending_hosted_chat("team_1")["status"], "integration")

    def test_integration_declaration_and_start_errors_are_redacted(self) -> None:
        declaration = object()
        contract = SimpleNamespace(integrations={"cloudflare": declaration})
        with mock.patch.object(assistants, "_installed_assistant", return_value=("assistant", contract, object())):
            self.assertIs(api._current_integration_declaration("team_1", "assistant", "cloudflare"), declaration)
        with (
            mock.patch.object(assistants, "_installed_assistant", return_value=("other", contract, object())),
            self.assertRaises(api.integration_service.OAuthIntegrationDeclarationError),
        ):
            api._current_integration_declaration("team_1", "assistant", "cloudflare")

        with (
            mock.patch.object(api.hosted_resources, "_require_current_authorization"),
            mock.patch.object(
                state._integration_challenges,
                "get",
                side_effect=api.integration_challenges.IntegrationChallengeNotFoundError("missing"),
            ),
            self.assertRaises(state.ApiError),
        ):
            api._start_oauth_integration(
                "team_1", "challenge", "assistant", "cloudflare", "binding", self.lease()
            )

        challenge = SimpleNamespace(payload=object())
        with (
            mock.patch.object(api.hosted_resources, "_require_current_authorization"),
            mock.patch.object(state._integration_challenges, "get", return_value=challenge),
            self.assertRaises(state.ApiError),
        ):
            api._start_oauth_integration(
                "team_1", "challenge", "assistant", "cloudflare", "binding", self.lease()
            )

        challenge.payload = self.pending()
        failures = (
            api.integration_service.OAuthIntegrationUnavailableError("configured"),
            api.integration_service.OAuthIntegrationServiceError("failed"),
        )
        for failure in failures:
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(api.hosted_resources, "_require_current_authorization"),
                mock.patch.object(state._integration_challenges, "get", return_value=challenge),
                mock.patch.object(state._oauth_integrations, "authorization_url", side_effect=failure),
                self.assertRaises(state.ApiError),
            ):
                api._start_oauth_integration(
                    "team_1", "challenge", "assistant", "cloudflare", "binding", self.lease()
                )

    def test_callback_and_compensation_validate_resource_authority(self) -> None:
        body = {"state": "state", "session_binding": "binding"}
        for failure in (
            api.integration_pkce.OAuthChallengeNotFoundError("missing"),
            api.integration_pkce.OAuthChallengeError("invalid"),
        ):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(state._integration_pkce, "inspect_callback", side_effect=failure),
                self.assertRaises(state.ApiError),
            ):
                api._callback_binding(body)

        for resource in (None, ("owner",), ("", "container")):
            binding = SimpleNamespace(resource_binding=resource)
            with (
                self.subTest(resource=resource),
                mock.patch.object(state._integration_pkce, "inspect_callback", return_value=binding),
                self.assertRaises(state.ApiError),
            ):
                api._callback_binding(body)

        completion = SimpleNamespace(team_id="team_1", assistant_id="assistant", integration_id="cloudflare")
        with (
            mock.patch.object(state._oauth_integrations, "disconnect"),
            mock.patch.object(api.audit, "log") as audit,
        ):
            api._compensate_oauth_completion(completion, "account_1")
        self.assertEqual(audit.call_args.kwargs["result"], "ok")

        with (
            mock.patch.object(
                state._oauth_integrations,
                "disconnect",
                side_effect=api.integration_service.OAuthIntegrationServiceError("failed"),
            ),
            mock.patch.object(api.audit, "log") as audit,
            self.assertRaises(state.ApiError),
        ):
            api._compensate_oauth_completion(completion, "account_1")
        self.assertEqual(audit.call_args.kwargs["result"], "error")

    def test_completion_disconnect_and_resume_failures_are_closed(self) -> None:
        binding = SimpleNamespace(team_id="team_1", resource_binding=("account_1", "container"))
        body = {"state": "state", "code": "code", "session_binding": "binding"}
        with (
            mock.patch.object(api, "_callback_binding", return_value=(binding, "account_1", "container")),
            mock.patch.object(state, "_lock_for", return_value=nullcontext()),
            mock.patch.object(api.hosted_resources, "_cleanup_record", return_value=None),
            mock.patch.object(api.hosted_resources, "_authorize", return_value=self.lease()),
            mock.patch.object(
                state._oauth_integrations,
                "complete",
                side_effect=api.integration_service.OAuthIntegrationServiceError("failed"),
            ),
            self.assertRaises(state.ApiError),
        ):
            api._complete_oauth_integration(body)

        completion = SimpleNamespace(
            team_id="other",
            assistant_id="assistant",
            integration_id="cloudflare",
            resource_binding=("account_1", "container"),
        )
        with (
            mock.patch.object(api, "_callback_binding", return_value=(binding, "account_1", "container")),
            mock.patch.object(state, "_lock_for", return_value=nullcontext()),
            mock.patch.object(api.hosted_resources, "_cleanup_record", return_value=None),
            mock.patch.object(api.hosted_resources, "_authorize", return_value=self.lease()),
            mock.patch.object(state._oauth_integrations, "complete", return_value=completion),
            mock.patch.object(api, "_compensate_oauth_completion"),
            self.assertRaises(state.ApiError),
        ):
            api._complete_oauth_integration(body)

        with (
            mock.patch.object(state, "_lock_for", return_value=nullcontext()),
            mock.patch.object(api.hosted_resources, "_require_current_authorization"),
            mock.patch.object(api, "_current_integration_declaration"),
            mock.patch.object(api.hosted_chat_human, "cancel_pending"),
            mock.patch.object(state._integration_challenges, "cancel_team"),
            mock.patch.object(
                state._oauth_integrations,
                "disconnect",
                side_effect=api.integration_service.OAuthIntegrationServiceError("failed"),
            ),
            self.assertRaises(state.ApiError),
        ):
            api._disconnect_oauth_integration.__wrapped__("team_1", "assistant", "cloudflare", self.lease())

        @contextmanager
        def exclusive(*_args):
            yield "token", object()

        def invalid_inspect(strategy):
            strategy.inspect(object())

        with (
            mock.patch.object(api, "_exclusive_chat_turn", exclusive),
            mock.patch.object(api.chat_turn_engine, "admit_integration_resume", side_effect=invalid_inspect),
            self.assertRaises(AssertionError),
        ):
            api._resume_chat_integrations("team_1", "challenge", self.lease())

        pending = self.pending()
        active = SimpleNamespace(assistant_id="assistant")

        def valid_inspect(strategy):
            inspected = strategy.inspect(pending)
            self.assertEqual(inspected.identity, ("current",))
            return SimpleNamespace(response={"inspected": True}, pending=None)

        with (
            mock.patch.object(api, "_exclusive_chat_turn", exclusive),
            mock.patch.object(
                segment,
                "_hosted_chat_setup",
                return_value=("Team", (active,), [], object(), "key", 1, ("current",)),
            ),
            mock.patch.object(assistants, "_integration_bindings", return_value={}),
            mock.patch.object(api.chat_turn_engine, "admit_integration_resume", side_effect=valid_inspect),
        ):
            self.assertEqual(
                api._resume_chat_integrations("team_1", "challenge", self.lease()),
                {"inspected": True},
            )

        admission = SimpleNamespace(response={"pending": True}, pending=None)
        with (
            mock.patch.object(api, "_exclusive_chat_turn", exclusive),
            mock.patch.object(api.chat_turn_engine, "admit_integration_resume", return_value=admission),
        ):
            self.assertEqual(api._resume_chat_integrations("team_1", "challenge", self.lease()), {"pending": True})

        admission = SimpleNamespace(response=None, pending=object())
        with (
            mock.patch.object(api, "_exclusive_chat_turn", exclusive),
            mock.patch.object(api.chat_turn_engine, "admit_integration_resume", return_value=admission),
            self.assertRaises(AssertionError),
        ):
            api._resume_chat_integrations("team_1", "challenge", self.lease())

        admission = SimpleNamespace(response=None, pending=pending)
        resumed_segment = object()
        with (
            mock.patch.object(api, "_exclusive_chat_turn", exclusive),
            mock.patch.object(api.chat_turn_engine, "admit_integration_resume", return_value=admission),
            mock.patch.object(segment, "_run_hosted_chat_segment", return_value=resumed_segment),
            mock.patch.object(segment, "_hosted_segment_response", return_value={"reply": "done"}) as respond,
        ):
            self.assertEqual(api._resume_chat_integrations("team_1", "challenge", self.lease()), {"reply": "done"})
        self.assertIs(respond.call_args.args[0].segment, resumed_segment)

        with mock.patch.object(api.hosted_chat_human, "resume_chat_human", return_value={"reply": "human"}) as resume:
            self.assertEqual(api._resume_chat_human("team_1", {}, None, self.lease()), {"reply": "human"})
        self.assertIs(resume.call_args.args[-1], api._exclusive_chat_turn)

    def test_stop_action_and_chat_cover_absent_changed_and_running_states(self) -> None:
        self.assertFalse(api._stop_active_action("team_1", None))
        with mock.patch.dict(state._active_action_container_ids, {}, clear=True):
            self.assertFalse(api._stop_active_action("team_1", "token"))
        with (
            mock.patch.dict(state._active_action_container_ids, {"team_1": ("token", "container")}, clear=True),
            mock.patch.object(
                state._docker.containers,
                "get",
                side_effect=api.docker.errors.NotFound("missing"),
            ),
        ):
            self.assertTrue(api._stop_active_action("team_1", "token"))
        with (
            mock.patch.dict(state._active_action_container_ids, {"team_1": ("token", "container")}, clear=True),
            mock.patch.object(
                state._docker.containers,
                "get",
                side_effect=api.docker.errors.DockerException("failed"),
            ),
            self.assertRaises(state.ApiError),
        ):
            api._stop_active_action("team_1", "token")

        assistant_container = object()
        with (
            mock.patch.dict(state._active_action_container_ids, {"team_1": ("token", "container")}, clear=True),
            mock.patch.object(state._docker.containers, "get", return_value=assistant_container),
            mock.patch.object(assistants, "_fail_stop_action") as stop,
        ):
            self.assertTrue(api._stop_active_action("team_1", "token"))
        stop.assert_called_once_with("team_1", assistant_container)

        stopped = SimpleNamespace(id="container", status="stopped", reload=mock.Mock())
        with (
            mock.patch.object(state._integration_challenges, "cancel_team", return_value=False),
            mock.patch.object(api.hosted_chat_human, "cancel_pending", return_value=False),
            mock.patch.object(state, "_lock_for", return_value=nullcontext()),
            mock.patch.object(api.hosted_resources, "_require_current_authorization", return_value=stopped),
            self.assertRaises(state.ApiError),
        ):
            api._stop_chat("team_1", self.lease())

        running = SimpleNamespace(id="container", status="running", reload=mock.Mock())
        with (
            mock.patch.object(state._integration_challenges, "cancel_team", return_value=False),
            mock.patch.object(api.hosted_chat_human, "cancel_pending", return_value=False),
            mock.patch.object(state, "_lock_for", return_value=nullcontext()),
            mock.patch.object(api.hosted_resources, "_require_current_authorization", return_value=running),
            mock.patch.dict(state._active_chat_tokens, {"team_1": "token"}, clear=True),
            mock.patch.dict(state._active_chat_container_ids, {"team_1": "other"}, clear=True),
            self.assertRaises(state.ApiError),
        ):
            api._stop_chat("team_1", self.lease())

        with (
            mock.patch.object(state._integration_challenges, "cancel_team", return_value=False),
            mock.patch.object(api.hosted_chat_human, "cancel_pending", return_value=False),
            mock.patch.object(state, "_lock_for", return_value=nullcontext()),
            mock.patch.object(api.hosted_resources, "_require_current_authorization", return_value=running),
            mock.patch.dict(state._active_chat_tokens, {"team_1": "token"}, clear=True),
            mock.patch.dict(state._active_chat_container_ids, {"team_1": "container"}, clear=True),
            mock.patch.object(api, "_stop_active_action", return_value=True),
        ):
            result = api._stop_chat("team_1", self.lease())
        self.assertTrue(result["accepted"])
        self.assertIn("token", state._cancelled_chat_tokens)

        with (
            mock.patch.object(state._integration_challenges, "cancel_team", return_value=False),
            mock.patch.object(api.hosted_chat_human, "cancel_pending", return_value=False),
            mock.patch.object(state, "_lock_for", return_value=nullcontext()),
            mock.patch.object(api.hosted_resources, "_require_current_authorization", return_value=running),
            mock.patch.dict(state._active_chat_tokens, {}, clear=True),
            mock.patch.dict(state._active_chat_container_ids, {}, clear=True),
            mock.patch.object(api, "_stop_active_action", return_value=False),
        ):
            result = api._stop_chat("team_1", self.lease())
        self.assertFalse(result["accepted"])


if __name__ == "__main__":
    unittest.main()
