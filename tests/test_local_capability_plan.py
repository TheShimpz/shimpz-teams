from __future__ import annotations

import threading
import unittest
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from inference import client as brain_runtime_client
from inference import config as inference_config
from local.chat import capabilities
from local.errors import ApiProblemError


def plan_body() -> dict[str, object]:
    return {
        "objective": "Configure example.com and send the result by WhatsApp.",
        "candidates": [
            {
                "id": "shimpz-cloudflare",
                "name": "Shimpz Cloudflare",
                "summary": "Manage reviewed DNS records.",
                "actions": ["change-dns", "list-zones"],
                "integrations": [{"id": "cloudflare", "provider": "cloudflare"}],
            },
            {
                "id": "shimpz-whatsapp",
                "name": "Shimpz WhatsApp",
                "summary": "Send reviewed WhatsApp messages.",
                "actions": ["send-message"],
                "integrations": [{"id": "whatsapp", "provider": "whatsapp"}],
            },
        ],
    }


class Subject:
    def __init__(self) -> None:
        self._team_lock = threading.RLock()
        self._lock = lambda _team_id: self._team_lock
        self.network = SimpleNamespace(id="network-generation-1")
        self.assistant_lifecycle = SimpleNamespace(
            _network=mock.Mock(return_value=self.network),
            _validate_network=mock.Mock(return_value="Marketing"),
        )
        self.inference_store = SimpleNamespace(
            load=mock.Mock(return_value=inference_config.InferenceConfig("openai", "gpt-5.6-terra")),
        )
        self.brain_runtime = SimpleNamespace(
            capability_plan=mock.Mock(
                return_value=brain_runtime_client.RuntimeCapabilityPlan(
                    "install-required",
                    ("shimpz-cloudflare", "shimpz-whatsapp"),
                )
            )
        )
        self._capability_plan_snapshot = lambda team_id, provider: capabilities._capability_plan_snapshot(
            self,
            team_id,
            provider,
        )


class LocalCapabilityPlanTests(unittest.TestCase):
    def test_projects_one_exact_stateless_plan_after_binding_revalidation(self) -> None:
        subject = Subject()

        result = capabilities.capability_plan(
            subject,
            "team_1",
            plan_body(),
            "openai",
            "private-model-key",
        )

        self.assertEqual(
            result,
            {
                "team_id": "team_1",
                "status": "install-required",
                "assistant_ids": ["shimpz-cloudflare", "shimpz-whatsapp"],
            },
        )
        request = subject.brain_runtime.capability_plan.call_args.kwargs
        self.assertEqual(request["provider"], "openai")
        self.assertEqual(request["model"], "gpt-5.6-terra")
        self.assertEqual(request["api_key"], "private-model-key")
        self.assertEqual(tuple(item.id for item in request["candidates"]), (
            "shimpz-cloudflare",
            "shimpz-whatsapp",
        ))
        self.assertEqual(subject.assistant_lifecycle._validate_network.call_count, 2)

    def test_invalid_input_never_reaches_brain(self) -> None:
        subject = Subject()
        invalid = (
            {},
            {**plan_body(), "extra": True},
            {**plan_body(), "objective": " hidden "},
            {**plan_body(), "candidates": []},
            {**plan_body(), "candidates": list(reversed(plan_body()["candidates"]))},
            {
                **plan_body(),
                "candidates": [{**plan_body()["candidates"][0], "secret": "must-not-cross"}],
            },
        )
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ApiProblemError) as caught:
                capabilities.capability_plan(subject, "team_1", body, "openai", "private-model-key")
            self.assertEqual(caught.exception.code, "invalid-body")
        subject.brain_runtime.capability_plan.assert_not_called()

    def test_model_failure_and_team_drift_fail_closed_without_secret_echo(self) -> None:
        subject = Subject()
        subject.brain_runtime.capability_plan.side_effect = brain_runtime_client.BrainRuntimeError(
            "provider leaked private-model-key"
        )
        with self.assertRaises(ApiProblemError) as unavailable:
            capabilities.capability_plan(subject, "team_1", plan_body(), "openai", "private-model-key")
        self.assertEqual(unavailable.exception.code, "capability-plan-unavailable")
        self.assertNotIn("private-model-key", unavailable.exception.message)

        subject = Subject()
        before = capabilities.CapabilityPlanSnapshot("network-generation-1", "openai", "gpt-5.6-terra")
        after = capabilities.CapabilityPlanSnapshot("network-generation-2", "openai", "gpt-5.6-terra")
        subject._capability_plan_snapshot = mock.Mock(side_effect=(before, after))
        with self.assertRaises(ApiProblemError) as drift:
            capabilities.capability_plan(subject, "team_1", plan_body(), "openai", "private-model-key")
        self.assertEqual(drift.exception.code, "team-context-changed")
        self.assertEqual(drift.exception.status, HTTPStatus.CONFLICT)

    def test_snapshot_requires_current_team_and_provider_binding(self) -> None:
        subject = Subject()
        with self.assertRaises(ApiProblemError) as mismatch:
            capabilities._capability_plan_snapshot(subject, "team_1", "anthropic")
        self.assertEqual(mismatch.exception.code, "inference-provider-mismatch")

        subject.inference_store.load.side_effect = inference_config.InferenceConfigError("private state")
        with self.assertRaises(ApiProblemError) as unavailable:
            capabilities._capability_plan_snapshot(subject, "team_1", "openai")
        self.assertEqual(unavailable.exception.code, "inference-not-configured")


if __name__ == "__main__":
    unittest.main()
