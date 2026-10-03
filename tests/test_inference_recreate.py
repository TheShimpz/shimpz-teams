"""Recriar's compile request and its closed answer (ADR-0092 amendment, 2026-10-02)."""

from __future__ import annotations

import unittest

from inference import client as brain_runtime_client
from inference import recreate

ASSISTANT = brain_runtime_client.RuntimeAssistant(
    "shimpz-cloudflare",
    "Manages DNS.",
    (brain_runtime_client.RuntimeAction("list-zones", "List zones.", {"type": "object"}),),
)
CREDENTIALS = ("openai", "gpt-6.1-sol", "sk-test-0123456789")
CHANGE = {"op": "create", "routine_id": None}
CLARIFICATION = {
    "question": "Quantas por página?",
    "options": [{"label": "25", "description": ""}, {"label": "50", "description": ""}],
    "default_index": 0,
}


class Client:
    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.sent: list[tuple[dict[str, object], str, str]] = []

    def routine_compile(self, payload, provider, model):
        self.sent.append((payload, provider, model))
        return self.answer


def _answer(**changes: object) -> dict[str, object]:
    return {"routine": CHANGE, "reply": "Pronto.", "clarification": None, "refusal": None, **changes}


class RecreateCompileTests(unittest.TestCase):
    def test_only_the_message_and_current_contracts_are_sent_and_the_answer_is_closed(self) -> None:
        client = Client(_answer())
        compiled = recreate.compile_routine(client, CREDENTIALS, "Todo dia às 9h, liste as zonas", (ASSISTANT,))
        self.assertEqual((compiled.routine, compiled.clarification), (CHANGE, None))
        ((payload, provider, model),) = client.sent
        self.assertEqual((provider, model), CREDENTIALS[:2])
        self.assertEqual(set(payload), {"provider", "locale", "message", "assistants"})
        self.assertEqual(payload["assistants"][0]["actions"][0]["id"], "list-zones")
        asked = recreate.compile_routine(
            Client(_answer(routine={**CHANGE, "question": {}}, clarification=CLARIFICATION)),
            CREDENTIALS,
            "x",
            (ASSISTANT,),
        )
        self.assertEqual(asked.clarification, CLARIFICATION)
        refused = _answer(routine=None, reply=None, refusal="unsupported")
        self.assertEqual(recreate.compile_routine(Client(refused), CREDENTIALS, "x", (ASSISTANT,)), "unsupported")

    def test_an_invalid_request_or_answer_fails_closed(self) -> None:
        for credentials, message, assistants in (
            (("other", "m", "k"), "x", (ASSISTANT,)),
            (CREDENTIALS, "", (ASSISTANT,)),
            (CREDENTIALS, "x" * (recreate.MAX_MESSAGE_CHARS + 1), (ASSISTANT,)),
            (CREDENTIALS, "x", ()),
        ):
            with self.subTest(message=message[:3]), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                recreate.compile_routine(Client(_answer()), credentials, message, assistants)
        for answer in (
            None,
            {**_answer(), "extra": 1},
            _answer(refusal="maybe", routine=None, reply=None),
            _answer(refusal="unsupported"),
            _answer(clarification=CLARIFICATION),
            _answer(routine={**CHANGE, "question": {}}),
            _answer(routine={**CHANGE, "question": {}}, clarification={"question": "?"}),
        ):
            with self.subTest(answer=answer), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                recreate.compile_routine(Client(answer), CREDENTIALS, "x", (ASSISTANT,))


if __name__ == "__main__":
    unittest.main()
