"""The Brain clarification crosses Team only as its exact closed shape (ADR-0081)."""

import json
import unittest
from pathlib import Path

from test_brain_runtime_client import context

from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from protocol.http.v1 import payload

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "protocol/http/v1/vectors.json").read_text())
ASKED = VECTORS["clarification"]["valid"][0]


class ClarificationContractTests(unittest.TestCase):
    def test_every_golden_vector_has_its_expected_outcome(self):
        for value in VECTORS["clarification"]["valid"]:
            with self.subTest(value=value):
                self.assertEqual(payload.canonical_clarification(value), value)
        for value in VECTORS["clarification"]["invalid"]:
            with self.subTest(value=value):
                self.assertIsNone(payload.canonical_clarification(value))
        self.assertIsNone(payload.canonical_clarification({**ASKED, "options": ["Hoje", "Semana"]}))
        self.assertIsNone(payload.canonical_clarification({**ASKED, "options": "Hoje"}))

    def test_a_question_that_steers_no_choice_has_a_null_default_and_marks_none(self):
        unsteered = {**ASKED, "default_index": None}
        self.assertEqual(payload.canonical_clarification(unsteered), unsteered)
        self.assertNotIn("✓", payload.render_clarification(unsteered))
        self.assertIsNone(payload.canonical_clarification({**ASKED, "default_index": False}))

    def test_the_reply_is_the_exact_golden_rendering(self):
        for value, rendered in zip(
            VECTORS["clarification"]["valid"], VECTORS["clarification"]["rendered"], strict=True
        ):
            self.assertEqual(payload.render_clarification(value), rendered)

    def test_a_clarification_after_an_action_round_is_refused(self):
        asked = brain_runtime_client.RuntimeTurn(
            "completed", payload.render_clarification(ASKED), (), clarification=ASKED
        )
        action = brain_runtime_client.ActionRequest("i-1", "hello-pulse", "hello", {})

        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("action-required", "", (action,))

            def resume(self, _context, _results):
                return asked

        with self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "after Actions ran"):
            chat_orchestrator.run(
                Runtime(),
                context("sk-test-0123456789abcdef"),
                "Cumprimente Ada",
                chat_orchestrator.ChatStrategy(lambda _a, _b, value: value, lambda _request: {"message": "hi"}),
            )

    def test_a_completed_turn_hands_its_clarification_to_the_outcome(self):
        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn(
                    "completed", payload.render_clarification(ASKED), (), clarification=ASKED
                )

        outcome = chat_orchestrator.run(
            Runtime(),
            context("sk-test-0123456789abcdef"),
            "Quais modelos saíram?",
            chat_orchestrator.ChatStrategy(lambda _a, _b, value: value, lambda _request: {}),
        )
        self.assertEqual(outcome.clarification, ASKED)
        self.assertEqual(outcome.reply, payload.render_clarification(ASKED))


if __name__ == "__main__":
    unittest.main()
