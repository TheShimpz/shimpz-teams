"""Brain runtime client contracts for stateless structured Assistant lifecycle routing."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from test_brain_runtime_client import RuntimeClientCase, _Response, directory_candidates

from inference import client as brain_runtime_client


class BrainRuntimeIntentRouteTests(RuntimeClientCase):
    def test_intent_route_uses_only_the_stateless_bounded_endpoint(self):
        client, connection = self.client(
            _Response(
                {
                    "task_follows": False,
                    "intent": "assistant-uninstall",
                    "query": "",
                    "assistant_ids": ["shimpz-cloudflare"],
                    "reply": "",
                }
            )
        )

        route = client.intent_route(
            provider="openai",
            model="gpt-6-sol",
            api_key=self.secret,
            objective="tire o cloudflare",
            expected_intent="assistant-uninstall",
            candidates=directory_candidates(uninstall=True),
            context=None,
        )

        self.assertEqual(
            route,
            brain_runtime_client.RuntimeIntentRoute(
                "assistant-uninstall",
                assistant_ids=("shimpz-cloudflare",),
            ),
        )
        method, path, raw_body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/intent-route"))
        self.assertEqual(headers["Authorization"], f"Bearer {self.token}")
        payload = json.loads(raw_body)
        self.assertEqual(payload["provider"]["api_key"], self.secret)
        self.assertEqual(payload["expected_intent"], "assistant-uninstall")
        self.assertEqual(
            [item["id"] for item in payload["candidates"]],
            [
                "shimpz-cloudflare",
                "shimpz-whatsapp",
            ],
        )
        self.assertTrue(all(item["summary"] == "" for item in payload["candidates"]))
        self.assertNotIn("thread_id", payload)

    def test_intent_route_task_continuation_is_a_strict_install_classification_flag(self):
        parse = brain_runtime_client.BrainRuntimeClient._parse_intent_route

        def route(intent="assistant-install", query="exa", follows=True, reply="", ids=()):
            return {
                "intent": intent,
                "query": query,
                "assistant_ids": list(ids),
                "reply": reply,
                "task_follows": follows,
            }

        self.assertTrue(parse(route(), None, ()).task_follows)
        self.assertFalse(parse(route(follows=False), None, ()).task_follows)
        candidates = (brain_runtime_client.RuntimeDirectoryCandidate("shimpz-exa", "Exa", "Search."),)
        for value, expected in (
            (route(follows=1), None),
            (route(follows=None), None),
            (route(intent="ordinary-task", query=""), None),
            (route(intent="assistant-uninstall"), None),
            (route(query="", reply="Which Assistant?"), None),
            (route(query="", ids=("shimpz-exa",)), "assistant-install"),
            ({key: item for key, item in route(follows=False).items() if key != "task_follows"}, None),
        ):
            with self.subTest(value=value), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                parse(value, expected, candidates)

    def test_intent_route_accepts_closed_classification_and_unresolved_results(self):
        client, connection = self.client(
            _Response({"task_follows": False, "intent": "ordinary-task", "query": "", "assistant_ids": [], "reply": ""})
        )
        self.assertEqual(
            client.intent_route(
                provider="openai",
                model="gpt-6-sol",
                api_key=self.secret,
                objective="liste minhas zonas",
                expected_intent=None,
                candidates=(),
                context=brain_runtime_client.RuntimeLifecycleContext(
                    reference=brain_runtime_client.RuntimeLifecycleReference(
                        "shimpz-cloudflare",
                        "Shimpz Cloudflare",
                    ),
                ),
            ),
            brain_runtime_client.RuntimeIntentRoute("ordinary-task"),
        )
        payload = json.loads(connection.requests[0][2])
        self.assertEqual(
            payload["lifecycle_reference"],
            {"id": "shimpz-cloudflare", "name": "Shimpz Cloudflare"},
        )

        clarification = "Which installed Assistant do you want to uninstall?"
        client, _connection = self.client(
            _Response(
                {
                    "task_follows": False,
                    "intent": "unresolved",
                    "query": "",
                    "assistant_ids": [],
                    "reply": clarification,
                }
            )
        )
        self.assertEqual(
            client.intent_route(
                provider="openai",
                model="gpt-6-sol",
                api_key=self.secret,
                objective="remove it",
                expected_intent="assistant-uninstall",
                candidates=directory_candidates(uninstall=True),
                context=None,
            ),
            brain_runtime_client.RuntimeIntentRoute("unresolved", reply=clarification),
        )

        client, _connection = self.client(
            _Response(
                {
                    "intent": "ordinary-task",
                    "query": "",
                    "assistant_ids": ["shimpz-cloudflare"],
                }
            )
        )
        with self.assertRaises(brain_runtime_client.BrainRuntimeError):
            client.intent_route(
                provider="openai",
                model="gpt-6-sol",
                api_key=self.secret,
                objective="liste minhas zonas",
                expected_intent=None,
                candidates=(),
                context=None,
            )

    def test_intent_route_carries_bounded_conversation_context(self):
        client, connection = self.client(
            _Response(
                {
                    "task_follows": False,
                    "intent": "assistant-uninstall",
                    "query": "cloudflare",
                    "assistant_ids": [],
                    "reply": "",
                }
            )
        )

        route = client.intent_route(
            provider="openai",
            model="gpt-6-sol",
            api_key=self.secret,
            objective="desinstala esse então",
            expected_intent=None,
            candidates=(),
            context=brain_runtime_client.RuntimeLifecycleContext(
                conversation=(
                    brain_runtime_client.RuntimeConversationEntry("user", "Quais temos?", False),
                    brain_runtime_client.RuntimeConversationEntry(
                        "assistant",
                        "Temos apenas Cloudflare/DNS.",
                        False,
                    ),
                ),
            ),
        )

        self.assertEqual(route, brain_runtime_client.RuntimeIntentRoute("assistant-uninstall", "cloudflare"))
        payload = json.loads(connection.requests[0][2])
        self.assertEqual(
            payload["conversation"],
            [
                {"role": "user", "text": "Quais temos?", "truncated": False},
                {
                    "role": "assistant",
                    "text": "Temos apenas Cloudflare/DNS.",
                    "truncated": False,
                },
            ],
        )
        self.assertIsNone(payload["language_exemplar"])

    def test_empty_directory_selection_returns_only_a_clarification(self):
        reply = "Qual Assistant instalado você quer desinstalar?"
        client, connection = self.client(
            _Response({"task_follows": False, "intent": "unresolved", "query": "", "assistant_ids": [], "reply": reply})
        )

        route = client.intent_route(
            provider="openai",
            model="gpt-6-sol",
            api_key=self.secret,
            objective="desinstale desconhecido",
            expected_intent="assistant-uninstall",
            candidates=(),
            context=brain_runtime_client.RuntimeLifecycleContext(
                language_exemplar="desinstale desconhecido",
            ),
        )

        self.assertEqual(route, brain_runtime_client.RuntimeIntentRoute("unresolved", reply=reply))
        self.assertEqual(json.loads(connection.requests[0][2])["candidates"], [])

    def test_intent_route_unicode_text_matches_the_browser_contract(self):
        reply = "Quel Assistant voulez-vous désinstaller\u00a0?"
        client, _connection = self.client(
            _Response({"task_follows": False, "intent": "unresolved", "query": "", "assistant_ids": [], "reply": reply})
        )
        route = client.intent_route(
            provider="openai",
            model="gpt-6-sol",
            api_key=self.secret,
            objective="désinstalle",
            expected_intent="assistant-uninstall",
            candidates=(),
            context=brain_runtime_client.RuntimeLifecycleContext(
                language_exemplar="desinstala 👩‍💻\r\nagora",
            ),
        )
        self.assertEqual(route.reply, reply)

        for separator in ("\u2028", "\u2029"):
            invalid = f"Question{separator}suivante"
            client, _connection = self.client(
                _Response(
                    {"task_follows": False, "intent": "unresolved", "query": "", "assistant_ids": [], "reply": invalid}
                )
            )
            with self.subTest(separator=separator), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client.intent_route(
                    provider="openai",
                    model="gpt-6-sol",
                    api_key=self.secret,
                    objective="désinstalle",
                    expected_intent="assistant-uninstall",
                    candidates=(),
                    context=None,
                )

    def test_intent_route_rejects_invalid_inputs_and_outputs_without_widening(self):
        invalid_outputs = (
            {"task_follows": False, "intent": "invalid", "query": "", "assistant_ids": [], "reply": ""},
            {
                "task_follows": False,
                "intent": "assistant-install",
                "query": "",
                "assistant_ids": ["unknown"],
                "reply": "",
            },
            {
                "task_follows": False,
                "intent": "assistant-install",
                "query": "",
                "assistant_ids": ["shimpz-whatsapp", "shimpz-cloudflare"],
                "reply": "",
            },
            {"task_follows": False, "intent": "assistant-uninstall", "query": "", "assistant_ids": [], "reply": ""},
            {"task_follows": False, "intent": "ordinary-task", "query": "cloudflare", "assistant_ids": [], "reply": ""},
            {
                "task_follows": False,
                "intent": "unresolved",
                "query": "",
                "assistant_ids": ["shimpz-cloudflare"],
                "reply": "clarify",
            },
            {
                "task_follows": False,
                "intent": "ordinary-task",
                "query": "",
                "assistant_ids": ["shimpz-cloudflare"],
                "reply": "",
            },
            {
                "task_follows": False,
                "intent": "ordinary-task",
                "query": "",
                "assistant_ids": [],
                "reply": "",
                "extra": True,
            },
        )
        for payload in invalid_outputs:
            with self.subTest(payload=payload), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client, _connection = self.client(_Response(payload))
                client.intent_route(
                    provider="openai",
                    model="gpt-6-sol",
                    api_key=self.secret,
                    objective="install cloudflare",
                    expected_intent="assistant-install",
                    candidates=directory_candidates(),
                    context=None,
                )

        invalid_requests = (
            ("assistant-install", directory_candidates()[::-1]),
            ("assistant-install", (directory_candidates()[0], directory_candidates()[0])),
            ("assistant-uninstall", directory_candidates()),
            (None, directory_candidates()[:1]),
            ("unsupported", directory_candidates()[:1]),
            ("assistant-install", (object(),)),
            (
                "assistant-install",
                (
                    brain_runtime_client.RuntimeDirectoryCandidate(
                        "shimpz-cloudflare",
                        "Shimpz Cloudflare",
                        None,
                    ),
                ),
            ),
        )
        for expected, shortlist in invalid_requests:
            with self.subTest(shortlist=shortlist, expected=expected):
                client, connection = self.client(_Response({"intent": "unresolved", "query": "", "assistant_ids": []}))
                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.intent_route(
                        provider="openai",
                        model="gpt-6-sol",
                        api_key=self.secret,
                        objective="lifecycle objective",
                        expected_intent=expected,
                        candidates=shortlist,
                        context=None,
                    )
                self.assertEqual(connection.requests, [])

        invalid_classifications = (
            {
                "task_follows": False,
                "intent": "ordinary-task",
                "query": "",
                "assistant_ids": ["shimpz-cloudflare", "shimpz-cloudflare"],
                "reply": "",
            },
            {"task_follows": False, "intent": "ordinary-task", "query": "cloudflare", "assistant_ids": [], "reply": ""},
            {
                "task_follows": False,
                "intent": "ordinary-task",
                "query": "",
                "assistant_ids": [],
                "reply": "Which Assistant?",
            },
        )
        for payload in invalid_classifications:
            with self.subTest(payload=payload), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client, _connection = self.client(_Response(payload))
                client.intent_route(
                    provider="openai",
                    model="gpt-6-sol",
                    api_key=self.secret,
                    objective="hello",
                    expected_intent=None,
                    candidates=(),
                    context=None,
                )

        invalid_contexts = (
            object(),
            brain_runtime_client.RuntimeLifecycleContext(reference=object()),
            brain_runtime_client.RuntimeLifecycleContext(
                conversation=[],
            ),
            brain_runtime_client.RuntimeLifecycleContext(
                conversation=(brain_runtime_client.RuntimeConversationEntry("system", "remove it", False),),
            ),
            brain_runtime_client.RuntimeLifecycleContext(
                conversation=(brain_runtime_client.RuntimeConversationEntry("user", "remove it", 1),),
            ),
            brain_runtime_client.RuntimeLifecycleContext(
                conversation=(brain_runtime_client.RuntimeConversationEntry("user", "x" * 513, False),),
            ),
            brain_runtime_client.RuntimeLifecycleContext(language_exemplar="remove it"),
        )
        for route_context in invalid_contexts:
            with self.subTest(route_context=route_context):
                client, connection = self.client(
                    _Response(
                        {
                            "task_follows": False,
                            "intent": "ordinary-task",
                            "query": "",
                            "assistant_ids": [],
                            "reply": "",
                        }
                    )
                )
                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.intent_route(
                        provider="openai",
                        model="gpt-6-sol",
                        api_key=self.secret,
                        objective="hello",
                        expected_intent=None,
                        candidates=(),
                        context=route_context,
                    )
                self.assertEqual(connection.requests, [])

        client, connection = self.client(
            _Response({"task_follows": False, "intent": "ordinary-task", "query": "", "assistant_ids": [], "reply": ""})
        )
        with (
            mock.patch.object(brain_runtime_client, "MAX_CONVERSATION_CHARS", 1),
            self.assertRaises(brain_runtime_client.BrainRuntimeError),
        ):
            client.intent_route(
                provider="openai",
                model="gpt-6-sol",
                api_key=self.secret,
                objective="hello",
                expected_intent=None,
                candidates=(),
                context=brain_runtime_client.RuntimeLifecycleContext(
                    conversation=(brain_runtime_client.RuntimeConversationEntry("user", "hi", False),),
                ),
            )
        self.assertEqual(connection.requests, [])

        client, connection = self.client(
            _Response(
                {"task_follows": False, "intent": "unresolved", "query": "", "assistant_ids": [], "reply": "clarify"}
            )
        )
        with self.assertRaises(brain_runtime_client.BrainRuntimeError):
            client.intent_route(
                provider="openai",
                model="gpt-6-sol",
                api_key=self.secret,
                objective="install",
                expected_intent="assistant-install",
                candidates=[],
                context=None,
            )
        self.assertEqual(connection.requests, [])

        client, connection = self.client(_Response({"intent": "ordinary-task", "query": "", "assistant_ids": []}))
        with self.assertRaises(brain_runtime_client.BrainRuntimeError):
            client.intent_route(
                provider="invalid",
                model="gpt-6-sol",
                api_key=self.secret,
                objective="hello",
                expected_intent=None,
                candidates=(),
                context=None,
            )
        self.assertEqual(connection.requests, [])

        client, connection = self.client(_Response({"intent": "unresolved", "query": "", "assistant_ids": []}))
        with self.assertRaises(brain_runtime_client.BrainRuntimeError):
            client.intent_route(
                provider="openai",
                model="gpt-6-sol",
                api_key=self.secret,
                objective="remove it",
                expected_intent="assistant-uninstall",
                candidates=directory_candidates(uninstall=True),
                context=brain_runtime_client.RuntimeLifecycleContext(
                    reference=brain_runtime_client.RuntimeLifecycleReference(
                        "shimpz-cloudflare",
                        "Shimpz Cloudflare",
                    ),
                ),
            )
        self.assertEqual(connection.requests, [])


if __name__ == "__main__":
    unittest.main()
