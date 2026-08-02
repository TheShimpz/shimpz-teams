from __future__ import annotations

import unittest
from unittest import mock

from integrations import challenges as integration_challenges


def requirement() -> integration_challenges.IntegrationRequirement:
    return integration_challenges.IntegrationRequirement(
        assistant_id="shimpz-cloudflare",
        assistant_name="Shimpz Cloudflare",
        power_ids=("protected-action", "read-profile"),
        integrations=(
            (
                "x",
                "x",
                ("offline.access", "tweet.read", "tweet.write", "users.read"),
            ),
        ),
    )


class AssistantIntegrationChallengeTests(unittest.TestCase):
    def test_default_lifetime_encloses_the_brokered_oauth_transaction(self) -> None:
        store = integration_challenges.IntegrationChallengeStore()
        with mock.patch.object(integration_challenges.time, "monotonic", return_value=100.0):
            challenge = store.create("team_1", (requirement(),), object())

        self.assertEqual(challenge.expires_at, 700.0)

    def test_challenge_is_team_bound_single_use_and_keeps_payload_private(self) -> None:
        store = integration_challenges.IntegrationChallengeStore()
        private = {"continuation": "private user input"}
        challenge = store.create("team_1", (requirement(),), private)

        self.assertNotIn("private user input", repr(store._by_team))
        with self.assertRaises(integration_challenges.IntegrationChallengeNotFoundError):
            store.get("team_2", challenge.id)
        claimed = store.claim("team_1", challenge.id)
        self.assertIs(claimed.payload, private)
        with self.assertRaises(integration_challenges.IntegrationChallengeNotFoundError):
            store.claim("team_1", challenge.id)

    def test_one_pending_turn_per_team_and_global_capacity_fail_closed(self) -> None:
        store = integration_challenges.IntegrationChallengeStore(capacity=2)
        store.create("team_1", (requirement(),), object())
        with self.assertRaisesRegex(
            integration_challenges.IntegrationChallengeError,
            "already",
        ):
            store.create("team_1", (requirement(),), object())
        store.create("team_2", (requirement(),), object())
        with self.assertRaisesRegex(
            integration_challenges.IntegrationChallengeError,
            "capacity",
        ):
            store.create("team_3", (requirement(),), object())

    def test_expiry_cancel_and_invalid_identifiers_remove_no_other_team(self) -> None:
        store = integration_challenges.IntegrationChallengeStore(ttl_seconds=30)
        with mock.patch.object(integration_challenges.time, "monotonic", return_value=1.0):
            expired = store.create("team_1", (requirement(),), object())
        with (
            mock.patch.object(integration_challenges.time, "monotonic", return_value=31.0),
            self.assertRaises(integration_challenges.IntegrationChallengeNotFoundError),
        ):
            store.get("team_1", expired.id)

        active = store.create("team_2", (requirement(),), object())
        for team, identifier in (("../team", active.id), ("team_2", "not-a-challenge")):
            with self.subTest(team=team, identifier=identifier), self.assertRaises(RuntimeError):
                store.get(team, identifier)
        self.assertTrue(store.cancel_team("team_2"))
        self.assertFalse(store.cancel_team("team_2"))
        self.assertEqual(store.cancel_all(), 0)

    def test_empty_requirements_and_invalid_limits_are_rejected(self) -> None:
        with self.assertRaises(integration_challenges.IntegrationChallengeError):
            integration_challenges.IntegrationChallengeStore().create("team_1", (), object())
        for options in (
            {"capacity": 0},
            {"capacity": True},
            {"ttl_seconds": 29},
            {"ttl_seconds": 901},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                integration_challenges.IntegrationChallengeStore(**options)

    def test_authenticated_restore_preserves_id_payload_and_remaining_ttl(self) -> None:
        store = integration_challenges.IntegrationChallengeStore(ttl_seconds=30)
        private = object()
        with mock.patch.object(integration_challenges.time, "monotonic", return_value=10.0):
            restored = store.restore("team_1", "a" * 32, 7, (requirement(),), private)
        self.assertEqual(restored.id, "a" * 32)
        self.assertIs(restored.payload, private)
        with mock.patch.object(integration_challenges.time, "monotonic", return_value=17.0):
            self.assertIsNone(store.current("team_1"))


if __name__ == "__main__":
    unittest.main()
