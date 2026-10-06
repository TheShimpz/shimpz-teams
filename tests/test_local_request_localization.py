"""A Local turn's human requests render in the language its start pinned, from the binding they bind (ADR-0091)."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LOOKUP_INPUT, LOOKUP_RESULT, LocalContractCase, chat_body

from action import challenges as action_challenges
from action import human as action_human
from inference import client as brain_runtime_client
from local import app as local_app
from tests import human_request_fixtures

PURPOSE = "To list your zones, I need to read them in Cloudflare."
CHAT = chat_body("List zones", assistant_ids=["shimpz-cloudflare"])


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


class LocalChatChallengeRelocalizationTests(LocalContractCase):
    """A pending chat request follows the interface language with Routine's fresh-challenge semantics (ADR-0091)."""

    _controller = LocalRequestLocalizationTests._controller
    _submit = LocalRequestLocalizationTests._submit

    def _paused(self, directory: str, runtime: _Runtime, locale: str = "fr"):
        approval = human_request_fixtures.request(
            "approval", title="List zones", description="Allow listing the zones."
        )
        controller = self._controller(directory, runtime, [approval])
        paused = controller.chat_turn_service.chat("team_1", {**CHAT, "locale": locale}, "openai", "sk-test-0123456789")
        return controller, paused

    def test_a_chat_in_another_language_reopens_the_pending_request_as_a_fresh_challenge(self) -> None:
        runtime = _Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller, first = self._paused(directory, runtime)
            service = controller.chat_turn_service
            same = service.chat("team_1", {**CHAT, "locale": "fr"}, "openai", "sk-test-0123456789")
            reopened = service.chat("team_1", {**CHAT, "locale": "pt"}, "openai", "sk-test-0123456789")
            with self.assertRaises(local_app.ApiProblem) as stale:
                self._submit(controller, first)
            kept = service.chat("team_1", {**CHAT, "locale": None}, "openai", "sk-test-0123456789")
            completed = self._submit(controller, reopened)

        self.assertEqual(same["challenge_id"], first["challenge_id"])
        self.assertNotEqual(reopened["challenge_id"], first["challenge_id"])
        self.assertEqual((reopened["locale"], reopened["rendered"]["title"]), ("pt", "PT List zones"))
        # The canonical request and its fingerprint never depend on the display language.
        self.assertEqual(reopened["request"], first["request"])
        # The purpose was written in French, so a Portuguese challenge shows only the localized scope.
        self.assertEqual(first["purpose"], PURPOSE)
        self.assertNotIn("purpose", reopened)
        self.assertLessEqual(reopened["expires_in"], first["expires_in"])
        self.assertEqual(stale.exception.code, "human-request-expired")
        self.assertEqual(kept["challenge_id"], reopened["challenge_id"])
        self.assertEqual(completed["reply"], "Listed")
        self.assertEqual(runtime.purposes, 1)

    def test_opening_in_a_language_restores_the_purpose_of_its_own_locale_and_survives_restart(self) -> None:
        runtime = _Runtime()
        with tempfile.TemporaryDirectory() as directory:
            controller, first = self._paused(directory, runtime)
            service = controller.chat_turn_service
            german = service.open_chat_human("team_1", {"locale": "de"})
            unchanged = service.open_chat_human("team_1", {"locale": "de"})
            french = service.open_chat_human("team_1", {"locale": "fr"})
            restarted = self._controller(directory, runtime, [])
            restored = restarted.chat_turn_service.open_chat_human("team_1", {"locale": "fr"})
            relocalized = restarted.chat_turn_service.open_chat_human("team_1", {"locale": "ja"})

        self.assertEqual((german["locale"], german["rendered"]["title"]), ("de", "DE List zones"))
        self.assertNotIn("purpose", german)
        self.assertEqual(unchanged["challenge_id"], german["challenge_id"])
        self.assertEqual((french["locale"], french["purpose"]), ("fr", PURPOSE))
        self.assertEqual(len({first["challenge_id"], german["challenge_id"], french["challenge_id"]}), 3)
        # A restored continuation is the latest fresh challenge in its own language.
        self.assertEqual((restored["challenge_id"], restored["locale"]), (french["challenge_id"], "fr"))
        self.assertEqual((relocalized["locale"], relocalized["rendered"]["title"]), ("ja", "JA List zones"))
        self.assertEqual(relocalized["request"], first["request"])

    def test_opening_refuses_a_body_other_than_one_locale_and_reports_no_pending_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, _Runtime(), [])
            service = controller.chat_turn_service
            for body in ({}, {"locale": None}, {"locale": "xx"}, {"locale": "pt", "extra": True}, []):
                with self.subTest(body=body), self.assertRaises(local_app.ApiProblem) as invalid:
                    service.open_chat_human("team_1", body)
                self.assertEqual(invalid.exception.code, "invalid-body")
            empty = service.open_chat_human("team_1", {"locale": "pt"})

        self.assertEqual(empty, {"team_id": "team_1", "status": "none"})

    def test_opening_against_a_binding_with_another_pack_ends_the_paused_turn(self) -> None:
        # Another language, the language the challenge is already in, and a chat in either: none keeps the challenge.
        reopenings = {
            "open another language": lambda service: service.open_chat_human("team_1", {"locale": "pt"}),
            "open the same language": lambda service: service.open_chat_human("team_1", {"locale": "fr"}),
            "chat in another language": lambda service: service.chat(
                "team_1", {**CHAT, "locale": "pt"}, "openai", "sk-test-0123456789"
            ),
            "chat in the same language": lambda service: service.chat(
                "team_1", {**CHAT, "locale": "fr"}, "openai", "sk-test-0123456789"
            ),
            "chat without a language": lambda service: service.chat(
                "team_1", {**CHAT, "locale": None}, "openai", "sk-test-0123456789"
            ),
        }
        for name, reopen in reopenings.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                controller, first = self._paused(directory, _Runtime())
                spec = controller.registry["shimpz-cloudflare"]
                controller.registry["shimpz-cloudflare"] = replace(spec, pack_digest=f"sha256:{'9' * 64}")
                with self.assertRaises(local_app.ApiProblem) as changed:
                    reopen(controller.chat_turn_service)
                pending = controller.chat_turn_service.human_challenges.current("team_1")
                stored = controller.chat_continuations.current("team_1")
                with self.assertRaises(local_app.ApiProblem) as stale:
                    self._submit(controller, first)

                self.assertEqual(changed.exception.code, "team-context-changed")
                self.assertIsNone(pending)
                self.assertIsNone(stored)
                self.assertEqual(stale.exception.code, "human-request-expired")

    def test_a_request_that_cannot_be_reopened_keeps_or_ends_the_pending_turn_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, first = self._paused(directory, _Runtime())
            service = controller.chat_turn_service
            failures = (
                (
                    mock.patch.object(
                        action_challenges, "relocalize", side_effect=action_challenges.HumanChallengeError
                    ),
                    "human-request-invalid",
                ),
                (
                    mock.patch.object(
                        service.human_challenges, "reissue", side_effect=action_challenges.HumanChallengeNotFoundError
                    ),
                    "human-request-expired",
                ),
            )
            for patch, code in failures:
                with self.subTest(code=code), patch, self.assertRaises(local_app.ApiProblem) as refused:
                    service.open_chat_human("team_1", {"locale": "pt"})
                self.assertEqual(refused.exception.code, code)
            # A request that cannot be re-rendered or replaced keeps its challenge and continuation.
            kept = service.open_chat_human("team_1", {"locale": "fr"})
            unavailable = local_app.ApiProblem(503, "Team chat continuation state is unavailable", code="chat-state")
            with (
                mock.patch.object(service, "_persist_chat_continuation", side_effect=unavailable),
                self.assertRaises(local_app.ApiProblem) as unsaved,
            ):
                service.open_chat_human("team_1", {"locale": "pt"})
            pending = service.human_challenges.current("team_1")
            stored = controller.chat_continuations.current("team_1")

        self.assertEqual(kept["challenge_id"], first["challenge_id"])
        # A fresh challenge that cannot be kept ends the paused turn rather than leaving the earlier one answerable.
        self.assertIs(unsaved.exception, unavailable)
        self.assertIsNone(pending)
        self.assertIsNone(stored)
