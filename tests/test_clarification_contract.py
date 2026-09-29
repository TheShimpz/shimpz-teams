"""The Brain clarification crosses Team only as its exact closed shape (ADR-0081)."""

from __future__ import annotations

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

    def test_a_completed_turn_hands_its_clarification_to_the_outcome(self):
        class Runtime:
            def start(self, _context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn("completed", "Qual período?", (), clarification=ASKED)

        outcome = chat_orchestrator.run(
            Runtime(),
            context("sk-test-0123456789abcdef"),
            "Quais modelos saíram?",
            chat_orchestrator.ChatStrategy(lambda _a, _b, value: value, lambda _request: {}),
        )
        self.assertEqual(outcome.clarification, ASKED)
        self.assertEqual(outcome.reply, "Qual período?")


if __name__ == "__main__":
    unittest.main()
