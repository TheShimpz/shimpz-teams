import unittest
from unittest import mock

from power import challenges, human


def requirement() -> challenges.HumanRequirement:
    descriptor = {
        "kind": "approval",
        "ordinal": 0,
        "title": "Publish record",
        "description": "Publish the reviewed DNS record.",
    }
    descriptor["fingerprint"] = human._fingerprint(descriptor)
    return challenges.HumanRequirement(
        "cloudflare",
        "Cloudflare",
        "publish-record",
        "Publish one DNS record.",
        "interrupt-1",
        human.validate_request(descriptor, ("approval",)),
    )


class HumanChallengeTests(unittest.TestCase):
    def test_challenge_is_team_bound_and_one_use(self) -> None:
        store = challenges.HumanChallengeStore()
        pending = store.create("team_1", requirement(), object())

        self.assertIs(store.get("team_1", pending.id), pending)
        with self.assertRaises(challenges.HumanChallengeNotFoundError):
            store.get("team_2", pending.id)
        self.assertIs(store.claim("team_1", pending.id), pending)
        with self.assertRaises(challenges.HumanChallengeNotFoundError):
            store.get("team_1", pending.id)

    def test_projection_contains_only_public_reviewed_context(self) -> None:
        store = challenges.HumanChallengeStore()
        pending = store.create("team_1", requirement(), {"private": "must-not-project"})

        with mock.patch.object(challenges.time, "monotonic", return_value=pending.expires_at - 299):
            payload = challenges.challenge_payload(pending)

        self.assertEqual(payload["status"], "human-required")
        self.assertEqual(payload["expires_in"], 299)
        self.assertEqual(payload["request"]["kind"], "approval")
        self.assertNotIn("private", repr(payload))

    def test_expired_payload_is_drained_once_for_dependent_cleanup(self) -> None:
        clock = [100.0]
        store = challenges.HumanChallengeStore(ttl_seconds=30, clock=lambda: clock[0])
        pending = store.create("team_1", requirement(), {"continuation": "opaque"})
        clock[0] = 130.0

        self.assertEqual(store.drain_expired(), (pending,))
        self.assertEqual(store.drain_expired(), ())
        self.assertIsNone(store.current("team_1"))


if __name__ == "__main__":
    unittest.main()
