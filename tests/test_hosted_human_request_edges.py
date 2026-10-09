import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import hosted_assistant_fixture as harness

from tests import human_request_fixtures

human = harness.hosted_chat_human
segment = harness.hosted_chat_segment
state = harness.runtime_state
assistants = harness.hosted_assistants
action_challenges = segment.action_challenges
action_human = segment.action_human


class HostedHumanRequestEdgeTests(unittest.TestCase):
    @staticmethod
    def pending(owner: str = "account_1", identity: tuple[object, ...] = ("identity",)) -> object:
        return assistants._PendingHostedChat(
            SimpleNamespace(),
            (),
            (),
            owner,
            identity,
            (),
        )

    def test_expiry_pending_and_body_shapes_fail_closed(self) -> None:
        invalid = SimpleNamespace(payload=object())
        with (
            mock.patch.object(state._human_challenges, "drain_expired", return_value=(invalid,)),
            self.assertRaises(AssertionError),
        ):
            human._expire_challenges("team_1")

        pending = self.pending()
        expired = SimpleNamespace(payload=pending)
        with (
            mock.patch.object(state._human_challenges, "drain_expired", return_value=(expired,)),
            mock.patch.object(segment, "_purge_hosted_human_pending") as purge,
        ):
            human._expire_challenges("team_1")
        purge.assert_called_once_with(pending)

        with (
            mock.patch.object(human, "_expire_challenges") as expire,
            mock.patch.object(state._human_challenges, "current", return_value=None),
        ):
            self.assertEqual(human.pending_chat_human("team_1")["status"], "none")
        # Reading one Team's challenge never purges another Team's expired continuation.
        expire.assert_called_once_with("team_1")

        for body in (None, {"decision": "unknown"}, {"challenge_id": "id", "decision": "deny", "value": True}):
            with self.subTest(body=body), self.assertRaises(state.ApiError):
                human._resume_body(body)
        self.assertEqual(human._resume_body({"challenge_id": "id", "decision": "deny"}), ("id", "deny", None))

    def test_pending_and_context_validation_reject_stale_capabilities(self) -> None:
        with (
            mock.patch.object(
                state._human_challenges,
                "get",
                side_effect=action_challenges.HumanChallengeNotFoundError("missing"),
            ),
            mock.patch.object(human, "_expire_challenges") as expire,
            self.assertRaises(state.ApiError),
        ):
            human._pending_challenge("team_1", "id")
        expire.assert_called_once_with("team_1")

        challenge = SimpleNamespace(payload=object())
        with (
            mock.patch.object(state._human_challenges, "get", return_value=challenge),
            self.assertRaises(AssertionError),
        ):
            human._pending_challenge("team_1", "id")

        challenge.payload = self.pending(owner="other")
        with self.assertRaises(state.ApiError):
            human._validate_pending_context("team_1", challenge, object(), "account_1")

        challenge.payload = self.pending(identity=("expected",))
        requirement = human_request_fixtures.requirement(human_request_fixtures.request("approval"))
        challenge.requirement = requirement
        messages = requirement.request.messages()
        current = SimpleNamespace(
            assistant_id=requirement.assistant_id,
            contract=SimpleNamespace(machine_contract={"messages": messages}, pack_digest=requirement.copy.pack_digest),
        )
        repacked = SimpleNamespace(
            assistant_id=requirement.assistant_id,
            contract=SimpleNamespace(machine_contract={"messages": messages}, pack_digest=f"sha256:{'9' * 64}"),
        )
        other = SimpleNamespace(assistant_id="other-assistant", contract=current.contract)
        # A changed identity, a changed pack, or a missing Assistant all end the challenge (ADR-0091).
        for assistants_now, identity in (
            ((current,), ("changed",)),
            ((repacked,), ("expected",)),
            ((other,), ("expected",)),
        ):
            with (
                self.subTest(identity=identity),
                mock.patch.object(
                    segment, "_hosted_chat_setup", return_value=("t", assistants_now, *("u",) * 4, identity)
                ),
                mock.patch.object(state._human_challenges, "cancel_team") as cancel,
                mock.patch.object(segment, "_purge_hosted_human_pending") as purge,
                self.assertRaises(state.ApiError),
            ):
                human._validate_pending_context("team_1", challenge, object(), "account_1")
            cancel.assert_called_once_with("team_1")
            purge.assert_called_once_with(challenge.payload)

        with mock.patch.object(
            segment,
            "_hosted_chat_setup",
            return_value=("t", (current,), *("u",) * 4, ("expected",)),
        ):
            self.assertEqual(
                human._validate_pending_context("team_1", challenge, object(), "account_1"),
                (challenge.payload, (current,)),
            )

    def test_response_admission_handles_denial_assurance_and_schema_failures(self) -> None:
        pending = self.pending()
        request = SimpleNamespace(kind="approval")
        challenge = SimpleNamespace(
            id="challenge",
            requirement=SimpleNamespace(request=request, interrupt_id="interrupt"),
        )
        with self.assertRaises(state.ApiError):
            human._admit_response("team_1", challenge, (pending, ()), "deny", None, {"kind": "auth"})
        with mock.patch.object(state._human_challenges, "claim") as claim:
            self.assertIsNone(human._admit_response("team_1", challenge, (pending, ()), "deny", None, None))
        claim.assert_called_once_with("team_1", "challenge")

        self.assertIsNone(
            human._admit_response(
                "team_1",
                challenge,
                (pending, ()),
                "submit",
                True,
                {"kind": "unexpected"},
            )
        )
        with (
            mock.patch.object(
                action_human,
                "append_response",
                side_effect=action_human.HumanRequestError("invalid"),
            ),
            self.assertRaises(state.ApiError),
        ):
            human._admit_response("team_1", challenge, (pending, ()), "submit", True, None)

    def test_resume_failure_and_cancel_paths_are_terminal(self) -> None:
        pending = self.pending()
        lease = SimpleNamespace(owner="account_1")

        @contextmanager
        def exclusive(_team_id, _lease):
            yield "token", object()

        for decision, reason in (("deny", "denied"), ("submit", "authentication-failed")):
            with (
                self.subTest(decision=decision),
                mock.patch.object(human, "_resume_body", return_value=("id", decision, None)),
                mock.patch.object(human, "_pending_challenge", return_value=object()),
                mock.patch.object(human, "_validate_pending_context", return_value=(pending, ())),
                mock.patch.object(human, "_admit_response", return_value=None),
                mock.patch.object(
                    segment,
                    "_terminal_hosted_human_failure",
                    return_value={"reason": reason},
                ) as terminal,
            ):
                self.assertEqual(human.resume_chat_human("team_1", {}, None, lease, exclusive)["reason"], reason)
            terminal.assert_called_once_with("team_1", "token", pending, reason)

        with (
            mock.patch.object(human, "_expire_challenges"),
            mock.patch.object(state._human_challenges, "withdraw_team", return_value=None),
        ):
            self.assertFalse(human.cancel_pending("team_1"))
        with (
            mock.patch.object(human, "_expire_challenges"),
            mock.patch.object(state._human_challenges, "withdraw_team", return_value=SimpleNamespace(payload=object())),
            self.assertRaises(AssertionError),
        ):
            human.cancel_pending("team_1")

        challenge = SimpleNamespace(payload=pending)
        with (
            mock.patch.object(human, "_expire_challenges"),
            mock.patch.object(state._human_challenges, "withdraw_team", return_value=challenge),
            mock.patch.object(segment, "_purge_hosted_human_pending") as purge,
        ):
            self.assertTrue(human.cancel_pending("team_1"))
        purge.assert_called_once_with(pending)


