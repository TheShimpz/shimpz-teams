from __future__ import annotations

import unittest
from unittest import mock

from integrations import challenges as integration_challenges


def requirement() -> integration_challenges.IntegrationRequirement:
    return integration_challenges.IntegrationRequirement(
        assistant_id="shimpz-cloudflare",
        assistant_name="Shimpz Cloudflare",
        action_ids=("protected-action", "read-profile"),
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

    def test_withdrawal_returns_exactly_the_cancelled_challenge_once(self) -> None:
        store = integration_challenges.IntegrationChallengeStore()
        payload = object()
        challenge = store.create("team_1", (requirement(),), payload)
        other = store.create("team_2", (requirement(),), object())

        withdrawn = store.withdraw_team("team_1")

        self.assertIs(withdrawn, challenge)
        self.assertIs(withdrawn.payload, payload)
        self.assertIsNone(store.withdraw_team("team_1"))
        with self.assertRaises(integration_challenges.IntegrationChallengeNotFoundError):
            store.claim("team_1", challenge.id)
        # A claimed challenge already belongs to its responder, so a later withdrawal finds nothing to end.
        self.assertIs(store.claim("team_2", other.id), other)
        self.assertIsNone(store.withdraw_team("team_2"))

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

    def test_generic_configuration_collision_restore_and_commit_edges(self) -> None:
        contract = integration_challenges._CONTRACT
        invalid = integration_challenges.challenge_store.ChallengeContract(
            None,
            bool,
            integration_challenges.IntegrationChallengeError,
            integration_challenges.IntegrationChallengeNotFoundError,
            "integration",
        )
        with self.assertRaisesRegex(ValueError, "configuration"):
            integration_challenges.challenge_store.ChallengeStore(invalid)
        with self.assertRaisesRegex(ValueError, "configuration"):
            integration_challenges.challenge_store.ChallengeStore(contract, retain_expired=1)

        store = integration_challenges.IntegrationChallengeStore(capacity=1, ttl_seconds=30)
        with mock.patch.object(
            integration_challenges.challenge_store.secrets,
            "token_hex",
            side_effect=("a" * 32, "b" * 32),
        ):
            store._pending["a" * 32] = contract.pending_type(
                "a" * 32,
                "seed",
                float("inf"),
                (requirement(),),
                object(),
            )
            store._capacity = 2
            created = store.create("team_1", (requirement(),), object())
        self.assertEqual(created.id, "b" * 32)

        for remaining, metadata in ((0, (requirement(),)), (1, ())):
            with (
                self.subTest(remaining=remaining, metadata=metadata),
                self.assertRaisesRegex(
                    integration_challenges.IntegrationChallengeError,
                    "restore is invalid",
                ),
            ):
                store.restore("team_2", "c" * 32, remaining, metadata, object())

        with self.assertRaisesRegex(integration_challenges.IntegrationChallengeError, "already"):
            store.restore("team_1", "c" * 32, 1, (requirement(),), object())
        with self.assertRaisesRegex(integration_challenges.IntegrationChallengeError, "capacity"):
            store.restore("team_2", "c" * 32, 1, (requirement(),), object())

        with self.assertRaisesRegex(integration_challenges.IntegrationChallengeError, "commit is invalid"):
            store.claim_after("team_1", created.id, None)

    def test_claim_and_expiry_keep_foreign_reverse_index_untouched(self) -> None:
        store = integration_challenges.IntegrationChallengeStore(ttl_seconds=30)
        challenge = store.create("team_1", (requirement(),), object())
        store._by_team["team_1"] = "foreign"
        self.assertEqual(store.claim("team_1", challenge.id), challenge)
        self.assertEqual(store._by_team["team_1"], "foreign")

        with mock.patch.object(integration_challenges.time, "monotonic", return_value=1.0):
            store.create("team_2", (requirement(),), object())
        store._by_team["team_2"] = "foreign"
        with mock.patch.object(integration_challenges.time, "monotonic", return_value=31.0):
            self.assertIsNone(store.current("team_1"))
        self.assertEqual(store._by_team["team_2"], "foreign")

    def test_abandoned_continuations_expire_without_retaining_their_payloads(self) -> None:
        # No Integration consumer drains expiries, so each abandoned prompt's continuation is dropped at expiry.
        store = integration_challenges.IntegrationChallengeStore(capacity=1, ttl_seconds=30)
        for attempt in range(100):
            with mock.patch.object(integration_challenges.time, "monotonic", return_value=attempt * 31.0):
                store.create("team_1", (requirement(),), {"continuation": f"private input {attempt}"})
        with mock.patch.object(integration_challenges.time, "monotonic", return_value=100 * 31.0):
            self.assertIsNone(store.current("team_1"))

        self.assertFalse(store.cancel_team("team_1"))
        self.assertNotIn("private input", repr(vars(store)))
        with self.assertRaisesRegex(integration_challenges.IntegrationChallengeError, "do not retain"):
            store.drain_expired()


if __name__ == "__main__":
    unittest.main()
