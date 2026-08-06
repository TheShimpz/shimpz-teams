"""Hosted Owner-facing Power human-request suspension boundaries."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import hosted_assistant_fixture as harness

hosted_chat_segment = harness.hosted_chat_segment
hosted_chat_human = harness.hosted_chat_human
hosted_chat_lifecycle = harness.hosted_chat_lifecycle
runtime_state = harness.runtime_state
brain_runtime_client = runtime_state.brain_runtime_client
chat_orchestrator = hosted_chat_segment.chat_orchestrator
chat_turn_engine = hosted_chat_segment.chat_turn_engine
power_challenges = hosted_chat_segment.power_challenges
power_human = hosted_chat_segment.power_human


class HostedHumanRequestTests(unittest.TestCase):
    @staticmethod
    def _request(kind: str) -> power_human.HumanRequest:
        descriptor = {
            "kind": kind,
            "ordinal": 0,
            "title": "Confirm action",
            "description": "Confirm this reviewed action.",
        }
        descriptor["fingerprint"] = power_human._fingerprint(descriptor)
        return power_human.validate_request(descriptor, (kind,))

    @staticmethod
    def _pending(continuation, transcripts=()) -> object:
        return harness.hosted_assistants._PendingHostedChat(
            continuation,
            ("shimpz-cloudflare",),
            (),
            "account_1",
            ("container-1", "account_1", "Marketing"),
            transcripts,
        )

    def test_human_suspension_creates_one_owner_challenge(self) -> None:
        descriptor = {
            "kind": "approval",
            "ordinal": 0,
            "title": "Publish zone",
            "description": "Publish this reviewed DNS zone.",
        }
        descriptor["fingerprint"] = power_human._fingerprint(descriptor)
        request = power_human.validate_request(descriptor, ("approval",))
        power = brain_runtime_client.PowerRequest(
            "power-1",
            "shimpz-cloudflare",
            "list-zones",
            {"page": 1, "per_page": 25},
        )
        continuation = chat_orchestrator.ChatContinuation(
            brain_runtime_client.RuntimeTurn("power-required", "", (power,)),
            (),
            (),
            0,
        )
        segment = chat_turn_engine.SegmentResult(
            "Marketing",
            ("container-1", "account_1", "Marketing"),
            chat_orchestrator.ChatHumanSuspension(continuation, power, request),
            (),
            (
                power_challenges.HumanRequirement(
                    "shimpz-cloudflare",
                    "Shimpz Cloudflare",
                    "list-zones",
                    "List zones",
                    "power-1",
                    request,
                ),
            ),
        )
        challenges = power_challenges.HumanChallengeStore()

        with (
            mock.patch.object(runtime_state, "_human_challenges", challenges),
            mock.patch.object(runtime_state, "_commit_chat_terminal", return_value=True),
        ):
            response = hosted_chat_segment._hosted_segment_response(
                "team_1",
                "turn-token",
                segment,
                ("shimpz-cloudflare",),
                (),
                "account_1",
            )

        self.assertEqual(response["status"], "human-required")
        self.assertEqual(response["request"]["kind"], "approval")
        pending = challenges.current("team_1")
        self.assertIsNotNone(pending)
        self.assertEqual(pending.payload.owner, "account_1")

    def test_auth_response_requires_exact_account_assurance(self) -> None:
        request = self._request("auth:second-factor")
        continuation = SimpleNamespace()
        pending = self._pending(continuation)
        requirement = power_challenges.HumanRequirement(
            "shimpz-cloudflare",
            "Shimpz Cloudflare",
            "publish-zone",
            "Publish zone",
            "power-1",
            request,
        )
        challenges = power_challenges.HumanChallengeStore()
        challenge = challenges.create("team_1", requirement, pending)

        with mock.patch.object(runtime_state, "_human_challenges", challenges):
            denied = hosted_chat_human._admit_response(
                "team_1",
                challenge,
                pending,
                "submit",
                "opaque-account-handle",
                None,
            )
            admitted = hosted_chat_human._admit_response(
                "team_1",
                challenge,
                pending,
                "submit",
                "opaque-account-handle",
                {"kind": "auth:second-factor", "challenge_id": challenge.id},
            )

        self.assertIsNone(denied)
        self.assertEqual(admitted[0].responses[0].value, True)
        self.assertIsNone(challenges.current("team_1"))

    def test_resume_replays_only_the_admitted_boolean_auth_result(self) -> None:
        request = self._request("auth:reauth")
        power = brain_runtime_client.PowerRequest(
            "power-1",
            "shimpz-cloudflare",
            "publish-zone",
            {},
        )
        continuation = chat_orchestrator.ChatContinuation(
            brain_runtime_client.RuntimeTurn("power-required", "", (power,)),
            (),
            (),
            0,
        )
        pending = self._pending(continuation)
        requirement = power_challenges.HumanRequirement(
            "shimpz-cloudflare",
            "Shimpz Cloudflare",
            "publish-zone",
            "Publish zone",
            "power-1",
            request,
        )
        challenges = power_challenges.HumanChallengeStore()
        challenge = challenges.create("team_1", requirement, pending)
        lease = SimpleNamespace(owner="account_1")

        @contextmanager
        def exclusive(_team_id, _lease):
            yield "turn-token", SimpleNamespace(id="container-1")

        segment = SimpleNamespace()
        with (
            mock.patch.object(runtime_state, "_human_challenges", challenges),
            mock.patch.object(hosted_chat_human, "_validate_pending_context", return_value=pending),
            mock.patch.object(hosted_chat_segment, "_run_hosted_chat_segment", return_value=segment) as run,
            mock.patch.object(
                hosted_chat_segment,
                "_hosted_segment_response",
                return_value={"reply": "done"},
            ) as respond,
        ):
            result = hosted_chat_human.resume_chat_human(
                "team_1",
                {
                    "challenge_id": challenge.id,
                    "decision": "submit",
                    "value": "opaque-account-handle",
                },
                {"kind": "auth:reauth", "challenge_id": challenge.id},
                lease,
                exclusive,
            )

        transcripts = run.call_args.args[0].transcripts
        self.assertEqual(transcripts[0].responses[0].value, True)
        self.assertNotEqual(transcripts[0].responses[0].value, "opaque-account-handle")
        self.assertEqual(respond.call_args.args[-1], transcripts)
        self.assertEqual(result, {"reply": "done"})

    def test_lifecycle_change_cancels_challenge_and_purges_replayable_state(self) -> None:
        request = self._request("approval")
        pending = self._pending(SimpleNamespace())
        requirement = power_challenges.HumanRequirement(
            "shimpz-cloudflare",
            "Shimpz Cloudflare",
            "publish-zone",
            "Publish zone",
            "power-1",
            request,
        )
        challenges = power_challenges.HumanChallengeStore()
        challenges.create("team_1", requirement, pending)
        journal = mock.Mock()

        with (
            mock.patch.object(runtime_state, "_human_challenges", challenges),
            mock.patch.object(runtime_state, "_power_execution_journal", return_value=journal),
        ):
            cancelled = hosted_chat_lifecycle.cancel_replayable_human("team_1", "container-1")

        self.assertTrue(cancelled)
        self.assertIsNone(challenges.current("team_1"))
        journal.purge_replayable.assert_called_once_with("container-1")


if __name__ == "__main__":
    unittest.main()
