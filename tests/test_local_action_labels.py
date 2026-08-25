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


class Subject:
    def __init__(self) -> None:
        self._team_lock = threading.RLock()
        self._lock = lambda _team_id: self._team_lock
        self.network = SimpleNamespace(id="network-generation-1", name="team-network")
        self.spec = SimpleNamespace(
            assistant_id="cloudflare-assistant",
            version="0.4.4",
            actions={"list-zones": object(), "get-zone": object()},
        )
        self.assistant_lifecycle = SimpleNamespace(
            _network=mock.Mock(return_value=self.network),
            _validate_network=mock.Mock(return_value="Marketing"),
        )
        self._active_chat_assistants = mock.Mock(
            return_value=(SimpleNamespace(spec=self.spec),),
        )
        self.inference_store = SimpleNamespace(
            load=mock.Mock(return_value=inference_config.InferenceConfig("openai", "gpt-5.6-terra")),
        )
        self.brain_runtime = SimpleNamespace(
            action_labels=mock.Mock(
                return_value=(
                    brain_runtime_client.RuntimeActionLabel("get-zone", "Consultar zona DNS"),
                    brain_runtime_client.RuntimeActionLabel("list-zones", "Listar zonas DNS"),
                )
            )
        )
        self._action_label_snapshot = lambda team_id, assistant_id, provider: capabilities._action_label_snapshot(
            self,
            team_id,
            assistant_id,
            provider,
        )


class LocalActionLabelTests(unittest.TestCase):
    def test_projects_exact_installed_actions_after_binding_revalidation(self) -> None:
        subject = Subject()

        result = capabilities.action_labels(
            subject,
            "team_1",
            "cloudflare-assistant",
            {"language_exemplar": "  Quero listar minhas zonas DNS  "},
            "openai",
            "private-model-key",
        )

        self.assertEqual(
            result,
            {
                "team_id": "team_1",
                "assistant": "cloudflare-assistant",
                "assistant_version": "0.4.4",
                "actions": [
                    {"id": "get-zone", "label": "Consultar zona DNS"},
                    {"id": "list-zones", "label": "Listar zonas DNS"},
                ],
            },
        )
        subject.brain_runtime.action_labels.assert_called_once_with(
            provider="openai",
            model="gpt-5.6-terra",
            api_key="private-model-key",
            language_exemplar="Quero listar minhas zonas DNS",
            action_ids=("get-zone", "list-zones"),
        )
        self.assertEqual(subject._active_chat_assistants.call_count, 2)

    def test_binding_or_model_drift_discards_generated_labels(self) -> None:
        subject = Subject()
        before = capabilities.ActionLabelSnapshot(
            "network-generation-1",
            "0.4.4",
            ("get-zone", "list-zones"),
            "openai",
            "gpt-5.6-terra",
        )
        after = capabilities.ActionLabelSnapshot(
            "network-generation-1",
            "0.4.5",
            ("get-zone", "list-zones"),
            "openai",
            "gpt-5.6-terra",
        )
        subject._action_label_snapshot = mock.Mock(side_effect=(before, after))

        with self.assertRaises(ApiProblemError) as caught:
            capabilities.action_labels(
                subject,
                "team_1",
                "cloudflare-assistant",
                {"language_exemplar": "Liste minhas zonas"},
                "openai",
                "private-model-key",
            )

        self.assertEqual(caught.exception.code, "team-context-changed")
        self.assertEqual(caught.exception.status, HTTPStatus.CONFLICT)

    def test_invalid_input_and_brain_failure_are_bounded(self) -> None:
        subject = Subject()
        for body in ({}, {"language_exemplar": ""}, {"language_exemplar": "hidden\0instruction"}):
            with self.subTest(body=body), self.assertRaises(ApiProblemError):
                capabilities.action_labels(
                    subject,
                    "team_1",
                    "cloudflare-assistant",
                    body,
                    "openai",
                    "private-model-key",
                )
        subject.brain_runtime.action_labels.assert_not_called()

        subject.brain_runtime.action_labels.side_effect = brain_runtime_client.BrainRuntimeError(
            "provider leaked private-model-key"
        )
        with self.assertRaises(ApiProblemError) as caught:
            capabilities.action_labels(
                subject,
                "team_1",
                "cloudflare-assistant",
                {"language_exemplar": "Liste minhas zonas"},
                "openai",
                "private-model-key",
            )
        self.assertEqual(caught.exception.code, "action-labels-unavailable")
        self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertNotIn("private-model-key", caught.exception.message)

    def test_snapshot_rejects_missing_assistant_provider_and_inference_state(self) -> None:
        subject = Subject()
        subject._active_chat_assistants.return_value = ()
        with self.assertRaises(ApiProblemError) as missing:
            capabilities._action_label_snapshot(subject, "team_1", "cloudflare-assistant", "openai")
        self.assertEqual(missing.exception.code, "assistant-unavailable")

        subject._active_chat_assistants.return_value = (SimpleNamespace(spec=subject.spec),)
        with self.assertRaises(ApiProblemError) as mismatch:
            capabilities._action_label_snapshot(subject, "team_1", "cloudflare-assistant", "anthropic")
        self.assertEqual(mismatch.exception.code, "inference-provider-mismatch")

        subject.inference_store.load.side_effect = inference_config.InferenceConfigError("private state")
        with self.assertRaises(ApiProblemError) as unavailable:
            capabilities._action_label_snapshot(subject, "team_1", "cloudflare-assistant", "openai")
        self.assertEqual(unavailable.exception.code, "inference-not-configured")


if __name__ == "__main__":
    unittest.main()
