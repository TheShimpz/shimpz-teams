import dataclasses
import threading
import types
import unittest
from contextlib import nullcontext
from http import HTTPStatus
from unittest import mock

from local_controller_harness import chat_body

from action import challenges as action_challenges
from action import human as action_human
from action import journal as action_journal
from action import stored_input as action_stored_input
from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from inference import client as brain_runtime_client
from integrations import challenges as integration_challenges
from integrations import flow as integration_flow
from local import app as local_app
from local.chat import api as local_chat_api
from local.chat import execution as local_chat_execution
from local.chat import human as local_chat_human
from local.chat import pause as local_chat_pause
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
                drain_expired=lambda _team_id: (),
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

        subject.human_challenges.drain_expired = lambda _team_id: (_challenge(object()),)
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
                drain_expired=lambda _team_id: (),
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
        live: list[object] = [challenge]
        subject = types.SimpleNamespace(
            human_challenges=types.SimpleNamespace(
                current=lambda _team_id: live[0], withdraw_team=lambda _team_id: events.append("withdraw")
            ),
            _delete_withdrawn_continuation=lambda _team_id, item: events.append(("delete", item.id)),
            _purge_human_pending=lambda item: events.append(("purge", item is pending)),
        )
        with self.assertRaises(AssertionError):
            local_chat_human._validate_pending_context(subject, "team_1", "openai", object())

        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_human._validate_pending_context(subject, "team_1", "anthropic", challenge)
        self.assertEqual(caught.exception.code, "team-context-changed")
        self.assertEqual(events, ["withdraw", ("delete", "challenge"), ("purge", True)])

        # A challenge no longer live, or reissued with the same batch, has another owner: nothing is touched.
        for stale in (None, dataclasses.replace(challenge, id="reissued")):
            events.clear()
            live[0] = stale
            with self.assertRaises(local_app.ApiProblem) as caught:
                local_chat_human._validate_pending_context(subject, "team_1", "anthropic", challenge)
            self.assertEqual(caught.exception.code, "team-context-changed")
            self.assertEqual(events, [])

        live[0] = challenge
        events.clear()
        subject._chat_setup = lambda *_args: ("different",)
        subject._chat_identity = lambda *_args: ("different",)
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_human._validate_pending_context(subject, "team_1", "openai", challenge)
        self.assertEqual(caught.exception.code, "team-context-changed")
        self.assertEqual(events, ["withdraw", ("delete", "challenge"), ("purge", True)])

    def test_a_drifted_integration_turn_ends_with_its_oauth_state_before_its_continuation(self) -> None:
        challenge = integration_challenges.PendingIntegrationChallenge("challenge", "team_1", 10, (), _pending())
        events: list[object] = []
        live: list[object] = [challenge]
        subject = types.SimpleNamespace(
            integration_challenges=types.SimpleNamespace(
                current=lambda _team_id: live[0], withdraw_team=lambda _team_id: events.append("withdraw")
            ),
            oauth_pkce=types.SimpleNamespace(cancel_team=lambda _team_id: events.append("oauth")),
            _delete_withdrawn_continuation=lambda _team_id, item: events.append(("delete", item.id)),
        )
        with self.assertRaises(AssertionError):
            local_chat_pause._end_drifted_turn(subject, "team_1", object())

        with self.assertRaises(local_app.ApiProblem) as caught:
            local_chat_pause._end_drifted_turn(subject, "team_1", challenge)
        self.assertEqual(caught.exception.code, "team-context-changed")
        self.assertEqual(events, ["withdraw", "oauth", ("delete", "challenge")])

        # A challenge no longer live, or another turn's, has another owner: nothing of it or its Team is touched.
        for stale in (None, dataclasses.replace(challenge, id="newer")):
            events.clear()
            live[0] = stale
            with self.assertRaises(local_app.ApiProblem) as caught:
                local_chat_pause._end_drifted_turn(subject, "team_1", challenge)
            self.assertEqual(caught.exception.code, "team-context-changed")
            self.assertEqual(events, [])

    def test_invalid_submitted_human_response_is_not_claimed(self) -> None:
        pending = _pending()
        challenge = _challenge(pending)
        subject = types.SimpleNamespace(
            human_challenges=types.SimpleNamespace(claim=mock.Mock(), claim_after=mock.Mock()),
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
            local_chat_human._admit_human_response(subject, "team_1", challenge, pending, ("submit", object()), ())
        self.assertEqual(caught.exception.code, "invalid-human-response")
        subject.human_challenges.claim.assert_not_called()
        subject.human_challenges.claim_after.assert_not_called()

    def test_a_stored_input_answer_is_sealed_for_its_paused_action_before_its_challenge_is_consumed(self) -> None:
        paused = brain_runtime_client.ActionRequest("interrupt", "assistant", "action", {"zone": "example.com"})
        sibling = brain_runtime_client.ActionRequest("other", "assistant", "action", {})
        pending = PendingLocalChat(
            continuation=types.SimpleNamespace(turn=types.SimpleNamespace(actions=(sibling, paused))),
            assistant_ids=("assistant",),
            file_ids=(),
            provider="openai",
            identity=("identity",),
        )
        request = types.SimpleNamespace(stored_input="app-secret")
        requirement = types.SimpleNamespace(
            interrupt_id="interrupt", assistant_id="assistant", action_id="action", request=request
        )
        challenge = action_challenges.PendingHumanChallenge("challenge", "team_1", 10, requirement, pending)
        declaration = types.SimpleNamespace(kind="password")
        spec = types.SimpleNamespace(
            assistant_id="assistant",
            actions={"action": types.SimpleNamespace(stored_inputs=("app-secret", "token"))},
            stored_inputs={"app-secret": declaration, "token": declaration},
        )
        assistants = (types.SimpleNamespace(spec=spec),)
        submission = action_human.StoredInputSubmission("app-secret", "private-value")
        admission = action_human.HumanResponseAdmission((), 3, submission)
        consumed: list[str] = []

        def claim_after(_team_id, _challenge_id, commit):
            commit(challenge)
            consumed.append(_challenge_id)

        store = types.SimpleNamespace(seal=mock.Mock())
        subject = types.SimpleNamespace(
            human_challenges=types.SimpleNamespace(claim_after=claim_after),
            _delete_chat_continuation=mock.Mock(),
            assistant_stored_inputs=store,
        )
        with (
            mock.patch.object(action_human, "append_response", return_value=admission),
            mock.patch.object(local_chat_human.local_audit, "record_request") as audit,
        ):
            admitted = local_chat_human._admit_human_response(
                subject, "team_1", challenge, pending, ("submit", "private-value"), assistants
            )
        self.assertIs(admitted, admission)
        self.assertEqual(consumed, ["challenge"])
        store.seal.assert_called_once_with(
            "team_1",
            "assistant",
            "app-secret",
            "password",
            "private-value",
            local_app.action_execution.stored_input_origin(paused),
        )
        audit.assert_called_once_with(
            "assistant-action",
            result="ok",
            team_id="team_1",
            assistant="assistant",
            detail="stored-input-sealed:action:app-secret",
        )
        self.assertNotIn("private-value", repr(audit.call_args_list))

        # A failed seal, a slot the Action does not declare, or an answer for another interrupt consumes nothing.
        refusals = (
            (
                types.SimpleNamespace(seal=mock.Mock(side_effect=action_stored_input.StoredInputStoreError("x"))),
                spec,
                requirement,
                "assistant-stored-input-state-unavailable",
            ),
            (
                store,
                types.SimpleNamespace(
                    actions={"action": types.SimpleNamespace(stored_inputs=("token",))},
                    stored_inputs=spec.stored_inputs,
                    assistant_id="assistant",
                ),
                requirement,
                "invalid-human-response",
            ),
            (
                store,
                spec,
                types.SimpleNamespace(**{**vars(requirement), "interrupt_id": "missing"}),
                "invalid-human-response",
            ),
        )
        refusals = (*refusals, (store, None, requirement, "invalid-human-response"))
        for refused_store, refused_spec, refused_requirement, code in refusals:
            consumed.clear()
            subject.assistant_stored_inputs = refused_store
            refused_challenge = action_challenges.PendingHumanChallenge(
                "challenge", "team_1", 10, refused_requirement, pending
            )

            def refusing_claim(_team_id, _challenge_id, commit, current=refused_challenge):
                commit(current)
                consumed.append(_challenge_id)

            subject.human_challenges.claim_after = refusing_claim
            with (
                self.subTest(code=code),
                mock.patch.object(action_human, "append_response", return_value=admission),
                mock.patch.object(local_chat_human.local_audit, "record_request"),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                local_chat_human._admit_human_response(
                    subject,
                    "team_1",
                    refused_challenge,
                    pending,
                    ("submit", "private-value"),
                    () if refused_spec is None else (types.SimpleNamespace(spec=refused_spec),),
                )
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(consumed, [])


class LocalChatApiBoundaryEdgeTests(unittest.TestCase):
    def test_pending_continuation_prefers_human_then_integration(self) -> None:
        human = types.SimpleNamespace(requirement=types.SimpleNamespace(copy=types.SimpleNamespace(locale="fr")))
        pending = types.SimpleNamespace(provider="anthropic", identity=("identity",))
        integration = types.SimpleNamespace(payload=pending)
        # Every reopening is validated against the binding, even one that keeps the challenge's own language.
        relocalized = mock.Mock(side_effect=lambda challenge, _locale: challenge)
        subject = types.SimpleNamespace(
            _expire_human_challenges=mock.Mock(),
            _lock=lambda _team_id: nullcontext(),
            human_challenges=types.SimpleNamespace(current=lambda _team_id: human),
            integration_challenges=types.SimpleNamespace(current=lambda _team_id: integration),
            _relocalized_human=relocalized,
            _human_response=lambda value: {"human": value},
            _integration_response=lambda value: {"integration": value},
            _chat_identity=lambda *current: current,
        )
        self.assertEqual(
            local_chat_api._pending_chat_continuation(subject, "team_1"),
            {"human": human},
        )
        self.assertEqual(local_chat_api._pending_chat_continuation(subject, "team_1", "pt"), {"human": human})
        self.assertEqual(relocalized.call_args_list, [mock.call(human, "fr"), mock.call(human, "pt")])
        subject.human_challenges.current = lambda _team_id: None
        # The Integration gate is validated against the provider and context its own turn paused with.
        with mock.patch.object(
            local_chat_api.local_chat_pause, "_paused_setup", return_value=(pending, ("identity",))
        ) as setup:
            self.assertEqual(
                local_chat_api._pending_chat_continuation(subject, "team_1"),
                {"integration": integration},
            )
        setup.assert_called_once_with(subject, "team_1", "anthropic", integration)

    def test_segment_dispatch_rejects_invalid_state_and_terminal_conflict(self) -> None:
        segment = types.SimpleNamespace(
            outcome=object(),
            identity=("identity",),
            team_name="Team",
            requirement_groups=lambda: (),
        )
        response = ResponseRequest("team_1", "token", segment, (), (), "openai", recording="b" * 32)
        write = mock.Mock()
        subject = types.SimpleNamespace(
            _delete_chat_continuation=mock.Mock(),
            _commit_chat_terminal=lambda *_args: False,
            _lock=lambda _team_id: threading.RLock(),
            _routine_record=mock.Mock(return_value=(write, {"routine_refusal": {"code": "routine-recording-empty"}})),
            routine_recordings=types.SimpleNamespace(end=mock.Mock()),
        )

        def invalid_pending(_outcome, _groups, pending, _pauses, _complete):
            return pending(object())

        with (
            mock.patch.object(local_chat_api.chat_turn_engine, "dispatch", invalid_pending),
            self.assertRaises(AssertionError),
        ):
            local_chat_api._segment_response(subject, response)

        terminal = types.SimpleNamespace(
            reply="reply",
            routine={"op": "record"},
            clarification=None,
            restricted_actions=None,
            memory=(),
            actions=(),
        )

        def conflicting_terminal(_outcome, _groups, _pending, _pauses, complete):
            return complete(terminal)

        with (
            mock.patch.object(local_chat_api.chat_turn_engine, "dispatch", conflicting_terminal),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_api._segment_response(subject, response)
        self.assertEqual(caught.exception.code, "chat-stopped")
        # Stop won the commit, so the turn's card was admitted but never kept, and its span went on unchanged.
        subject._routine_record.assert_called_once()
        write.assert_not_called()

        def failing_commit(*_args):
            raise local_app.ApiProblem(503, "memory", code="memory-store-failed")

        subject._commit_chat_terminal = failing_commit
        with (
            mock.patch.object(local_chat_api.chat_turn_engine, "dispatch", conflicting_terminal),
            self.assertRaises(local_app.ApiProblem),
        ):
            local_chat_api._segment_response(subject, response)
        write.assert_not_called()

        def committing(_team_id, _token, apply):
            apply()
            return True

        subject._commit_chat_terminal = committing
        with mock.patch.object(local_chat_api.chat_turn_engine, "dispatch", conflicting_terminal):
            body = local_chat_api._segment_response(subject, response)
        # The card or its refusal is written with the reply and carried beside it.
        write.assert_called_once_with()
        self.assertEqual(
            (body["reply"], body["routine_refusal"], body["clarification"]),
            ("reply", {"code": "routine-recording-empty"}, None),
        )

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

    def test_a_card_whose_terminal_line_outgrows_its_bound_is_refused_never_cut(self) -> None:
        write = mock.Mock()
        terminal = types.SimpleNamespace(routine={"op": "record"})
        body = {"team_id": "team_1", "reply": "x" * 200_000}
        for fields, expected in (
            ({"routine_proposal": {"name": "y" * 70_000}}, {"routine_refusal": {"code": "routine-proposal-too-large"}}),
            ({"routine_proposal": {"name": "y"}}, {"routine_proposal": {"name": "y"}}),
            ({"routine_refusal": {"code": "z" * 63}}, {"routine_refusal": {"code": "z" * 63}}),
        ):
            with self.subTest(fields=fields):
                subject = types.SimpleNamespace(_routine_record=mock.Mock(return_value=(write, fields)))
                chosen_write, chosen = local_chat_api._routine_outcome(subject, object(), terminal, body)
                self.assertEqual(chosen, expected)
                # A refused card writes nothing; a card or refusal that fits keeps its own write.
                self.assertEqual(chosen_write is write, chosen == fields)

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
                    chat_body(message),
                    "openai",
                    "key",
                )
            self.assertEqual(caught.exception.code, "invalid-message")

        for body, code in (
            ({**chat_body("hello"), "request": {"issued_at": "now", "nonce": "0" * 32}}, "invalid-request"),
            (chat_body("hello", timezone="Mars/Olympus"), "invalid-timezone"),
        ):
            with self.subTest(code=code), self.assertRaises(local_app.ApiProblem) as caught:
                local_chat_api.chat(subject, "team_1", body, "openai", "key")
            self.assertEqual(caught.exception.code, code)

        pending = {"status": "pending"}
        subject._pending_chat_continuation = lambda _team_id, _locale: pending
        # A zone that loads is admitted, and the pending turn answers before anything records.
        self.assertIs(
            local_chat_api.chat(subject, "team_1", chat_body("hello", timezone="America/Sao_Paulo"), "openai", "key"),
            pending,
        )
        self.assertIs(
            local_chat_api.chat(
                subject,
                "team_1",
                chat_body("hello"),
                "openai",
                "key",
            ),
            pending,
        )

        responses = iter((None, pending))
        subject._pending_chat_continuation = lambda _team_id, _locale: next(responses)
        subject._exclusive_chat_turn = lambda _team_id: nullcontext("token")
        self.assertIs(
            local_chat_api.chat(
                subject,
                "team_1",
                chat_body("hello"),
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
            local_chat_api.resume_chat_integrations(subject, "team_1", {"challenge_id": "challenge"}, "openai", "key")

        response = {"status": "pending"}
        with mock.patch.object(
            local_chat_api.chat_turn_engine,
            "admit_integration_resume",
            return_value=types.SimpleNamespace(response=response, pending=None),
        ):
            self.assertIs(
                local_chat_api.resume_chat_integrations(
                    subject, "team_1", {"challenge_id": "challenge"}, "openai", "key"
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
            local_chat_api.resume_chat_integrations(subject, "team_1", {"challenge_id": "challenge"}, "openai", "key")


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

    @staticmethod
    def _invoke(subject: types.SimpleNamespace, request: object, container_id: str) -> object:
        """Invoke one chat Action for Team team_1 under chat token "token" with fresh, non-secret evidence."""
        evidence = local_app.action_execution.ActionInvocationEvidence(
            _action_private_inputs(),
            action_human.ActionTranscript(""),
            "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
        )
        return local_chat_execution._invoke_chat_action(subject, "team_1", "token", request, container_id, evidence)

    def test_action_invocation_rejects_generation_and_turn_drift(self) -> None:
        request = types.SimpleNamespace(
            interrupt_id="interrupt",
            assistant_id="assistant",
            action="action",
            input={},
        )
        subject = self._invocation_subject()
        with self.assertRaises(local_app.ApiProblem) as caught:
            self._invoke(subject, request, "different")
        self.assertEqual(caught.exception.code, "team-context-changed")

        subject._active_chat_tokens["team_1"] = "different"
        with self.assertRaises(chat_orchestrator.ChatStoppedError):
            self._invoke(subject, request, "container")

        subject = self._invocation_subject()

        def replace_active(*_args):
            subject._active_action_containers["team_1"] = ("new-token", object())
            return {"result": "ok"}

        subject.assistant_lifecycle.invoke.side_effect = replace_active
        self.assertEqual(
            self._invoke(subject, request, "container"),
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
            self._invoke(subject, request, "container")

        subject = self._invocation_subject()
        subject._chat_cancelled = lambda _token: True
        with self.assertRaises(chat_orchestrator.ChatStoppedError):
            self._invoke(subject, request, "container")

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

    def test_a_rejected_stored_input_clear_failure_is_redacted(self) -> None:
        store = types.SimpleNamespace(
            delete=mock.Mock(side_effect=action_stored_input.StoredInputStoreError("unavailable"))
        )
        with (
            mock.patch.object(local_chat_execution.local_audit, "record_request"),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            local_chat_execution.clear_rejected_stored_input(store, "team_1", "assistant", "action", "token")
        self.assertEqual(caught.exception.code, "assistant-stored-input-state-unavailable")


if __name__ == "__main__":
    unittest.main()
