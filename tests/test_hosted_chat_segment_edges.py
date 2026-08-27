from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import hosted_assistant_fixture as harness

segment = harness.hosted_chat_segment
state = harness.runtime_state
assistants = harness.hosted_assistants


class HostedChatSegmentEdgeTests(unittest.TestCase):
    @staticmethod
    def pending(identity: tuple[object, ...] = ("generation",)) -> object:
        return assistants._PendingHostedChat(SimpleNamespace(), (), (), "account_1", identity, ())

    def test_anchor_setup_and_problem_projection_fail_closed(self) -> None:
        with (
            mock.patch.object(segment.hosted_resources, "_get_container", return_value=None),
            self.assertRaises(state.ApiError),
        ):
            segment._current_team_anchor("team_1", "container", "account_1")

        container = SimpleNamespace(id="other", attrs={}, labels={"team.owner": "account_1"})
        with (
            mock.patch.object(segment.hosted_resources, "_get_container", return_value=container),
            self.assertRaises(state.ApiError),
        ):
            segment._current_team_anchor("team_1", "container", "account_1")

        with (
            mock.patch.object(segment.hosted_resources, "_team_name_from_anchor", return_value="Team"),
            mock.patch.object(assistants, "_active_team_assistants", return_value=()),
            mock.patch.object(assistants, "_select_team_assistants", return_value=()),
            mock.patch.object(assistants, "_chat_file_metadata", return_value=[]),
            mock.patch.object(
                state._inference_store,
                "load",
                side_effect=segment.inference_config.InferenceConfigError("missing"),
            ),
            self.assertRaises(state.ApiError),
        ):
            segment._hosted_chat_setup("team_1", [], (), object(), "account_1")

        failures = (
            ("invalid-continuation", None),
            ("invalid-suspension", None),
            ("context-changed", None),
            ("journal", segment.action_journal.ActionJournalError("failed")),
            ("stopped", segment.chat_orchestrator.ChatStoppedError("stopped")),
            ("orchestration", segment.chat_orchestrator.ChatOrchestrationError("failed")),
            ("brain", segment.brain_runtime_client.BrainRuntimeError("failed")),
        )
        for reason, failure in failures:
            with self.subTest(reason=reason), self.assertRaises(state.ApiError):
                segment._raise_hosted_chat_problem(reason, failure)
        with self.assertRaises(AssertionError):
            segment._raise_hosted_chat_problem("unknown", None)

    def test_private_requirements_current_identity_and_action_require_fresh_state(self) -> None:
        with (
            mock.patch.object(
                segment.integration_flow,
                "requirements_for_batch",
                side_effect=segment.integration_flow.IntegrationFlowError("invalid"),
            ),
            self.assertRaises(state.ApiError),
        ):
            segment._hosted_private_requirements("team_1", {}, ())

        request = segment.HostedChatSegmentRequest("team_1", (), (), "token", object(), "account_1")
        validation = segment.HostedValidationContext({}, object(), object(), {})
        with self.assertRaises(AssertionError):
            segment._hosted_chat_current_identity(request, (), None, 0, validation)

        config = SimpleNamespace(provider="openai")
        current_anchor = SimpleNamespace(id="container")
        request = segment.HostedChatSegmentRequest(
            "team_1",
            (),
            (),
            "token",
            SimpleNamespace(id="container"),
            "account_1",
        )
        with (
            mock.patch.object(segment, "_current_team_anchor", return_value=current_anchor),
            mock.patch.object(segment.hosted_resources, "_team_name_from_anchor", return_value="Team"),
            mock.patch.object(segment.assistant_lifecycle, "_dynamic_binding_snapshot", return_value={}),
            mock.patch.object(assistants, "_chat_file_metadata", return_value=[]),
            mock.patch.object(
                state._inference_store,
                "load",
                side_effect=segment.inference_config.InferenceConfigError("missing"),
            ),
            self.assertRaises(state.ApiError),
        ):
            segment._hosted_chat_current_identity(request, (), config, 1, validation)

        action_request = segment.brain_runtime_client.ActionRequest(
            "interrupt",
            "assistant",
            "action",
            {},
        )
        execution = segment.HostedActionExecution("team_1", "token", {}, {})
        with self.assertRaises(state.ApiError):
            segment._execute_hosted_action(execution, action_request, object(), {}, object())

    def test_integration_challenge_pause_and_human_pending_failures(self) -> None:
        challenge = SimpleNamespace(team_id="team_1", requirements=(SimpleNamespace(assistant_id="assistant"),))
        with (
            mock.patch.object(
                assistants,
                "_installed_assistant",
                side_effect=segment.assistant_registry.AssistantSpecError("changed"),
            ),
            self.assertRaises(state.ApiError),
        ):
            segment._hosted_integration_challenge_payload(challenge)

        contract = SimpleNamespace()
        container = object()
        with (
            mock.patch.object(
                assistants,
                "_installed_assistant",
                return_value=("assistant", contract, container),
            ),
            mock.patch.object(assistants, "_hosted_integration_spec", return_value=object()),
            mock.patch.object(segment.integration_flow, "challenge_payload", return_value={"ok": True}),
        ):
            self.assertEqual(segment._hosted_integration_challenge_payload(challenge), {"ok": True})

        pending = self.pending()
        suspension = SimpleNamespace(continuation=SimpleNamespace())
        with (
            mock.patch.object(
                state._integration_challenges,
                "create",
                side_effect=segment.integration_challenges.IntegrationChallengeError("pending"),
            ),
            self.assertRaises(state.ApiError),
        ):
            segment._pause_hosted_connection("team_1", "token", suspension, (), pending)

        created = object()
        with (
            mock.patch.object(state._integration_challenges, "create", return_value=created),
            mock.patch.object(segment, "_commit_hosted_suspension") as commit,
            mock.patch.object(segment, "_hosted_integration_challenge_payload", return_value={"pending": True}),
        ):
            self.assertEqual(
                segment._pause_hosted_connection("team_1", "token", suspension, (), pending),
                {"pending": True},
            )
        commit.assert_called_once()

        invalid_pending = self.pending(identity=())
        with self.assertRaises(state.ApiError):
            segment._purge_hosted_human_pending(invalid_pending)
        with (
            mock.patch.object(
                state,
                "_action_execution_journal",
                return_value=SimpleNamespace(
                    purge=mock.Mock(side_effect=segment.action_journal.ActionJournalError("failed"))
                ),
            ),
            self.assertRaises(state.ApiError),
        ):
            segment._purge_hosted_human_pending(pending)

        with (
            mock.patch.object(state._human_challenges, "cancel_team"),
            mock.patch.object(segment, "_purge_hosted_human_pending"),
            mock.patch.object(state, "_commit_chat_terminal", return_value=False),
            self.assertRaises(state.ApiError),
        ):
            segment._terminal_hosted_human_failure("team_1", "token", pending, "denied")
        with (
            mock.patch.object(state._human_challenges, "cancel_team"),
            mock.patch.object(segment, "_purge_hosted_human_pending"),
            mock.patch.object(state, "_commit_chat_terminal", return_value=True),
        ):
            self.assertEqual(
                segment._terminal_hosted_human_failure("team_1", "token", pending, "denied"),
                {"team_id": "team_1", "status": "human-denied", "reason": "denied"},
            )

        with (
            mock.patch.object(
                segment.action_challenges,
                "challenge_payload",
                side_effect=segment.action_challenges.HumanChallengeError("changed"),
            ),
            self.assertRaises(state.ApiError),
        ):
            segment._hosted_human_challenge_payload(object())

    def test_human_pause_and_terminal_dispatch_validate_exact_types(self) -> None:
        pending = self.pending()
        outcome = SimpleNamespace(request=object(), continuation=SimpleNamespace())
        with mock.patch.object(segment, "_terminal_hosted_human_failure", return_value={"failed": True}) as terminal:
            self.assertEqual(segment._pause_hosted_human("team_1", "token", outcome, (), pending), {"failed": True})
        terminal.assert_called_once()

        response = SimpleNamespace(secret=True)
        transcript = SimpleNamespace(responses=(response,))
        pending = assistants._PendingHostedChat(
            SimpleNamespace(),
            (),
            (),
            "account_1",
            ("generation",),
            (transcript,),
        )
        requirement = SimpleNamespace(request=outcome.request)
        with mock.patch.object(segment, "_terminal_hosted_human_failure", return_value={"secret": True}):
            self.assertEqual(
                segment._pause_hosted_human("team_1", "token", outcome, (requirement,), pending),
                {"secret": True},
            )

        with (
            mock.patch.object(
                state._human_challenges,
                "create",
                side_effect=segment.action_challenges.HumanChallengeError("pending"),
            ),
            mock.patch.object(segment, "_purge_hosted_human_pending") as purge,
            self.assertRaises(state.ApiError),
        ):
            clean_pending = self.pending()
            segment._pause_hosted_human("team_1", "token", outcome, (requirement,), clean_pending)
        purge.assert_called_once_with(clean_pending)

        invalid_segment = SimpleNamespace(
            outcome=object(),
            identity=(),
            team_name="Team",
            requirement_groups=mock.Mock(return_value=((), ())),
        )
        with (
            mock.patch.object(segment.chat_turn_engine, "dispatch", side_effect=lambda *_args: _args[2](object())),
            self.assertRaises(AssertionError),
        ):
            segment._hosted_segment_response(
                segment.HostedSegmentResponseRequest("team_1", "token", invalid_segment, (), (), "account_1")
            )

    def test_segment_callbacks_require_fresh_action_and_human_evidence(self) -> None:
        action = SimpleNamespace(summary="Action", input_schema={})
        contract = SimpleNamespace(name="Reviewed Assistant", actions={"action": action})
        active = SimpleNamespace(
            assistant_id="assistant",
            container=SimpleNamespace(id="assistant-container"),
            contract=contract,
            image="image",
            version="0.4.1",
        )
        config = SimpleNamespace(provider="openai", model="model")
        identity = ("identity",)
        request = segment.HostedChatSegmentRequest(
            "team_1",
            (),
            ("assistant",),
            "token",
            SimpleNamespace(id="container"),
            "account_1",
            message="hello",
        )

        def run_callbacks(strategy, **_kwargs):
            prepared = strategy.prepare()
            execute = prepared.durable_batch._strategy.execute
            requested = segment.brain_runtime_client.ActionRequest("interrupt", "assistant", "action", {})
            with self.assertRaises(AssertionError):
                execute(requested, {})

            strategy.validate_context()
            with self.assertRaises(AssertionError):
                execute(requested, {})

            missing = segment.brain_runtime_client.ActionRequest("interrupt", "missing", "action", {})
            with self.assertRaises(segment.chat_orchestrator.ChatOrchestrationError):
                strategy.human_requirement(missing, object())
            missing_action = segment.brain_runtime_client.ActionRequest("interrupt", "assistant", "missing", {})
            with self.assertRaises(segment.chat_orchestrator.ChatOrchestrationError):
                strategy.human_requirement(missing_action, object())
            requirement = strategy.human_requirement(requested, object())
            self.assertEqual(requirement.action_id, "action")
            self.assertEqual(requirement.assistant_name, "Reviewed Assistant")
            return "Team", identity, SimpleNamespace(), SimpleNamespace(integrations=(), human=())

        with (
            mock.patch.object(
                segment,
                "_hosted_chat_setup",
                return_value=("Team", (active,), [], config, "key", 1, identity),
            ),
            mock.patch.object(segment.assistant_lifecycle, "_require_assistant_genesis", return_value="genesis"),
            mock.patch.object(segment.hosted_resources, "_brain_thread_id", return_value="thread"),
            mock.patch.object(segment, "_hosted_chat_current_identity", return_value=identity),
            mock.patch.object(segment.chat_turn_engine, "run_segment", side_effect=run_callbacks),
        ):
            result = segment._run_hosted_chat_segment_with_metadata(request, object(), object())
        self.assertEqual(result.team_name, "Team")

        def mismatch(strategy, **_kwargs):
            strategy.prepare()
            strategy.validate_context()

        with (
            mock.patch.object(
                segment,
                "_hosted_chat_setup",
                return_value=("Team", (active,), [], config, "key", 1, identity),
            ),
            mock.patch.object(segment.assistant_lifecycle, "_require_assistant_genesis", return_value="genesis"),
            mock.patch.object(segment.hosted_resources, "_brain_thread_id", return_value="thread"),
            mock.patch.object(segment, "_hosted_chat_current_identity", return_value=("changed",)),
            mock.patch.object(segment.chat_turn_engine, "run_segment", side_effect=mismatch),
            self.assertRaises(state.ApiError),
        ):
            segment._run_hosted_chat_segment_with_metadata(request, object(), object())

        invalid_segment = SimpleNamespace(
            outcome=object(),
            identity=(),
            team_name="Team",
            requirement_groups=mock.Mock(return_value=((), ())),
        )
        terminal = SimpleNamespace(reply="done")
        with (
            mock.patch.object(segment.chat_turn_engine, "dispatch", side_effect=lambda *_args: _args[-1](terminal)),
            mock.patch.object(state, "_commit_chat_terminal", return_value=False),
            self.assertRaises(state.ApiError),
        ):
            segment._hosted_segment_response(
                segment.HostedSegmentResponseRequest("team_1", "token", invalid_segment, (), (), "account_1")
            )

        with (
            mock.patch.object(segment.chat_turn_engine, "dispatch", side_effect=ValueError("invalid")),
            self.assertRaises(state.ApiError),
        ):
            segment._hosted_segment_response(
                segment.HostedSegmentResponseRequest("team_1", "token", invalid_segment, (), (), "account_1")
            )


if __name__ == "__main__":
    unittest.main()
