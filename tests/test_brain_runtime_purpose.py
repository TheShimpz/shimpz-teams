"""Brain runtime client contracts for the turn locale and the optional human-request purpose (ADR-0090)."""

from __future__ import annotations

import json
import threading
import unittest
from dataclasses import replace
from unittest import mock

from test_brain_runtime_client import RuntimeClientCase, _Connection, _Response, context

from inference import client as brain_runtime_client

REQUEST = brain_runtime_client.ActionRequest("interrupt-1", "shimpz-exa", "search-web", {"query": "AI news"})
PURPOSE = "Para trazer as notícias de IA de hoje, preciso pesquisar na web com o Exa."
COMPLETED = {"status": "completed", "reply": "Hi.", "actions": [], "clarification": None}


class TurnLocaleTests(RuntimeClientCase):
    def test_only_a_start_carries_the_interface_locale(self):
        client, connection = self.client(_Response(COMPLETED))
        client.start(replace(context(self.secret), locale="pt"), "Oi", conversation=())
        self.assertEqual(json.loads(connection.requests[0][2])["locale"], "pt")

        client, connection = self.client(_Response(COMPLETED))
        client.start(context(self.secret), "Hi", conversation=())
        self.assertIsNone(json.loads(connection.requests[0][2])["locale"])

        client, connection = self.client(_Response(COMPLETED))
        client.resume(replace(context(self.secret), locale="pt"), {"interrupt-1": {"ok": True}})
        self.assertNotIn("locale", json.loads(connection.requests[0][2]))

    def test_a_locale_outside_the_closed_languages_never_reaches_the_brain(self):
        for locale in ("pt-BR", "", "EN"):
            with self.subTest(locale=locale):
                client, connection = self.client(_Response(COMPLETED))
                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.start(replace(context(self.secret), locale=locale), "Oi", conversation=())
                self.assertEqual(connection.requests, [])


class PurposeTests(RuntimeClientCase):
    def test_asks_for_one_exact_pending_interrupt_and_returns_its_sentence(self):
        client, connection = self.client(_Response({"purpose": PURPOSE}))

        purpose = client.purpose(context(self.secret), REQUEST, "Exa", "Search the web with Exa.")

        self.assertEqual(purpose, PURPOSE)
        method, path, raw_body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/turns/purpose"))
        self.assertEqual(headers["Authorization"], f"Bearer {self.token}")
        self.assertEqual(
            json.loads(raw_body),
            {
                "thread_id": "team:hello-pulse:conversation-1",
                "interrupt_id": "interrupt-1",
                "assistant_id": "shimpz-exa",
                "assistant_name": "Exa",
                "action_id": "search-web",
                "action_summary": "Search the web with Exa.",
                "provider": {"provider": "openai", "model": "gpt-test", "api_key": self.secret},
            },
        )
        self.assertTrue(connection.closed)

    def test_any_failure_or_invalid_sentence_leaves_the_purpose_absent(self):
        responses = (
            _Response({"purpose": None}),
            _Response({"purpose": "Search — then read"}),
            _Response({"purpose": "Visit https://example.com"}),
            _Response({"purpose": PURPOSE, "extra": True}),
            _Response({"purpose": PURPOSE}, status=502),
            _Response({"purpose": PURPOSE}, usage=None),
            _Response(["not", "an", "object"]),
        )
        for response in responses:
            with self.subTest(response=response._raw):
                client, _connection = self.client(response)
                self.assertIsNone(client.purpose(context(self.secret), REQUEST, "Exa", "Search the web."))

    def test_an_overall_deadline_disconnects_a_brain_that_never_answers(self):
        shutdown = threading.Event()

        class Socket:
            @staticmethod
            def settimeout(_seconds):
                return None

            @staticmethod
            def shutdown(_how):
                shutdown.set()

        class Hanging(_Connection):
            def connect(self) -> None:
                self.sock = Socket()

            def getresponse(self):
                if not shutdown.wait(5):
                    raise AssertionError("the purpose deadline never fired")
                raise OSError("connection shut down")

        connection = Hanging(_Response({"purpose": PURPOSE}))
        client = brain_runtime_client.BrainRuntimeClient(
            base_url="http://brain-runtime:8080",
            token_file=self.token_file,
            connection_factory=lambda _host, _port, _timeout: connection,
        )
        with mock.patch.object(brain_runtime_client, "PURPOSE_DEADLINE_SECONDS", 0.05):
            self.assertIsNone(client.purpose(context(self.secret), REQUEST, "Exa", "Search the web."))
        self.assertTrue(shutdown.is_set())
        self.assertTrue(connection.closed)

    def test_a_request_attached_after_its_deadline_fails_before_sending(self):
        expired = brain_runtime_client.RequestAbort()
        expired.abort()
        client, connection = self.client(_Response({"purpose": PURPOSE}))
        with mock.patch.object(brain_runtime_client, "RequestAbort", return_value=expired):
            self.assertIsNone(client.purpose(context(self.secret), REQUEST, "Exa", "Search the web."))
        self.assertEqual(connection.requests, [])


if __name__ == "__main__":
    unittest.main()
