from __future__ import annotations

import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inference import client as brain_runtime_client


class _Response:
    def __init__(self, payload: object, *, status: int = 200, raw: bytes | None = None) -> None:
        self.status = status
        self._raw = raw if raw is not None else json.dumps(payload).encode()

    def read(self, _maximum: int) -> bytes:
        return self._raw


class _Connection:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, *request) -> None:
        self.requests.append(request)

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        self.closed = True


def context(secret: str) -> brain_runtime_client.RuntimeContext:
    return brain_runtime_client.RuntimeContext(
        thread_id="team:hello-pulse:conversation-1",
        team_name="Marketing",
        assistants=(
            brain_runtime_client.RuntimeAssistant(
                id="hello-pulse",
                genesis="Combine the declared greeting Actions into one bounded welcome.",
                actions=(
                    brain_runtime_client.RuntimeAction(
                        id="hello",
                        summary="Return a greeting.",
                        input_schema={
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "additionalProperties": False,
                        },
                    ),
                ),
            ),
        ),
        provider="openai",
        model="gpt-test",
        api_key=secret,
    )


def capability_candidates() -> tuple[brain_runtime_client.RuntimeCapabilityCandidate, ...]:
    return (
        brain_runtime_client.RuntimeCapabilityCandidate(
            id="shimpz-cloudflare",
            name="Shimpz Cloudflare",
            summary="Manage reviewed DNS records.",
            actions=("change-dns", "list-zones"),
            integrations=(
                brain_runtime_client.RuntimeCapabilityIntegration("cloudflare", "cloudflare"),
            ),
        ),
        brain_runtime_client.RuntimeCapabilityCandidate(
            id="shimpz-whatsapp",
            name="Shimpz WhatsApp",
            summary="Send reviewed WhatsApp messages.",
            actions=("send-message",),
            integrations=(
                brain_runtime_client.RuntimeCapabilityIntegration("whatsapp", "whatsapp"),
            ),
        ),
    )


class BrainRuntimeClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = secrets.token_hex(32)
        self.secret = secrets.token_urlsafe(32)
        self.token_file = Path(self.directory.name) / "token"
        self.token_file.write_text(self.token, encoding="utf-8")

    def client(self, response: _Response):
        connection = _Connection(response)
        client = brain_runtime_client.BrainRuntimeClient(
            base_url="http://brain-runtime:8080",
            token_file=self.token_file,
            connection_factory=lambda _host, _port, _timeout: connection,
        )
        return client, connection

    def test_start_uses_only_the_fixed_runtime_endpoint_and_private_token(self):
        client, connection = self.client(_Response({"status": "completed", "reply": "Hello.", "actions": []}))

        result = client.start(context(self.secret), "Hello")

        self.assertEqual(result.status, "completed")
        method, path, raw_body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/turns"))
        self.assertEqual(headers["Authorization"], f"Bearer {self.token}")
        payload = json.loads(raw_body)
        self.assertEqual(payload["provider"]["api_key"], self.secret)
        self.assertEqual(payload["team_name"], "Marketing")
        self.assertEqual(payload["assistants"][0]["id"], "hello-pulse")
        self.assertEqual(
            payload["assistants"][0]["genesis"],
            "Combine the declared greeting Actions into one bounded welcome.",
        )
        self.assertTrue(connection.closed)

    def test_action_suspension_is_parsed_without_gaining_execution_authority(self):
        client, _connection = self.client(
            _Response(
                {
                    "status": "action-required",
                    "reply": "",
                    "actions": [
                        {
                            "interrupt_id": "interrupt-1",
                            "assistant_id": "hello-pulse",
                            "action": "hello",
                            "input": {"name": "Ada"},
                        }
                    ],
                }
            )
        )

        result = client.start(context(self.secret), "Greet Ada")

        self.assertEqual(result.actions[0].action, "hello")
        self.assertEqual(result.actions[0].assistant_id, "hello-pulse")
        self.assertEqual(result.actions[0].input, {"name": "Ada"})

    def test_resume_sends_only_interrupt_results(self):
        client, connection = self.client(_Response({"status": "completed", "reply": "Done.", "actions": []}))

        client.resume(context(self.secret), {"interrupt-1": {"message": "Hello, Ada."}})

        _method, path, raw_body, _headers = connection.requests[0]
        self.assertEqual(path, "/v1/turns/resume")
        self.assertEqual(json.loads(raw_body)["results"], {"interrupt-1": {"message": "Hello, Ada."}})

    def test_delete_thread_uses_the_closed_runtime_endpoint(self):
        client, connection = self.client(_Response({"status": "deleted"}))

        result = client.delete_thread("team:hello-pulse:conversation-1")

        self.assertIsNone(result)
        method, path, raw_body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/threads/delete"))
        self.assertEqual(headers["Authorization"], f"Bearer {self.token}")
        self.assertEqual(
            json.loads(raw_body),
            {"thread_id": "team:hello-pulse:conversation-1"},
        )
        self.assertTrue(connection.closed)

    def test_action_labels_use_the_stateless_endpoint_and_exact_id_order(self):
        client, connection = self.client(
            _Response(
                {
                    "labels": [
                        {"id": "get-zone", "label": "Consultar zona DNS"},
                        {"id": "list-zones", "label": "Listar zonas DNS"},
                    ]
                }
            )
        )

        labels = client.action_labels(
            provider="openai",
            model="gpt-5.6-terra",
            api_key=self.secret,
            language_exemplar="Quero listar minhas zonas DNS",
            action_ids=("list-zones", "get-zone"),
        )

        self.assertEqual(
            labels,
            (
                brain_runtime_client.RuntimeActionLabel("list-zones", "Listar zonas DNS"),
                brain_runtime_client.RuntimeActionLabel("get-zone", "Consultar zona DNS"),
            ),
        )
        method, path, raw_body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/action-labels"))
        self.assertEqual(headers["Authorization"], f"Bearer {self.token}")
        self.assertEqual(
            json.loads(raw_body),
            {
                "provider": {"provider": "openai", "model": "gpt-5.6-terra", "api_key": self.secret},
                "language_exemplar": "Quero listar minhas zonas DNS",
                "actions": ["list-zones", "get-zone"],
            },
        )

    def test_capability_plan_uses_only_the_stateless_bounded_endpoint(self):
        client, connection = self.client(
            _Response(
                {
                    "status": "install-required",
                    "assistant_ids": ["shimpz-cloudflare", "shimpz-whatsapp"],
                }
            )
        )

        plan = client.capability_plan(
            provider="openai",
            model="gpt-5.6-terra",
            api_key=self.secret,
            objective="Configure example.com and send the result by WhatsApp.",
            candidates=capability_candidates(),
        )

        self.assertEqual(
            plan,
            brain_runtime_client.RuntimeCapabilityPlan(
                "install-required",
                ("shimpz-cloudflare", "shimpz-whatsapp"),
            ),
        )
        method, path, raw_body, headers = connection.requests[0]
        self.assertEqual((method, path), ("POST", "/v1/capability-plan"))
        self.assertEqual(headers["Authorization"], f"Bearer {self.token}")
        payload = json.loads(raw_body)
        self.assertEqual(payload["provider"]["api_key"], self.secret)
        self.assertEqual([item["id"] for item in payload["candidates"]], [
            "shimpz-cloudflare",
            "shimpz-whatsapp",
        ])
        self.assertNotIn("thread_id", payload)
        self.assertNotIn("genesis", raw_body.decode())
        self.assertNotIn("input_schema", raw_body.decode())

    def test_capability_plan_rejects_invalid_inputs_and_outputs_without_widening(self):
        invalid_outputs = (
            {"status": "sufficient", "assistant_ids": ["shimpz-cloudflare"]},
            {"status": "install-required", "assistant_ids": []},
            {"status": "install-required", "assistant_ids": ["unknown"]},
            {"status": "install-required", "assistant_ids": ["shimpz-whatsapp", "shimpz-cloudflare"]},
            {"status": "install-required", "assistant_ids": ["shimpz-cloudflare", "shimpz-cloudflare"]},
            {"status": "sufficient", "assistant_ids": [], "extra": True},
        )
        for payload in invalid_outputs:
            with self.subTest(payload=payload), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client, _connection = self.client(_Response(payload))
                client.capability_plan(
                    provider="openai",
                    model="gpt-5.6-terra",
                    api_key=self.secret,
                    objective="Configure DNS.",
                    candidates=capability_candidates(),
                )

        invalid_candidates = (
            (),
            capability_candidates()[::-1],
            (capability_candidates()[0], capability_candidates()[0]),
            (
                brain_runtime_client.RuntimeCapabilityCandidate(
                    id="shimpz-cloudflare",
                    name="Shimpz Cloudflare",
                    summary="Manage DNS.",
                    actions=("list-zones", "list-zones"),
                    integrations=(),
                ),
            ),
        )
        for candidates in invalid_candidates:
            with self.subTest(candidates=candidates):
                client, connection = self.client(_Response({"status": "sufficient", "assistant_ids": []}))
                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.capability_plan(
                        provider="openai",
                        model="gpt-5.6-terra",
                        api_key=self.secret,
                        objective="Configure DNS.",
                        candidates=candidates,
                    )
                self.assertEqual(connection.requests, [])

    def test_action_label_requests_and_responses_fail_closed(self):
        valid = {
            "labels": [
                {"id": "list-zones", "label": "Listar zonas DNS"},
                {"id": "get-zone", "label": "Consultar zona DNS"},
            ]
        }
        invalid_responses = (
            {**valid, "extra": True},
            {"labels": valid["labels"][:1]},
            {"labels": [*valid["labels"], {"id": "extra", "label": "Extra"}]},
            {"labels": [{"id": "list-zones", "label": "Mesmo"}, {"id": "get-zone", "label": "Mesmo"}]},
            {
                "labels": [
                    {"id": "list-zones", "label": "é"},
                    {"id": "get-zone", "label": "e\u0301"},
                ]
            },
            {
                "labels": [
                    {"id": "list-zones", "label": "Listar\nzona"},
                    {"id": "get-zone", "label": "Consultar zona"},
                ]
            },
        )
        for payload in invalid_responses:
            with self.subTest(payload=payload), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client, _connection = self.client(_Response(payload))
                client.action_labels(
                    provider="openai",
                    model="gpt-5.6-terra",
                    api_key=self.secret,
                    language_exemplar="Liste minhas zonas",
                    action_ids=("list-zones", "get-zone"),
                )

        duplicate_raw = (
            b'{"labels":[],"labels":['
            b'{"id":"list-zones","label":"Listar zonas DNS"},'
            b'{"id":"get-zone","label":"Consultar zona DNS"}'
            b"]}"
        )
        client, _connection = self.client(_Response({}, raw=duplicate_raw))
        with self.assertRaises(brain_runtime_client.BrainRuntimeError):
            client.action_labels(
                provider="openai",
                model="gpt-5.6-terra",
                api_key=self.secret,
                language_exemplar="Liste minhas zonas",
                action_ids=("list-zones", "get-zone"),
            )

        invalid_requests = (
            {"provider": "other"},
            {"api_key": "bad\0secret"},
            {"api_key": "x" * (16 * 1024 + 1)},
            {"language_exemplar": ""},
            {"language_exemplar": " surrounding "},
            {"language_exemplar": "hidden\0instruction"},
            {"action_ids": ()},
            {"action_ids": ("list-zones", "list-zones")},
            {"action_ids": ("../shell",)},
        )
        for update in invalid_requests:
            with self.subTest(update=update):
                client, connection = self.client(_Response(valid))
                request = {
                    "provider": "openai",
                    "model": "gpt-5.6-terra",
                    "api_key": self.secret,
                    "language_exemplar": "Liste minhas zonas",
                    "action_ids": ("list-zones", "get-zone"),
                    **update,
                }
                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.action_labels(**request)
                self.assertEqual(connection.requests, [])

    def test_delete_thread_rejects_invalid_ids_before_connecting(self):
        for thread_id in ("", "bad thread", "a" * 257, None):
            with self.subTest(thread_id=thread_id):
                client, connection = self.client(_Response({"status": "deleted"}))

                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.delete_thread(thread_id)

                self.assertEqual(connection.requests, [])

    def test_delete_thread_response_must_match_the_closed_contract(self):
        for payload in (
            {},
            {"status": "ok"},
            {"status": "deleted", "thread_id": "conversation-1"},
            ["deleted"],
        ):
            with self.subTest(payload=payload):
                client, _connection = self.client(_Response(payload))

                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.delete_thread("team:hello-pulse:conversation-1")

    def test_malformed_runtime_responses_fail_closed(self):
        invalid = (
            {"status": "completed", "reply": "", "actions": []},
            {"status": "completed", "reply": "x" * 60_001, "actions": []},
            {"status": "completed", "reply": "unsafe\u0000reply", "actions": []},
            {"status": "completed", "reply": "ok", "actions": [{"action": "hello"}]},
            {"status": "action-required", "reply": "unexpected", "actions": []},
            {"status": "unknown", "reply": "ok", "actions": []},
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                client, _connection = self.client(_Response(payload))
                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.start(context(self.secret), "Hello")

    def test_provider_or_transport_errors_never_echo_the_api_key(self):
        for response in (
            _Response({}, status=502, raw=self.secret.encode()),
            _Response({}, raw=b"not-json" + self.secret.encode()),
        ):
            with self.subTest(status=response.status):
                client, _connection = self.client(response)
                with self.assertRaises(brain_runtime_client.BrainRuntimeError) as raised:
                    client.start(context(self.secret), "Hello")
                self.assertNotIn(self.secret, str(raised.exception))

    def test_runtime_url_cannot_carry_credentials_paths_or_queries(self):
        for url in (
            "https://brain-runtime:8080",
            "http://user:secret@brain-runtime:8080",
            "http://brain-runtime:8080/other",
            "http://brain-runtime:8080?redirect=evil",
        ):
            with self.subTest(url=url), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                brain_runtime_client.BrainRuntimeClient(base_url=url, token_file=self.token_file)

    def test_default_connection_factory_builds_a_plain_http_connection(self) -> None:
        with mock.patch.object(
            brain_runtime_client.http.client,
            "HTTPConnection",
            return_value="connection",
        ) as factory:
            self.assertEqual(brain_runtime_client._connection("brain", 8080, 3.0), "connection")
        factory.assert_called_once_with("brain", 8080, timeout=3.0)

    def test_missing_and_malformed_runtime_tokens_fail_before_transport(self) -> None:
        missing = self.token_file.with_name("missing")
        client = brain_runtime_client.BrainRuntimeClient(token_file=missing)
        with self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "authentication"):
            client._token()

        for token in ("", "x" * 4097, "bad\0token"):
            with self.subTest(token_length=len(token)):
                self.token_file.write_text(token, encoding="utf-8")
                with self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "authentication"):
                    client = brain_runtime_client.BrainRuntimeClient(token_file=self.token_file)
                    client._token()

    def test_transport_and_oversized_responses_fail_closed_and_close(self) -> None:
        connection = _Connection(_Response({}))
        connection.request = mock.Mock(side_effect=OSError("offline"))
        client = brain_runtime_client.BrainRuntimeClient(
            token_file=self.token_file,
            connection_factory=lambda *_args: connection,
        )
        with self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "unavailable"):
            client.start(context(self.secret), "Hello")
        self.assertTrue(connection.closed)

        client, connection = self.client(_Response({}, raw=b"x" * (brain_runtime_client.MAX_RESPONSE_BYTES + 1)))
        with self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "invalid response"):
            client.start(context(self.secret), "Hello")
        self.assertTrue(connection.closed)

    def test_root_and_action_identity_response_shapes_fail_closed(self) -> None:
        invalid = (
            [],
            {"status": "completed", "reply": "ok", "actions": [], "extra": True},
            {
                "status": "action-required",
                "reply": "",
                "actions": [
                    {
                        "interrupt_id": "bad interrupt",
                        "assistant_id": "hello-pulse",
                        "action": "hello",
                        "input": {},
                    }
                ],
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                client, _connection = self.client(_Response(payload))
                with self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "invalid response"):
                    client.start(context(self.secret), "Hello")


if __name__ == "__main__":
    unittest.main()
