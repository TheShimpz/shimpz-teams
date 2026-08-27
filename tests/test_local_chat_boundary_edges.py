from __future__ import annotations

import types
import unittest
from contextlib import nullcontext
from http import HTTPStatus
from unittest import mock

from action import challenges as action_challenges
from action import human as action_human
from action import journal as action_journal
from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from inference import client as brain_runtime_client
from integrations import flow as integration_flow
from local import app as local_app
from local.chat import api as local_chat_api
from local.chat import execution as local_chat_execution
from local.chat import human as local_chat_human
from local.chat.types import PendingLocalChat, ResponseRequest


def _pending(*, provider: str = "openai", identity: tuple[object, ...] = ("identity",)) -> PendingLocalChat:
    return PendingLocalChat(
        continuation=object(),
        assistant_ids=(),
        file_ids=(),
        provider=provider,
        identity=identity,
    )


def _action_private_inputs() -> local_app.action_execution.RpcPrivateInputs:
    return local_app.action_execution.RpcPrivateInputs({}, {})


def _challenge(payload: object) -> action_challenges.PendingHumanChallenge:
    return action_challenges.PendingHumanChallenge(
        id="challenge",
        team_id="team_1",
        expires_at=10,
        requirement=types.SimpleNamespace(interrupt_id="interrupt", request=object()),
        payload=payload,
    )


