from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import hosted_assistant_fixture as harness

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
            human._expire_challenges()

        pending = self.pending()
        expired = SimpleNamespace(payload=pending)
        with (
            mock.patch.object(state._human_challenges, "drain_expired", return_value=(expired,)),
            mock.patch.object(segment, "_purge_hosted_human_pending") as purge,
        ):
            human._expire_challenges()
        purge.assert_called_once_with(pending)

        with (
            mock.patch.object(human, "_expire_challenges"),
            mock.patch.object(state._human_challenges, "current", return_value=None),
        ):
            self.assertEqual(human.pending_chat_human("team_1")["status"], "none")

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
        expire.assert_called_once_with()

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
        with (
            mock.patch.object(segment, "_hosted_chat_setup", return_value=(*("unused",) * 6, ("changed",))),
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
            return_value=("unused",) * 6 + (("expected",),),
        ):
            self.assertIs(
                human._validate_pending_context("team_1", challenge, object(), "account_1"),
                challenge.payload,
            )

    def test_response_admission_handles_denial_assurance_and_schema_failures(self) -> None:
        pending = self.pending()
        request = SimpleNamespace(kind="approval")
        challenge = SimpleNamespace(
            id="challenge",
            requirement=SimpleNamespace(request=request, interrupt_id="interrupt"),
        )
        with self.assertRaises(state.ApiError):
            human._admit_response("team_1", challenge, pending, "deny", None, {"kind": "auth"})
        with mock.patch.object(state._human_challenges, "claim") as claim:
            self.assertIsNone(human._admit_response("team_1", challenge, pending, "deny", None, None))
        claim.assert_called_once_with("team_1", "challenge")

        self.assertIsNone(
            human._admit_response(
                "team_1",
                challenge,
                pending,
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
            human._admit_response("team_1", challenge, pending, "submit", True, None)

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
                mock.patch.object(human, "_validate_pending_context", return_value=pending),
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
            mock.patch.object(state._human_challenges, "current", return_value=None),
        ):
            self.assertFalse(human.cancel_pending("team_1"))
        with (
            mock.patch.object(human, "_expire_challenges"),
            mock.patch.object(state._human_challenges, "current", return_value=SimpleNamespace(payload=object())),
            self.assertRaises(AssertionError),
        ):
            human.cancel_pending("team_1")

        challenge = SimpleNamespace(payload=pending)
        with (
            mock.patch.object(human, "_expire_challenges"),
            mock.patch.object(state._human_challenges, "current", return_value=challenge),
            mock.patch.object(state._human_challenges, "cancel_team", return_value=True),
            mock.patch.object(segment, "_purge_hosted_human_pending") as purge,
        ):
            self.assertTrue(human.cancel_pending("team_1"))
        purge.assert_called_once_with(pending)


if __name__ == "__main__":
    unittest.main()
