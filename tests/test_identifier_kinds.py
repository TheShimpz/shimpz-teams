"""Every Team boundary admits an identifier by its own kind, as the Team protocol defines it."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from action import stored_input
from integrations import flow, pkce, service
from integrations import store as integration_store
from protocol.http.v1 import payload as http_payload
from storage import private_state

ASSISTANT_KIND = (("a" * 40,), ("a" * 41, "a--b", "a.b", "A"))
IDENTIFIER_KIND = (("a" * 64, "api-token"), ("a" * 65, "api.token", "api_token", "A", None))
ACTION_KIND = (("dns.read", "zone_get", "a" * 128), ("a" * 129, "dns..read", "A"))


def _cases():
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


if __name__ == "__main__":
    unittest.main()
