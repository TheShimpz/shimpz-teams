import dataclasses
import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inference import client as brain_runtime_client
from inference import usage as brain_usage

NO_USAGE = dict.fromkeys(brain_usage.FIELDS, 0)


class _Response:
    """A Brain response; an object payload carries Brain's usage report unless ``usage`` is None."""

    def __init__(
        self, payload: object, *, status: int = 200, raw: bytes | None = None, usage: object = NO_USAGE
    ) -> None:
        self.status = status
        if isinstance(payload, dict) and usage is not None and "usage" not in payload:
            payload = {**payload, "usage": usage}
        # A turn response always carries its memory and Routine changes; a test that checks one states it itself.
        if isinstance(payload, dict) and {"reply", "actions"} <= set(payload) and "memory" not in payload:
            payload = {**payload, "memory": []}
        if isinstance(payload, dict) and {"reply", "actions"} <= set(payload) and "routine" not in payload:
            payload = {**payload, "routine": None}
        self._raw = raw if raw is not None else json.dumps(payload).encode()

    def read(self, _maximum: int) -> bytes:
        return self._raw


class _Connection:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.requests = []
        self.closed = False
        self.sock = None

    def connect(self) -> None:
        self.sock = mock.Mock()

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
        effort="low",
    )


def capability_candidates() -> tuple[brain_runtime_client.RuntimeCapabilityCandidate, ...]:
    return (
        brain_runtime_client.RuntimeCapabilityCandidate(
            id="shimpz-cloudflare",
            name="Shimpz Cloudflare",
            summary="Manage reviewed DNS records.",
            actions=("change-dns", "list-zones"),
            integrations=(brain_runtime_client.RuntimeCapabilityIntegration("cloudflare", "cloudflare"),),
        ),
        brain_runtime_client.RuntimeCapabilityCandidate(
            id="shimpz-whatsapp",
            name="Shimpz WhatsApp",
            summary="Send reviewed WhatsApp messages.",
            actions=("send-message",),
            integrations=(brain_runtime_client.RuntimeCapabilityIntegration("whatsapp", "whatsapp"),),
        ),
    )


def directory_candidates(*, uninstall: bool = False) -> tuple[brain_runtime_client.RuntimeDirectoryCandidate, ...]:
    return (
        brain_runtime_client.RuntimeDirectoryCandidate(
            "shimpz-cloudflare",
            "Shimpz Cloudflare",
            "" if uninstall else "Manage reviewed DNS records.",
        ),
        brain_runtime_client.RuntimeDirectoryCandidate(
            "shimpz-whatsapp",
            "Shimpz WhatsApp",
            "" if uninstall else "Send reviewed WhatsApp messages.",
        ),
    )


class RuntimeClientCase(unittest.TestCase):
    """Private runtime token and one fake connection shared by the Brain runtime client suites."""

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


