"""Hosted fail-closed boundaries before Owner human-request support is admitted."""

from __future__ import annotations

import unittest
from http import HTTPStatus

from hosted_assistant_fixture import hosted_chat_segment, runtime_state

from power import human as power_human

brain_runtime_client = runtime_state.brain_runtime_client
chat_orchestrator = hosted_chat_segment.chat_orchestrator
chat_turn_engine = hosted_chat_segment.chat_turn_engine


class HostedHumanRequestTests(unittest.TestCase):
    def test_human_suspension_fails_closed_before_a_challenge_is_created(self) -> None:
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
            ("identity",),
            chat_orchestrator.ChatHumanSuspension(continuation, power, request),
            (),
            (object(),),
        )

        with self.assertRaises(runtime_state.ApiError) as caught:
            hosted_chat_segment._hosted_segment_response(
                "team_1",
                "turn-token",
                segment,
                ("shimpz-cloudflare",),
                (),
                "account_1",
            )

        self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(caught.exception.message, "Power human requests are unavailable")


if __name__ == "__main__":
    unittest.main()
