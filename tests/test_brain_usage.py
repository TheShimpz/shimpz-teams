"""Brain model usage: closed parsing, per-request metering, and attachment to the request's audit event (ADR-0082)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_brain_runtime_client import (
    NO_USAGE,
    RuntimeClientCase,
    _Response,
    capability_candidates,
    context,
)

from inference import client as brain_runtime_client
from inference import usage as brain_usage
from local import audit as local_audit
from local.http import audit as request_audit_module

USAGE = {
    "model_calls": 2,
    "failed_calls": 0,
    "unreported_calls": 1,
    "input_tokens": 900,
    "output_tokens": 40,
    "cache_read_tokens": 512,
    "cache_write_tokens": 0,
}


class ParseAndMeterTests(unittest.TestCase):
    def test_only_the_closed_consistent_shape_parses(self):
        self.assertEqual(brain_usage.parse(dict(USAGE)), USAGE)
        broken = [
            None,
            [],
            {name: USAGE[name] for name in brain_usage.FIELDS[:-1]},
            {**USAGE, "usd": 1},
            {**USAGE, "input_tokens": True},
            {**USAGE, "input_tokens": 1.0},
            {**USAGE, "output_tokens": -1},
            {**USAGE, "output_tokens": brain_usage.MAX_COUNT + 1},
            {**USAGE, "failed_calls": 2},
        ]
        for value in broken:
            with self.subTest(value=value), self.assertRaises(brain_usage.UsageError):
                brain_usage.parse(value)

    def test_a_meter_keeps_each_operation_provider_and_model_apart_and_drains_once(self):
        meter = brain_usage.Meter()
        self.assertIsNone(meter.drain())
        meter.add("turn", "openai", "gpt-6-luna", USAGE)
        meter.add("turn", "openai", "gpt-6-luna", USAGE)
        meter.add("turn-resume", "openai", "gpt-6-luna", {**USAGE, "input_tokens": 50})
        meter.add("turn", "anthropic", "claude-sonnet-5-5", NO_USAGE)
        with self.assertRaises(brain_usage.UsageError):
            meter.add("chat", "openai", "gpt-6-luna", USAGE)
        drained = meter.drain()
        self.assertEqual(
            [(entry["operation"], entry["provider"], entry["model"], entry["count"]) for entry in drained],
            [
                ("turn", "anthropic", "claude-sonnet-5-5", 1),
                ("turn", "openai", "gpt-6-luna", 2),
                ("turn-resume", "openai", "gpt-6-luna", 1),
            ],
        )
        self.assertEqual((drained[1]["input_tokens"], drained[2]["input_tokens"]), (1800, 50))
        self.assertIsNone(meter.drain())

    def test_a_routine_run_reads_the_tokens_used_since_a_point_of_its_request(self):
        # Outside a metered request nothing is known, so nothing was used since.
        self.assertEqual((brain_usage.tokens(), brain_usage.since({})), ({}, []))
        with brain_usage.metered():
            brain_usage.record("turn", "openai", "gpt-6-luna", USAGE)
            before = brain_usage.tokens()
            self.assertEqual(before, {("openai", "gpt-6-luna"): (900, 40)})
            brain_usage.record("routine-recovery", "openai", "gpt-6-luna", USAGE)
            brain_usage.record("routine-recovery", "anthropic", "claude-sonnet-5-5", USAGE)
            self.assertEqual(
                brain_usage.since(before),
                [
                    {"provider": "anthropic", "model": "claude-sonnet-5-5", "input_tokens": 900, "output_tokens": 40},
                    {"provider": "openai", "model": "gpt-6-luna", "input_tokens": 900, "output_tokens": 40},
                ],
            )
            # A model whose tokens did not move since is left out.
            self.assertEqual(brain_usage.since(brain_usage.tokens()), [])

    def test_usage_outside_a_metered_request_is_not_kept(self):
        brain_usage.record("turn", "openai", "gpt-6-luna", USAGE)
        self.assertIsNone(brain_usage.drain())
        with brain_usage.metered():
            brain_usage.record("turn", "openai", "gpt-6-luna", USAGE)
            self.assertEqual(brain_usage.drain()[0]["output_tokens"], 40)
        self.assertIsNone(brain_usage.drain())


class ClientMeteringTests(RuntimeClientCase):
    def _operations(self):
        route = brain_runtime_client.RouteCredentials("openai", "gpt-6-luna", self.secret)
        return (
            (
                "turn",
                {"status": "completed", "reply": "Hi.", "actions": [], "clarification": None},
                lambda client: client.start(context(self.secret), "Hi", conversation=()),
            ),
            (
                "turn-resume",
                {"status": "completed", "reply": "Done.", "actions": [], "clarification": None},
                lambda client: client.resume(context(self.secret), {"interrupt-1": {"ok": True}}),
            ),
            (
                "action-labels",
                {"labels": [{"id": "list-zones", "label": "Listar zonas"}]},
                lambda client: client.action_labels(
                    provider="openai",
                    model="gpt-6-luna",
                    api_key=self.secret,
                    locale="pt",
                    action_ids=("list-zones",),
                ),
            ),
            (
                "capability-plan",
                {"status": "sufficient", "assistant_ids": []},
                lambda client: client.capability_plan(
                    provider="openai",
                    model="gpt-6-luna",
                    api_key=self.secret,
                    objective="Configure DNS.",
                    candidates=capability_candidates(),
                ),
            ),
            (
                "intent-route",
                {"task_follows": False, "intent": "ordinary-task", "query": "", "assistant_ids": [], "reply": ""},
                lambda client: client.intent_route(
                    credentials=route,
                    objective="oi",
                    expected_intent=None,
                    candidates=(),
                    context=brain_runtime_client.RuntimeLifecycleContext(locale="pt"),
                ),
            ),
            (
                "purpose",
                {"purpose": "To list your zones, I need Cloudflare."},
                lambda client: client.purpose(
                    context(self.secret),
                    brain_runtime_client.ActionRequest("interrupt-1", "shimpz-cloudflare", "list-zones", {}),
                    "Shimpz Cloudflare",
                    "List zones.",
                ),
            ),
        )

    def test_every_brain_operation_records_its_usage_under_its_own_name(self):
        for operation, payload, call in self._operations():
            with self.subTest(operation=operation), brain_usage.metered():
                client, _connection = self.client(_Response(payload, usage=USAGE))
                call(client)
                [entry] = brain_usage.drain()
                model = "gpt-test" if operation.startswith("turn") or operation == "purpose" else "gpt-6-luna"
                self.assertEqual((entry["operation"], entry["provider"], entry["model"]), (operation, "openai", model))
                self.assertEqual(entry["count"], 1)
                self.assertEqual(entry["input_tokens"], 900)

    def test_usage_is_kept_even_when_the_rest_of_the_response_is_invalid(self):
        with brain_usage.metered():
            client, _connection = self.client(_Response({"status": "bogus"}, usage=USAGE))
            with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client.start(context(self.secret), "Hi", conversation=())
            self.assertEqual(brain_usage.drain()[0]["operation"], "turn")

    def test_a_missing_or_invalid_usage_report_is_an_invalid_response(self):
        payload = {"status": "completed", "reply": "Hi.", "actions": [], "clarification": None}
        for response in (
            _Response(payload, usage=None),
            _Response(payload, usage={**USAGE, "failed_calls": 9}),
            _Response(["not", "an", "object"]),
        ):
            with self.subTest(response=response._raw), brain_usage.metered():
                client, _connection = self.client(response)
                with self.assertRaises(brain_runtime_client.BrainRuntimeError):
                    client.start(context(self.secret), "Hi", conversation=())
                self.assertIsNone(brain_usage.drain())


class AuditAttachmentTests(unittest.TestCase):
    def test_the_local_request_event_carries_the_usage_and_drains_it(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "audit.jsonl"
        local_audit.close()
        self.addCleanup(local_audit.close)
        request_audit = request_audit_module.RequestAudit(operation="chat")
        request_audit.machine()
        with mock.patch.object(local_audit, "AUDIT_PATH", path), brain_usage.metered():
            brain_usage.record("turn", "openai", "gpt-6-luna", USAGE)
            brain_usage.record("turn-resume", "openai", "gpt-6-luna", {**USAGE, "output_tokens": 7})
            request_audit.record("request", result="ok", team_id="team_1")
            request_audit.record("request", result="ok", team_id="team_1")
            local_audit.flush()
        first, second = (json.loads(line) for line in path.read_text().splitlines())
        self.assertEqual(
            [(entry["operation"], entry["output_tokens"]) for entry in first["model_usage"]],
            [("turn", 40), ("turn-resume", 7)],
        )
        self.assertEqual(first["model_usage"][0]["cache_read_tokens"], 512)
        self.assertNotIn("model_usage", second)


if __name__ == "__main__":
    unittest.main()