class BrainRuntimeClientTests(RuntimeClientCase):
    def test_start_uses_only_the_fixed_runtime_endpoint_and_private_token(self):
        client, connection = self.client(
            _Response({"status": "completed", "clarification": None, "reply": "Hello.", "actions": []})
        )

        result = client.start(context(self.secret), "Hello", conversation=())

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

    def test_start_carries_the_committed_conversation_and_resume_never_does(self):
        window = (
            brain_runtime_client.RuntimeConversationEntry("user", "List my DNS zones", False),
            brain_runtime_client.RuntimeConversationEntry("assistant", "Install Cloudflare first.", False),
        )
        client, connection = self.client(
            _Response({"status": "completed", "clarification": None, "reply": "Done.", "actions": []})
        )
        client.start(context(self.secret), "Can you enable it?", conversation=window)
        payload = json.loads(connection.requests[0][2])
        self.assertEqual(
            payload["conversation"],
            [
                {"role": "user", "text": "List my DNS zones", "truncated": False},
                {"role": "assistant", "text": "Install Cloudflare first.", "truncated": False},
            ],
        )
        client, connection = self.client(
            _Response({"status": "completed", "clarification": None, "reply": "Done.", "actions": []})
        )
        client.resume(context(self.secret), {"interrupt-1": {"status": "ok"}})
        self.assertNotIn("conversation", json.loads(connection.requests[0][2]))

    def test_only_a_start_carries_the_persons_turn_clock(self):
        clocked = dataclasses.replace(context(self.secret), turn_clock=("2026-10-06", "America/Sao_Paulo"))
        done = {"status": "completed", "clarification": None, "reply": "Done.", "actions": []}
        client, connection = self.client(_Response(done))
        client.start(clocked, "Hello", conversation=())
        payload = json.loads(connection.requests[0][2])
        self.assertEqual(payload["turn_clock"], {"date": "2026-10-06", "timezone": "America/Sao_Paulo"})
        client, connection = self.client(_Response(done))
        client.start(context(self.secret), "Hello", conversation=())
        self.assertNotIn("turn_clock", json.loads(connection.requests[0][2]))
        client, connection = self.client(_Response(done))
        client.resume(clocked, {"interrupt-1": {"status": "ok"}})
        self.assertNotIn("turn_clock", json.loads(connection.requests[0][2]))

    def test_start_and_resume_both_carry_the_prepared_attachments_and_action_gates(self):
        attachment = {
            "id": "a" * 32,
            "name": "notes.md",
            "media_type": "text/markdown",
            "size": 5,
            "sha256": "f" * 64,
            "content": {"type": "text", "text": "notes", "pdf": False},
        }
        base = context(self.secret)
        gated = dataclasses.replace(
            base,
            attachments=(attachment,),
            assistants=(
                dataclasses.replace(
                    base.assistants[0],
                    actions=(
                        brain_runtime_client.RuntimeAction(
                            "upload", "Upload.", {"type": "object"}, authorization=True, input_files=("document",)
                        ),
                    ),
                ),
            ),
        )
        for call in (
            lambda client: client.start(gated, "Use my notes", conversation=()),
            lambda client: client.resume(gated, {"interrupt-1": {"ok": True}}),
        ):
            client, connection = self.client(
                _Response({"status": "completed", "clarification": None, "reply": "Done.", "actions": []})
            )
            call(client)
            payload = json.loads(connection.requests[0][2])
            self.assertEqual(payload["attachments"], [attachment])
            self.assertEqual(
                payload["assistants"][0]["actions"][0],
                {
                    "id": "upload",
                    "summary": "Upload.",
                    "input_schema": {"type": "object"},
                    "output_schema": {},
                    "authorization": True,
                    "input_files": ["document"],
                },
            )
        client, connection = self.client(
            _Response({"status": "completed", "clarification": None, "reply": "Done.", "actions": []})
        )
        client.start(base, "Hello", conversation=())
        self.assertEqual(json.loads(connection.requests[0][2])["attachments"], [])

    def test_the_brain_gets_output_schemas_and_the_teams_advisory_routine_capacity(self):
        """A changed output schema is a new contract; the capacity only advises a continuous cap (ADR-0101)."""
        base = context(self.secret)
        output = {"type": "object", "properties": {"id": {"type": "string"}}}
        action = brain_runtime_client.RuntimeAction("publish", "Publish.", {"type": "object"}, output_schema=output)
        assistant = dataclasses.replace(base.assistants[0], actions=(action,))
        client, connection = self.client(
            _Response({"status": "completed", "clarification": None, "reply": "Done.", "actions": []})
        )
        client.start(dataclasses.replace(base, assistants=(assistant,), routine_capacity=123), "Hi", conversation=())
        payload = json.loads(connection.requests[0][2])
        self.assertEqual(
            (payload["routine_capacity"], payload["assistants"][0]["actions"][0]["output_schema"]), (123, output)
        )
        bare = dataclasses.replace(assistant, actions=(dataclasses.replace(action, output_schema={}),))
        self.assertNotEqual(brain_runtime_client.contract_digest(assistant), brain_runtime_client.contract_digest(bare))

    def test_an_invalid_conversation_is_refused_before_any_request(self):
        oversized = tuple(
            brain_runtime_client.RuntimeConversationEntry("user", "x", False)
            for _ in range(brain_runtime_client.MAX_CONVERSATION_ENTRIES + 1)
        )
        for window in (oversized, (brain_runtime_client.RuntimeConversationEntry("system", "x", False),), [object()]):
            client, connection = self.client(
                _Response({"status": "completed", "clarification": None, "reply": "Done.", "actions": []})
            )
            with (
                self.subTest(window=type(window)),
                self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "conversation window is invalid"),
            ):
                client.start(context(self.secret), "Hello", conversation=window)
            self.assertEqual(connection.requests, [])

    def test_action_suspension_is_parsed_without_gaining_execution_authority(self):
        client, _connection = self.client(
            _Response(
                {
                    "status": "action-required",
                    "clarification": None,
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

        result = client.start(context(self.secret), "Greet Ada", conversation=())

        self.assertEqual(result.actions[0].action, "hello")
        self.assertEqual(result.actions[0].assistant_id, "hello-pulse")
        self.assertEqual(result.actions[0].input, {"name": "Ada"})

    def test_resume_sends_only_interrupt_results(self):
        client, connection = self.client(
            _Response({"status": "completed", "clarification": None, "reply": "Done.", "actions": []})
        )

        client.resume(context(self.secret), {"interrupt-1": {"message": "Hello, Ada."}})

        _method, path, raw_body, _headers = connection.requests[0]
        self.assertEqual(path, "/v1/turns/resume")
        self.assertEqual(json.loads(raw_body)["results"], {"interrupt-1": {"message": "Hello, Ada."}})

    def test_delete_thread_uses_the_closed_runtime_endpoint(self):
        client, connection = self.client(_Response({"status": "deleted"}, usage=None))

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
            model="gpt-6.1-sol",
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
        self.assertEqual(
            [item["id"] for item in payload["candidates"]],
            [
                "shimpz-cloudflare",
                "shimpz-whatsapp",
            ],
        )
        self.assertNotIn("thread_id", payload)
        self.assertNotIn("genesis", raw_body.decode())
        self.assertNotIn("input_schema", raw_body.decode())

    def test_capability_plan_rejects_invalid_inputs_and_outputs_without_widening(self):
        invalid_outputs = (
            {"status": "unknown", "assistant_ids": []},
            {"status": "sufficient", "assistant_ids": None},
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
                    model="gpt-6.1-sol",
                    api_key=self.secret,
                    objective="Configure DNS.",
                    candidates=capability_candidates(),
                )

        invalid_candidates = (
            (),
            (object(),),
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
            (
                brain_runtime_client.RuntimeCapabilityCandidate(
                    id="shimpz-cloudflare",
                    name="Shimpz Cloudflare",
                    summary="Manage DNS.",
                    actions=("list-zones",),
                    integrations=(
                        brain_runtime_client.RuntimeCapabilityIntegration("second", "provider"),
                        brain_runtime_client.RuntimeCapabilityIntegration("first", "provider"),
                    ),
                ),
            ),
        )
        for candidates in invalid_candidates:
            with self.subTest(candidates=candidates):
                client, connection = self.client(_Response({"status": "sufficient", "assistant_ids": []}))
                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.capability_plan(
                        provider="openai",
                        model="gpt-6.1-sol",
                        api_key=self.secret,
                        objective="Configure DNS.",
                        candidates=candidates,
                    )
                self.assertEqual(connection.requests, [])

        client, connection = self.client(_Response({"status": "sufficient", "assistant_ids": []}))
        with self.assertRaises(brain_runtime_client.BrainRuntimeError):
            client.capability_plan(
                provider="invalid",
                model="gpt-6.1-sol",
                api_key=self.secret,
                objective="Configure DNS.",
                candidates=capability_candidates(),
            )
        self.assertEqual(connection.requests, [])
        with self.assertRaises(brain_runtime_client.BrainRuntimeError):
            brain_runtime_client.BrainRuntimeClient._capability_text(None, 10)

    def test_decision_responses_and_credentials_fail_closed(self):
        duplicate_raw = b'{"status":"install-required","status":"sufficient","assistant_ids":[]}'
        client, _connection = self.client(_Response({}, raw=duplicate_raw))
        with self.assertRaises(brain_runtime_client.BrainRuntimeError):
            client.capability_plan(
                provider="openai",
                model="gpt-6.1-sol",
                api_key=self.secret,
                objective="Configure DNS.",
                candidates=capability_candidates(),
            )
        for api_key in ("bad\0secret", "x" * (16 * 1024 + 1)):
            with self.subTest(api_key_length=len(api_key)):
                client, connection = self.client(_Response({"status": "sufficient", "assistant_ids": []}))
                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.capability_plan(
                        provider="openai",
                        model="gpt-6.1-sol",
                        api_key=api_key,
                        objective="Configure DNS.",
                        candidates=capability_candidates(),
                    )
                self.assertEqual(connection.requests, [])

    def test_delete_thread_rejects_invalid_ids_before_connecting(self):
        for thread_id in ("", "bad thread", "a" * 257, None):
            with self.subTest(thread_id=thread_id):
                client, connection = self.client(_Response({"status": "deleted"}, usage=None))

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

    def test_a_completed_turn_carries_one_closed_clarification(self):
        asked = {
            "question": "Qual período?",
            "options": [{"label": "Hoje", "description": ""}, {"label": "Semana", "description": "Sete dias."}],
            "default_index": 1,
        }
        rendered = "Qual período?\n\n1. Hoje\n2. Semana ✓ — Sete dias."
        client, _connection = self.client(
            _Response({"status": "completed", "clarification": asked, "reply": rendered, "actions": []})
        )
        turn = client.start(context(self.secret), "Quais modelos?", conversation=())
        self.assertEqual(turn.clarification, asked)
        self.assertEqual(turn.reply, rendered)
        # A reply that says anything but the question's own rendering is refused.
        client, _connection = self.client(
            _Response({"status": "completed", "clarification": asked, "reply": "I deleted everything.", "actions": []})
        )
        with self.assertRaises(brain_runtime_client.BrainRuntimeError):
            client.start(context(self.secret), "Quais modelos?", conversation=())

    def test_a_completed_turn_carries_at_most_one_routine_record_and_never_with_a_question(self):
        recorded = {"op": "record", "name": "DNS semanal"}
        client, connection = self.client(
            _Response(
                {"status": "completed", "clarification": None, "reply": "Ok.", "actions": [], "routine": recorded}
            )
        )
        routines = (
            {
                "routine_id": "a" * 32,
                "name": "Resumo",
                "schedule": {"kind": "daily", "time": "08:00"},
                "timezone": "UTC",
                "revision": 1,
                "daily_steps": 1,
                "output": {"mode": "show"},
                "steps": [{"id": "list", "assistant": "dns", "action": "list-zones", "inputs": []}],
            },
        )
        question = {"code": "routine-binding-unsourced", "options": [], "value": None}
        rerun = ({"assistant": "dns", "action": "list-zones", "count": 1, "inputs": []},)
        chat = dataclasses.replace(
            context(self.secret),
            routines=routines,
            routine_capacity=19_999,
            routine_mode=True,
            routine_question=question,
            routine_rerun=rerun,
        )
        # Local Team admits the record's closed shape; the client only bounds where it may appear.
        self.assertEqual(client.start(chat, "Toda segunda às 9h, confira o DNS", conversation=()).routine, recorded)
        sent = json.loads(connection.requests[0][2])
        self.assertEqual(
            (sent["routines"], sent["routine_capacity"], sent["knowledge_writable"]),
            ([dict(routines[0])], 19_999, True),
        )
        self.assertEqual(
            (sent["routine_mode"], sent["routine_question"], sent["routine_rerun"]), (True, question, [dict(rerun[0])])
        )
        question = {
            "question": "Qual?",
            "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}],
            "default_index": 0,
        }
        rendered = "Qual?\n\n1. A ✓\n2. B"
        for routine, status, clarification, reply in (
            (["record"], "completed", None, "Ok."),
            (recorded, "action-required", None, ""),
            (recorded, "completed", question, rendered),
        ):
            actions = (
                []
                if status == "completed"
                else [{"interrupt_id": "i", "assistant_id": "a", "action": "b", "input": {}}]
            )
            client, _connection = self.client(
                _Response(
                    {
                        "status": status,
                        "clarification": clarification,
                        "reply": reply,
                        "actions": actions,
                        "routine": routine,
                    }
                )
            )
            with self.subTest(status=status), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client.start(context(self.secret), "Toda segunda", conversation=())

    def test_malformed_runtime_responses_fail_closed(self):
        invalid = (
            {"status": "completed", "clarification": None, "reply": "", "actions": []},
            {"status": "completed", "clarification": None, "reply": "x" * 60_001, "actions": []},
            {"status": "completed", "clarification": None, "reply": "unsafe\u0000reply", "actions": []},
            {"status": "completed", "clarification": None, "reply": "ok", "actions": [{"action": "hello"}]},
            {"status": "action-required", "clarification": None, "reply": "unexpected", "actions": []},
            {"status": "unknown", "clarification": None, "reply": "ok", "actions": []},
            {"status": "completed", "reply": "ok", "actions": []},
            {"status": "completed", "clarification": {"question": "Q?"}, "reply": "ok", "actions": []},
            {
                "status": "action-required",
                "clarification": {
                    "question": "Qual?",
                    "options": [{"label": "A", "description": ""}, {"label": "B", "description": ""}],
                    "default_index": 0,
                },
                "reply": "",
                "actions": [{"interrupt_id": "i-1", "assistant_id": "hello-pulse", "action": "hello", "input": {}}],
            },
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                client, _connection = self.client(_Response(payload))
                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.start(context(self.secret), "Hello", conversation=())

    def test_provider_or_transport_errors_never_echo_the_api_key(self):
        for response in (
            _Response({}, status=502, raw=self.secret.encode()),
            _Response({}, raw=b"not-json" + self.secret.encode()),
        ):
            with self.subTest(status=response.status):
                client, _connection = self.client(response)
                with self.assertRaises(brain_runtime_client.BrainRuntimeError) as raised:
                    client.start(context(self.secret), "Hello", conversation=())
                self.assertNotIn(self.secret, str(raised.exception))

    def test_response_nesting_beyond_the_decoder_fails_closed(self) -> None:
        client, _connection = self.client(_Response({}, raw=b"[]"))
        with (
            mock.patch.object(brain_runtime_client.strict_json, "loads", side_effect=RecursionError),
            self.assertRaises(brain_runtime_client.BrainRuntimeError),
        ):
            client.start(context(self.secret), "Hello", conversation=())

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
            client.start(context(self.secret), "Hello", conversation=())
        self.assertTrue(connection.closed)

        client, connection = self.client(_Response({}, raw=b"x" * (brain_runtime_client.MAX_RESPONSE_BYTES + 1)))
        with self.assertRaisesRegex(brain_runtime_client.BrainRuntimeError, "invalid response"):
            client.start(context(self.secret), "Hello", conversation=())
        self.assertTrue(connection.closed)

    def test_root_and_action_identity_response_shapes_fail_closed(self) -> None:
        invalid = (
            [],
            {"status": "completed", "clarification": None, "reply": "ok", "actions": [], "extra": True},
            {
                "status": "action-required",
                "clarification": None,
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
                    client.start(context(self.secret), "Hello", conversation=())


if __name__ == "__main__":
    unittest.main()
