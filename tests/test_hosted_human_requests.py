"""Hosted Owner-facing Power human-request suspension boundaries."""

from __future__ import annotations

import importlib
import unittest
from unittest import mock

from power import challenges as power_challenges
from power import human as power_human

harness = importlib.import_module("hosted_assistant_fixture")
hosted_chat_segment = harness.hosted_chat_segment
runtime_state = harness.runtime_state
brain_runtime_client = runtime_state.brain_runtime_client
chat_orchestrator = hosted_chat_segment.chat_orchestrator
chat_turn_engine = hosted_chat_segment.chat_turn_engine


class HostedHumanRequestTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