class LocalHumanBoundaryEdgeTests(unittest.TestCase):
    def test_pending_projection_and_expiration_validate_local_payloads(self) -> None:
        subject = types.SimpleNamespace(
            assistant_lifecycle=types.SimpleNamespace(_network=mock.Mock()),
            human_challenges=types.SimpleNamespace(
                current=lambda _team_id: None,
                drain_expired=lambda: (),
            ),
        )
        self.assertEqual(
            local_chat_human.pending_chat_human(subject, "team_1"),
            {"team_id": "team_1", "status": "none"},
        )

        challenge = object()
        subject.human_challenges.current = lambda _team_id: challenge
        subject._human_response = lambda value: {"challenge": value}
        self.assertEqual(
            local_chat_human.pending_chat_human(subject, "team_1"),
            {"challenge": challenge},
        )

        subject.human_challenges.drain_expired = lambda: (_challenge(object()),)
        with self.assertRaises(AssertionError):
            local_chat_human._expire_human_challenges(subject)

    def test_resume_body_rejects_unknown_decisions_and_fields(self) -> None:
        invalid = (
            None,
            {"decision": "unknown"},
            {"challenge_id": "id", "decision": "deny", "value": None},
            {"challenge_id": "id", "decision": "submit"},
        )
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(local_app.ApiProblem) as caught:
                local_chat_human._resume_body(body)
            self.assertEqual(caught.exception.code, "invalid-body")

    def test_pending_challenge_maps_expiration_and_rejects_foreign_payload(self) -> None:
        subject = types.SimpleNamespace(
            human_challenges=types.SimpleNamespace(
                get=mock.Mock(side_effect=action_challenges.HumanChallengeNotFoundError("expired")),
                drain_expired=lambda: (),
            )
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_human._pending_challenge(subject, "team_1", "challenge")
        self.assertEqual(caught.exception.code, "human-request-expired")

        subject.human_challenges.get = lambda *_args: _challenge(object())
        with self.assertRaises(AssertionError):
            local_chat_human._pending_challenge(subject, "team_1", "challenge")

    def test_context_validation_purges_provider_and_identity_drift(self) -> None:
        pending = _pending()
        challenge = _challenge(pending)
        events: list[str] = []
        subject = types.SimpleNamespace(
            human_challenges=types.SimpleNamespace(cancel_team=lambda _team_id: events.append("cancel")),
            _delete_chat_continuation=lambda *_args: events.append("delete"),
            _purge_human_pending=lambda _pending: events.append("purge"),
        )
        with self.assertRaises(AssertionError):
            local_chat_human._validate_pending_context(subject, "team_1", "openai", object())

        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_human._validate_pending_context(subject, "team_1", "anthropic", challenge)
        self.assertEqual(caught.exception.code, "team-context-changed")
        self.assertEqual(events, ["cancel", "delete", "purge"])

        events.clear()
        subject._chat_setup = lambda *_args: ("different",)
        subject._chat_identity = lambda *_args: ("different",)
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_human._validate_pending_context(subject, "team_1", "openai", challenge)
        self.assertEqual(caught.exception.code, "team-context-changed")
        self.assertEqual(events, ["cancel", "delete", "purge"])

    def test_invalid_submitted_human_response_is_not_claimed(self) -> None:
        pending = _pending()
        challenge = _challenge(pending)
        subject = types.SimpleNamespace(
            human_challenges=types.SimpleNamespace(claim=mock.Mock()),
            _delete_chat_continuation=mock.Mock(),
        )
        with (
            mock.patch.object(
                action_human,
                "append_response",
                side_effect=action_human.HumanRequestError("invalid"),
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_human._admit_human_response(
                subject,
                "team_1",
                challenge,
                pending,
                "submit",
                object(),
            )
        self.assertEqual(caught.exception.code, "invalid-human-response")
        subject.human_challenges.claim.assert_not_called()


class LocalChatApiBoundaryEdgeTests(unittest.TestCase):
    def test_pending_continuation_prefers_human_then_integration(self) -> None:
        human = object()
        integration = object()
        subject = types.SimpleNamespace(
            _expire_human_challenges=mock.Mock(),
            human_challenges=types.SimpleNamespace(current=lambda _team_id: human),
            integration_challenges=types.SimpleNamespace(current=lambda _team_id: integration),
            _human_response=lambda value: {"human": value},
            _integration_response=lambda value: {"integration": value},
        )
        self.assertEqual(
            local_chat_api._pending_chat_continuation(subject, "team_1"),
            {"human": human},
        )
        subject.human_challenges.current = lambda _team_id: None
        self.assertEqual(
            local_chat_api._pending_chat_continuation(subject, "team_1"),
            {"integration": integration},
        )

    def test_segment_dispatch_rejects_invalid_state_and_terminal_conflict(self) -> None:
        segment = types.SimpleNamespace(
            outcome=object(),
            identity=("identity",),
            team_name="Team",
            requirement_groups=lambda: (),
        )
        response = ResponseRequest("team_1", "token", segment, (), (), "openai")
        subject = types.SimpleNamespace(
            _delete_chat_continuation=mock.Mock(),
            _commit_chat_terminal=lambda *_args: False,
        )

        def invalid_pending(_outcome, _groups, pending, _pauses, _complete):
            return pending(object())

        with (
            mock.patch.object(local_chat_api.chat_turn_engine, "dispatch", invalid_pending),
            self.assertRaises(AssertionError),
        ):
            local_chat_api._segment_response(subject, response)

        def conflicting_terminal(_outcome, _groups, _pending, _pauses, complete):
            return complete(types.SimpleNamespace(reply="reply"))

        with (
            mock.patch.object(
                local_chat_api.chat_turn_engine,
                "dispatch",
                conflicting_terminal,
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_api._segment_response(subject, response)
        self.assertEqual(caught.exception.code, "chat-stopped")

        with (
            mock.patch.object(
                local_chat_api.chat_turn_engine,
                "dispatch",
                side_effect=ValueError("invalid dispatch"),
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_api._segment_response(subject, response)
        self.assertEqual(caught.exception.code, "internal-error")

    def test_chat_rejects_invalid_input_and_observes_pending_state_twice(self) -> None:
        subject = types.SimpleNamespace()
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_api.chat(subject, "team_1", {}, "openai", "key")
        self.assertEqual(caught.exception.code, "invalid-body")

        for message in ("", "\0"):
            with self.subTest(message=message), self.assertRaises(local_app.ApiProblem) as caught:
                local_chat_api.chat(
                    subject,
                    "team_1",
                    {"message": message, "files": [], "assistant_ids": []},
                    "openai",
                    "key",
                )
            self.assertEqual(caught.exception.code, "invalid-message")

        pending = {"status": "pending"}
        subject._pending_chat_continuation = lambda _team_id: pending
        self.assertIs(
            local_chat_api.chat(
                subject,
                "team_1",
                {"message": "hello", "files": [], "assistant_ids": []},
                "openai",
                "key",
            ),
            pending,
        )

        responses = iter((None, pending))
        subject._pending_chat_continuation = lambda _team_id: next(responses)
        subject._exclusive_chat_turn = lambda _team_id: nullcontext("token")
        self.assertIs(
            local_chat_api.chat(
                subject,
                "team_1",
                {"message": "hello", "files": [], "assistant_ids": []},
                "openai",
                "key",
            ),
            pending,
        )

    def test_integration_resume_rejects_invalid_shared_state(self) -> None:
        subject = types.SimpleNamespace(
            _exclusive_chat_turn=lambda _team_id: nullcontext("token"),
            _lock=lambda _team_id: nullcontext(),
            integration_challenges=object(),
            assistant_integrations=object(),
            oauth_pkce=types.SimpleNamespace(cancel_team=mock.Mock()),
            _integration_response=mock.Mock(),
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_api.resume_chat_integrations(subject, "team_1", {}, "openai", "key")
        self.assertEqual(caught.exception.code, "invalid-body")

        def inspect_invalid(strategy):
            strategy.inspect(object())

        with (
            mock.patch.object(
                local_chat_api.chat_turn_engine,
                "admit_integration_resume",
                side_effect=inspect_invalid,
            ),
            self.assertRaises(AssertionError),
        ):
            local_chat_api.resume_chat_integrations(
                subject,
                "team_1",
                {"challenge_id": "challenge"},
                "openai",
                "key",
            )

        response = {"status": "pending"}
        with mock.patch.object(
            local_chat_api.chat_turn_engine,
            "admit_integration_resume",
            return_value=types.SimpleNamespace(response=response, pending=None),
        ):
            self.assertIs(
                local_chat_api.resume_chat_integrations(
                    subject,
                    "team_1",
                    {"challenge_id": "challenge"},
                    "openai",
                    "key",
                ),
                response,
            )

        with (
            mock.patch.object(
                local_chat_api.chat_turn_engine,
                "admit_integration_resume",
                return_value=types.SimpleNamespace(response=None, pending=object()),
            ),
            self.assertRaises(AssertionError),
        ):
            local_chat_api.resume_chat_integrations(
                subject,
                "team_1",
                {"challenge_id": "challenge"},
                "openai",
                "key",
            )


class LocalChatExecutionBoundaryEdgeTests(unittest.TestCase):
    @staticmethod
    def _invocation_subject(container_id: str = "container") -> types.SimpleNamespace:
        container = types.SimpleNamespace(id=container_id)
        return types.SimpleNamespace(
            _lock=lambda _team_id: nullcontext(),
            assistant_lifecycle=types.SimpleNamespace(
                _resolve=lambda *_args: object(),
                _network=lambda _team_id: types.SimpleNamespace(name="network"),
                _assistant_container=lambda *_args: container,
                _validate_container=mock.Mock(),
                invoke=mock.Mock(return_value={"result": "ok"}),
            ),
            _active_chat_guard=nullcontext(),
            _active_chat_tokens={"team_1": "token"},
            _cancelled_chat_tokens=set(),
            _active_action_containers={},
            _chat_cancelled=lambda _token: False,
        )

    def test_action_invocation_rejects_generation_and_turn_drift(self) -> None:
        request = types.SimpleNamespace(
            interrupt_id="interrupt",
            assistant_id="assistant",
            action="action",
            input={},
        )
        subject = self._invocation_subject()
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_execution._invoke_chat_action(
                subject,
                "team_1",
                "token",
                request,
                "different",
                action_human.ActionTranscript(""),
                _action_private_inputs(),
            )
        self.assertEqual(caught.exception.code, "team-context-changed")

        subject._active_chat_tokens["team_1"] = "different"
        with self.assertRaises(chat_orchestrator.ChatStoppedError):
            local_chat_execution._invoke_chat_action(
                subject,
                "team_1",
                "token",
                request,
                "container",
                action_human.ActionTranscript(""),
                _action_private_inputs(),
            )

        subject = self._invocation_subject()

        def replace_active(*_args):
            subject._active_action_containers["team_1"] = ("new-token", object())
            return {"result": "ok"}

        subject.assistant_lifecycle.invoke.side_effect = replace_active
        self.assertEqual(
            local_chat_execution._invoke_chat_action(
                subject,
                "team_1",
                "token",
                request,
                "container",
                action_human.ActionTranscript(""),
                _action_private_inputs(),
            ),
            "ok",
        )
        self.assertEqual(subject._active_action_containers["team_1"][0], "new-token")

        subject = self._invocation_subject()
        subject.assistant_lifecycle.invoke.side_effect = local_app.ApiProblem(
            HTTPStatus.BAD_GATEWAY,
            "failed",
            code="assistant-failed",
        )
        subject._chat_cancelled = lambda _token: True
        with self.assertRaises(chat_orchestrator.ChatStoppedError):
            local_chat_execution._invoke_chat_action(
                subject,
                "team_1",
                "token",
                request,
                "container",
                action_human.ActionTranscript(""),
                _action_private_inputs(),
            )

        subject = self._invocation_subject()
        subject._chat_cancelled = lambda _token: True
        with self.assertRaises(chat_orchestrator.ChatStoppedError):
            local_chat_execution._invoke_chat_action(
                subject,
                "team_1",
                "token",
                request,
                "container",
                action_human.ActionTranscript(""),
                _action_private_inputs(),
            )

    def test_problem_mapping_covers_every_closed_failure_family(self) -> None:
        cases = (
            ("invalid-continuation", None, "internal-error"),
            ("invalid-suspension", None, "internal-error"),
            ("context-changed", None, "team-context-changed"),
            ("journal", action_journal.ActionJournalError("failed"), "action-state-unavailable"),
            ("stopped", chat_orchestrator.ChatStoppedError("stopped"), "chat-stopped"),
            (
                "orchestration",
                chat_orchestrator.ChatOrchestrationError("failed"),
                "brain-runtime-failed",
            ),
            ("brain", brain_runtime_client.BrainRuntimeError("failed"), "brain-runtime-failed"),
        )
        for reason, failure, expected_code in cases:
            with self.subTest(reason=reason), self.assertRaises(local_app.ApiProblem) as caught:
                local_chat_execution._raise_chat_problem(reason, failure)
            self.assertEqual(caught.exception.code, expected_code)

        with self.assertRaises(AssertionError):
            local_chat_execution._raise_chat_problem("unknown", None)

    def test_action_contract_and_integration_errors_are_mapped(self) -> None:
        active = types.SimpleNamespace(spec=types.SimpleNamespace(actions={}))
        bindings = {"assistant": active}
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_execution._validate_chat_action(bindings, "assistant", "missing", {})
        self.assertEqual(caught.exception.code, "invalid-action-input")

        active.spec.actions["action"] = object()
        with (
            mock.patch.object(
                local_chat_execution,
                "validate_action_payload",
                side_effect=ValueError("invalid payload"),
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_execution._validate_chat_action(bindings, "assistant", "action", {})
        self.assertEqual(caught.exception.code, "invalid-action-input")

        requirements = chat_turn_engine.SegmentRequirements()
        subject = types.SimpleNamespace(assistant_integrations=object())
        with (
            mock.patch.object(
                integration_flow,
                "requirements_for_batch",
                side_effect=integration_flow.IntegrationFlowError("invalid"),
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_execution._require_chat_private_inputs(
                subject,
                "team_1",
                bindings,
                (),
                requirements,
            )
        self.assertEqual(caught.exception.code, "assistant-integration-contract-invalid")


if __name__ == "__main__":
    unittest.main()
