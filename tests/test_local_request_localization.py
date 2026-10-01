"""A Local turn's human requests render in the language its start pinned, from the binding they bind (ADR-0091)."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LocalContractCase

from action import human as action_human
from inference import client as brain_runtime_client
from local import app as local_app
from tests import human_request_fixtures

LOOKUP_INPUT = {"page": 1, "per_page": 25}
LOOKUP_RESULT = {
    "zones": [],
    "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0},
}
PURPOSE = "To list your zones, I need to read them in Cloudflare."
CHAT = {"message": "List zones", "files": [], "assistant_ids": ["shimpz-cloudflare"], "conversation": []}


class _Runtime:
    def __init__(self) -> None:
        self.purposes = 0
        self.request = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)

    def start(self, _context, _message, *, conversation=()):
        return brain_runtime_client.RuntimeTurn("action-required", "", (self.request,))

    def purpose(self, *_args):
        self.purposes += 1
        return PURPOSE

    def resume(self, _context, _results):
        return brain_runtime_client.RuntimeTurn("completed", "Listed", ())


class LocalRequestLocalizationTests(LocalContractCase):
    def _controller(self, directory: str, runtime: _Runtime, requests: list[action_human.HumanRequest]):
        controller = self._chat_controller(directory, runtime)

        def invoke(*args):
            responses = args[4].transcript.responses
            if len(responses) < len(requests):
                raise action_human.HumanRequestSuspensionError(requests[len(responses)])
            return {"result": LOOKUP_RESULT}

        controller.assistant_lifecycle.invoke = invoke
        return controller

    def _submit(self, controller, paused, value=True):
        return controller.chat_turn_service.resume_chat_human(
            "team_1",
            {"challenge_id": paused["challenge_id"], "decision": "submit", "value": value},
            "openai",
            "sk-test-0123456789",
        )

    def test_a_later_request_of_a_resumed_turn_renders_in_the_language_its_start_pinned(self) -> None:
        approval = human_request_fixtures.request(
            "approval", title="List zones", description="Allow listing the zones."
        )
        zone = human_request_fixtures.request(
            "input:text",
            1,
            title="Zone",
            description="Enter the reviewed zone.",
            label="Zone",
            required=True,
            placeholder="example.com",
            min_length=1,
            max_length=64,
        )
        runtime = _Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, runtime, [approval, zone])
            first = controller.chat_turn_service.chat(
                "team_1", {**CHAT, "locale": "fr"}, "openai", "sk-test-0123456789"
            )
            second = self._submit(controller, first)
            completed = self._submit(controller, second, "example.com")

        self.assertEqual(
            (first["locale"], first["rendered"]["title"], first["purpose"]), ("fr", "FR List zones", PURPOSE)
        )
        self.assertEqual(second["status"], "human-required")
        self.assertEqual(second["locale"], "fr")
        self.assertEqual(
            second["rendered"],
            {
                "title": "FR Zone",
                "description": "FR Enter the reviewed zone.",
                "label": "FR Zone",
                "placeholder": "FR example.com",
            },
        )
        self.assertEqual(second["purpose"], PURPOSE)
        self.assertEqual(completed["reply"], "Listed")

    def test_a_turn_without_an_interface_language_renders_english_without_a_purpose(self) -> None:
        approval = human_request_fixtures.request(
            "approval", title="List zones", description="Allow listing the zones."
        )
        runtime = _Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, runtime, [approval])
            paused = controller.chat_turn_service.chat(
                "team_1", {**CHAT, "locale": None}, "openai", "sk-test-0123456789"
            )

        self.assertEqual((paused["locale"], paused["rendered"]["title"]), ("en", "List zones"))
        self.assertNotIn("purpose", paused)
        self.assertEqual(runtime.purposes, 0)

    def test_resuming_against_a_binding_with_another_pack_needs_a_fresh_turn(self) -> None:
        approval = human_request_fixtures.request(
            "approval", title="List zones", description="Allow listing the zones."
        )
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, _Runtime(), [approval])
            paused = controller.chat_turn_service.chat(
                "team_1", {**CHAT, "locale": "de"}, "openai", "sk-test-0123456789"
            )
            spec = controller.registry["shimpz-cloudflare"]
            controller.registry["shimpz-cloudflare"] = replace(spec, pack_digest=f"sha256:{'9' * 64}")
            with self.assertRaises(local_app.ApiProblem) as changed:
                self._submit(controller, paused)
            self.assertIsNone(controller.chat_turn_service.human_challenges.current("team_1"))

        self.assertEqual(changed.exception.code, "team-context-changed")
