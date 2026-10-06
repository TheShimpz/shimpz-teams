"""Every Team boundary admits an identifier by its own kind, as the Team protocol defines it."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from action import stored_input
from assistant import manifest
from inference import client as brain_client
from integrations import flow, pkce, service
from integrations import store as integration_store
from local.chat import continuation
from local.install import snapshots
from protocol.http.v1 import payload as http_payload
from storage import private_state

ASSISTANT_KIND = (("a" * 40,), ("a" * 41, "a--b", "a.b", "A"))
IDENTIFIER_KIND = (("a" * 64, "api-token"), ("a" * 65, "api.token", "api_token", "A", None))
ACTION_KIND = (("dns.read", "zone_get", "a" * 128), ("a" * 129, "dns..read", "A"))


def _cases():
    error = continuation.ContinuationCodecError
    yield continuation._component_id, error, http_payload.canonical_assistant_id, ASSISTANT_KIND
    yield continuation._component_id, error, http_payload.canonical_identifier, IDENTIFIER_KIND
    yield continuation._component_id, error, http_payload.canonical_action_id, ACTION_KIND
    yield flow._component_id, flow.IntegrationFlowError, http_payload.canonical_assistant_id, ASSISTANT_KIND
    yield flow._component_id, flow.IntegrationFlowError, http_payload.canonical_identifier, IDENTIFIER_KIND
    yield flow._component_id, flow.IntegrationFlowError, http_payload.canonical_action_id, ACTION_KIND
    yield pkce._component_id, pkce.OAuthChallengeError, http_payload.canonical_assistant_id, ASSISTANT_KIND
    yield pkce._component_id, pkce.OAuthChallengeError, http_payload.canonical_identifier, IDENTIFIER_KIND
    yield service._identifier, service.OAuthIntegrationServiceError, http_payload.canonical_assistant_id, ASSISTANT_KIND
    yield service._identifier, service.OAuthIntegrationServiceError, http_payload.canonical_identifier, IDENTIFIER_KIND
    yield (
        integration_store._component_id,
        integration_store.OAuthIntegrationValidationError,
        http_payload.canonical_assistant_id,
        ASSISTANT_KIND,
    )
    yield (
        integration_store._component_id,
        integration_store.OAuthIntegrationValidationError,
        http_payload.canonical_identifier,
        IDENTIFIER_KIND,
    )
    yield (
        stored_input._component_id,
        stored_input.StoredInputValidationError,
        http_payload.canonical_assistant_id,
        ASSISTANT_KIND,
    )
    yield (
        stored_input._component_id,
        stored_input.StoredInputValidationError,
        http_payload.canonical_identifier,
        IDENTIFIER_KIND,
    )


class IdentifierKindTests(unittest.TestCase):
    def test_each_boundary_admits_exactly_its_identifier_kind(self) -> None:
        for admit, error, canonical, (valid, invalid) in _cases():
            for value in valid:
                with self.subTest(admit=admit.__module__, kind=canonical.__name__, value=value):
                    self.assertEqual(admit(value, "id", canonical), value)
            for value in invalid:
                with (
                    self.subTest(admit=admit.__module__, kind=canonical.__name__, value=value),
                    self.assertRaises(error),
                ):
                    admit(value, "id", canonical)

    def test_shared_record_stores_own_records_by_a_canonical_assistant(self) -> None:
        for policy, error in (
            (stored_input._POLICY, stored_input.StoredInputValidationError),
            (integration_store._POLICY, integration_store.OAuthIntegrationValidationError),
        ):
            store = SimpleNamespace(_policy=policy)
            with self.subTest(policy=policy.label):
                self.assertEqual(private_state.RecordStore._owner(store, "team_1", "a" * 40), ("team_1", "a" * 40))
                for assistant_id in ("a" * 41, "a.b"):
                    with self.assertRaises(error):
                        private_state.RecordStore._owner(store, "team_1", assistant_id)

    def test_an_identifier_is_the_default_kind(self) -> None:
        for admit, error in (
            (flow._component_id, flow.IntegrationFlowError),
            (pkce._component_id, pkce.OAuthChallengeError),
            (service._identifier, service.OAuthIntegrationServiceError),
            (integration_store._component_id, integration_store.OAuthIntegrationValidationError),
            (stored_input._component_id, stored_input.StoredInputValidationError),
        ):
            with self.subTest(admit=admit.__module__):
                self.assertEqual(admit("a" * 64, "id"), "a" * 64)
                with self.assertRaises(error):
                    admit("a" * 65, "id")


class DevelopersIdentifierAdmissionTests(unittest.TestCase):
    """A reviewed Assistant declares its Action, Integration, and Stored Input ids as Developers identifiers."""

    def test_manifest_identifiers_are_developers_identifiers(self) -> None:
        for kind in ("Action", "integration", "Stored Input"):
            with self.subTest(kind=kind):
                self.assertEqual(manifest._identifier("a" * 64, kind=kind), "a" * 64)
                for value in ("a" * 65, "dns.read", "A"):
                    with self.assertRaises(manifest.ManifestError):
                        manifest._identifier(value, kind=kind)
        assistant = http_payload.canonical_assistant_id
        self.assertEqual(manifest._identifier("a" * 40, kind="id", canonical=assistant), "a" * 40)
        with self.assertRaises(manifest.ManifestError):
            manifest._identifier("a" * 41, kind="id", canonical=assistant)

    def test_snapshot_capability_labels_are_developers_identifiers(self) -> None:
        self.assertEqual(snapshots._capability_ids("a,b" + "c" * 63, maximum=2, required=True), ("a", "b" + "c" * 63))
        for value in ("a" * 65, "dns.read", "a,A"):
            with self.subTest(value=value), self.assertRaises(snapshots.LocalSnapshotError):
                snapshots._capability_ids(value, maximum=2, required=True)


class BrainRuntimeIdentifierTests(unittest.TestCase):
    """What Team sends the Brain and admits back names each identifier by its own kind."""

    Client = brain_client.BrainRuntimeClient

    def _turn(self, assistant_id: str, action: str) -> dict[str, object]:
        request = {"interrupt_id": "call-1", "assistant_id": assistant_id, "action": action, "input": {}}
        return {
            "status": "action-required",
            "reply": "",
            "actions": [request],
            "clarification": None,
            "memory": [],
            "routine": None,
        }

    def test_requested_actions_name_a_canonical_assistant_and_action(self) -> None:
        self.assertEqual(len(self.Client._parse_turn(self._turn("a" * 40, "dns.read")).actions), 1)
        for assistant_id, action in (("a" * 41, "lookup"), ("dns.read", "lookup"), ("helper", "a" * 129)):
            with (
                self.subTest(assistant_id=assistant_id, action=action),
                self.assertRaises(brain_client.BrainRuntimeError),
            ):
                self.Client._parse_turn(self._turn(assistant_id, action))

    def test_capability_candidates_name_each_identifier_by_kind(self) -> None:
        def candidate(assistant_id: str, action: str, integration: str, provider: str):
            return brain_client.RuntimeCapabilityCandidate(
                assistant_id,
                "Helper",
                "Helps.",
                (action,),
                (brain_client.RuntimeCapabilityIntegration(integration, provider),),
            )

        valid = candidate("a" * 40, "dns.read", "i" * 64, "p" * 64)
        self.assertEqual(self.Client.validate_capability_plan_inputs("Help me", (valid,))[1], (valid,))
        for invalid in (
            candidate("a" * 41, "lookup", "token", "cloudflare"),
            candidate("helper", "a" * 129, "token", "cloudflare"),
            candidate("helper", "lookup", "i" * 65, "cloudflare"),
            candidate("helper", "lookup", "api.token", "cloudflare"),
            candidate("helper", "lookup", "token", "p" * 65),
        ):
            with self.subTest(candidate=invalid), self.assertRaises(brain_client.BrainRuntimeError):
                self.Client.validate_capability_plan_inputs("Help me", (invalid,))

    def test_lifecycle_routes_name_canonical_assistants(self) -> None:
        context = brain_client.RuntimeLifecycleContext(locale="en")
        for assistant_id in ("a" * 41, "dns.read"):
            candidates = (brain_client.RuntimeDirectoryCandidate(assistant_id, "Helper"),)
            with self.subTest(assistant_id=assistant_id), self.assertRaises(brain_client.BrainRuntimeError):
                self.Client.validate_intent_route_inputs("Install it", "assistant-install", candidates, context)
            reference = brain_client.RuntimeLifecycleReference(assistant_id, "Helper")
            with self.subTest(reference=assistant_id), self.assertRaises(brain_client.BrainRuntimeError):
                self.Client.validate_intent_route_inputs(
                    "Remove it", None, (), brain_client.RuntimeLifecycleContext(reference, (), "en")
                )


if __name__ == "__main__":
    unittest.main()
