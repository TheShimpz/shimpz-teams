"""Recriar's compile request and its closed answer (ADR-0092 amendment, 2026-10-02)."""

from __future__ import annotations

import unittest
from unittest import mock

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
    "default_index": None,
}


def _said(text: str) -> tuple[tuple[str, str], ...]:
    return (("said", text),)


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
        compiled = recreate.compile_routine(client, CREDENTIALS, _said("Todo dia às 9h, liste as zonas"), (ASSISTANT,))
        self.assertEqual((compiled.routine, compiled.clarification), (CHANGE, None))
        ((payload, provider, model),) = client.sent
        self.assertEqual((provider, model), CREDENTIALS[:2])
        self.assertEqual(set(payload), {"provider", "locale", "message", "draft", "assistants"})
        self.assertEqual(payload["assistants"][0]["actions"][0]["id"], "list-zones")
        self.assertEqual((payload["message"], payload["draft"]), ("Todo dia às 9h, liste as zonas", []))
        # Every sealed part before the last goes as the compile's draft, kinds included, exactly and in order.
        cited = Client(_answer())
        words = (("cited", "liste as zonas"), ("said", "faça isso a cada 30 segundos"), ("said", "Até 100 por dia"))
        recreate.compile_routine(cited, CREDENTIALS, words, (ASSISTANT,))
        self.assertEqual(cited.sent[0][0]["message"], "Até 100 por dia")
        self.assertEqual(
            cited.sent[0][0]["draft"],
            [{"kind": "cited", "text": "liste as zonas"}, {"kind": "said", "text": "faça isso a cada 30 segundos"}],
        )
        asked = recreate.compile_routine(
            Client(_answer(routine={**CHANGE, "question": {}}, clarification=CLARIFICATION)),
            CREDENTIALS,
            _said("x"),
            (ASSISTANT,),
        )
        self.assertEqual(asked.clarification, CLARIFICATION)
        # Every closed reason the Brain compiler refuses with, including a timing outside the Routine contract.
        for reason in ("unsupported", "schedule"):
            refused = _answer(routine=None, reply=None, refusal=reason)
            with self.subTest(reason=reason):
                self.assertEqual(
                    recreate.compile_routine(Client(refused), CREDENTIALS, _said("x"), (ASSISTANT,)), reason
                )

    def test_an_invalid_request_or_answer_fails_closed(self) -> None:
        for credentials, words, assistants in (
            (("other", "m", "k"), _said("x"), (ASSISTANT,)),
            (CREDENTIALS, _said(""), (ASSISTANT,)),
            (CREDENTIALS, _said("x" * 16_001), (ASSISTANT,)),
            (CREDENTIALS, _said("x"), ()),
            (CREDENTIALS, (), (ASSISTANT,)),
            (CREDENTIALS, (("cited", "x"),), (ASSISTANT,)),
            (CREDENTIALS, (("said", "x"),) * 14, (ASSISTANT,)),
        ):
            with self.subTest(words=str(words)[:20]), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                recreate.compile_routine(Client(_answer()), credentials, words, assistants)
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
                recreate.compile_routine(Client(answer), CREDENTIALS, _said("x"), (ASSISTANT,))


class RuntimeClientCompileTests(unittest.TestCase):
    def client(self, answer: object):
        client = brain_runtime_client.BrainRuntimeClient.__new__(brain_runtime_client.BrainRuntimeClient)
        client._post = mock.Mock(return_value=answer)
        return client

    def test_the_compile_posts_to_its_route_and_is_metered_before_it_is_admitted(self) -> None:
        usage = dict.fromkeys(brain_runtime_client.brain_usage.FIELDS, 0)
        client = self.client({**_answer(), "usage": usage})
        with mock.patch.object(brain_runtime_client.brain_usage, "record") as metered:
            compiled = recreate.compile_routine(client, CREDENTIALS, _said("x"), (ASSISTANT,))
        self.assertEqual(compiled.routine, CHANGE)
        self.assertEqual(client._post.call_args.args[0], "/v1/routine-compile")
        metered.assert_called_once_with("routine-compile", "openai", "gpt-6.1-sol", mock.ANY)
        for answer in (_answer(), {**_answer(), "usage": {"input_tokens": "x"}}):
            with (
                self.subTest(answer=answer),
                mock.patch.object(brain_runtime_client.brain_usage, "record") as unmetered,
                self.assertRaises(brain_runtime_client.BrainRuntimeError),
            ):
                recreate.compile_routine(self.client(answer), CREDENTIALS, _said("x"), (ASSISTANT,))
            unmetered.assert_not_called()


if __name__ == "__main__":
    unittest.main()