if __name__ == "__main__":
    unittest.main()


class HostedStoredInputAnswerTests(unittest.TestCase):
    def test_a_stored_input_answer_is_sealed_for_its_paused_action_or_refused_without_a_trace(self) -> None:
        paused = harness.hosted_chat_segment.brain_runtime_client.ActionRequest(
            "interrupt", "assistant", "action", {"zone": "example.com"}
        )
        pending = assistants._PendingHostedChat(
            SimpleNamespace(turn=SimpleNamespace(actions=(paused,))), (), (), "account_1", ("identity",), ()
        )
        requirement = SimpleNamespace(
            interrupt_id="interrupt",
            assistant_id="assistant",
            action_id="action",
            request=SimpleNamespace(stored_input="app-secret"),
        )
        challenge = SimpleNamespace(requirement=requirement)
        declaration = SimpleNamespace(kind="password")
        contract = SimpleNamespace(
            actions={"action": SimpleNamespace(stored_inputs=("app-secret", "token"))},
            stored_inputs={"app-secret": declaration, "token": declaration},
        )
        running = (SimpleNamespace(assistant_id="assistant", contract=contract),)
        submission = action_human.StoredInputSubmission("app-secret", "private-value")
        admission = action_human.HumanResponseAdmission((), 1, submission)

        # An answer to an ordinary request has nothing to seal.
        with mock.patch.object(state._assistant_stored_inputs, "seal") as seal:
            human._seal_stored_input_answer(
                "team_1", challenge, pending, running, action_human.HumanResponseAdmission((), 1)
            )
        seal.assert_not_called()

        with (
            mock.patch.object(state._assistant_stored_inputs, "seal") as seal,
            mock.patch.object(human.audit, "log") as audit,
        ):
            human._seal_stored_input_answer("team_1", challenge, pending, running, admission)
        seal.assert_called_once_with(
            "team_1",
            "assistant",
            "app-secret",
            "password",
            "private-value",
            human.action_execution.stored_input_origin(paused),
        )
        audit.assert_called_once_with(
            "assistant_action",
            "team_1",
            result="ok",
            phase="stored-input-sealed",
            assistant="assistant",
            action="action",
            stored_input="app-secret",
        )
        self.assertNotIn("private-value", repr(audit.call_args_list))

        undeclared = (
            SimpleNamespace(
                assistant_id="assistant",
                contract=SimpleNamespace(
                    actions={"action": SimpleNamespace(stored_inputs=("token",))}, stored_inputs=contract.stored_inputs
                ),
            ),
        )
        unavailable = mock.Mock(side_effect=human.action_stored_input.StoredInputStoreError("private-value"))
        for assistants_now, sealer, status in (
            ((), mock.Mock(), 422),
            (undeclared, mock.Mock(), 422),
            (running, unavailable, 503),
        ):
            with (
                self.subTest(status=status, assistants=len(assistants_now)),
                mock.patch.object(state._assistant_stored_inputs, "seal", sealer),
                mock.patch.object(human.audit, "log") as audit,
                self.assertRaises(state.ApiError) as refused,
            ):
                human._seal_stored_input_answer("team_1", challenge, pending, assistants_now, admission)
            self.assertEqual(int(refused.exception.status), status)
            self.assertNotIn("private-value", refused.exception.message)
            audit.assert_not_called()
