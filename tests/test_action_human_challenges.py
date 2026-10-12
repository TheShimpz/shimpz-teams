import dataclasses
import unittest
from unittest import mock

from action import challenges
from protocol.http.v1 import challenge as http_challenge
from tests import human_request_fixtures


def requirement() -> challenges.HumanRequirement:
    return human_request_fixtures.requirement(
        human_request_fixtures.request("approval", title="Publish record", description="Publish the reviewed record."),
        assistant_id="cloudflare",
        assistant_name="Cloudflare",
        action_id="publish-record",
        action_summary="Publish one DNS record.",
        assistant_version="0.4.1",
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

    def test_reissue_replaces_the_id_and_metadata_but_keeps_the_payload_and_expiry(self) -> None:
        clock = [100.0]
        store = challenges.HumanChallengeStore(clock=lambda: clock[0])
        continuation = {"continuation": "opaque"}
        pending = store.create("team_1", requirement(), continuation)
        localized = human_request_fixtures.requirement(pending.requirement.request, locale="pt")
        clock[0] = 160.0
        # A fresh id never collides with a live challenge (ADR-0091: a locale change needs a fresh challenge).
        with mock.patch.object(challenges.challenge_store.secrets, "token_hex", side_effect=(pending.id, "e" * 32)):
            fresh = store.reissue("team_1", pending.id, localized)

        self.assertEqual(
            (fresh.id, fresh.team_id, fresh.expires_at, fresh.requirement, fresh.payload),
            ("e" * 32, "team_1", pending.expires_at, localized, continuation),
        )
        self.assertIs(store.current("team_1"), fresh)
        with self.assertRaises(challenges.HumanChallengeNotFoundError):
            store.get("team_1", pending.id)
        with self.assertRaises(challenges.HumanChallengeNotFoundError):
            store.reissue("team_2", fresh.id, localized)
        with self.assertRaisesRegex(challenges.HumanChallengeError, "metadata"):
            store.reissue("team_1", fresh.id, object())
        clock[0] = fresh.expires_at
        with self.assertRaises(challenges.HumanChallengeNotFoundError):
            store.reissue("team_1", fresh.id, localized)

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
        store = challenges.HumanChallengeStore(ttl_seconds=30, clock=lambda: clock[0], retain_expired=True)
        pending = store.create("team_1", requirement(), {"continuation": "opaque"})
        other = store.create("team_2", requirement(), {"continuation": "other"})
        clock[0] = 130.0

        # One Team's drain, as its destruction runs it, leaves every other Team's expiry for that Team's consumer.
        self.assertEqual(store.drain_expired("team_1"), (pending,))
        self.assertEqual(store.drain_expired(), (other,))
        self.assertEqual(store.drain_expired(), ())
        self.assertIsNone(store.current("team_1"))

    def test_projection_rejects_wrong_type_and_expired_challenge(self) -> None:
        with self.assertRaises(challenges.HumanChallengeError):
            challenges.challenge_payload(object())
        pending = challenges.HumanChallengeStore().create("team_1", requirement(), object())
        with (
            mock.patch.object(challenges.time, "monotonic", return_value=pending.expires_at),
            self.assertRaisesRegex(challenges.HumanChallengeError, "expired"),
        ):
            challenges.challenge_payload(pending)


class InputProjectionTests(unittest.TestCase):
    def test_every_argument_is_shown_as_escaped_quoted_json(self) -> None:
        projection = challenges.input_projection(
            {
                "name": "www",
                "content": "v=spf1 ‮evil next\nline \U000e0001",
                "ttl": 300,
                "proxied": True,
                "tags": ["a", "b"],
                "nested": {"z": 1, "a": None},
                "flag": "true",
                "na​me": 1,
            }
        )
        rows = {field["name"]: field["value"] for field in projection["fields"]}
        self.assertEqual(projection["omitted"], 0)
        self.assertEqual(rows["name"], '"www"')
        self.assertEqual(rows["flag"], '"true"')
        self.assertEqual(rows["proxied"], "true")
        self.assertEqual(rows["tags"], '["a", "b"]')
        self.assertEqual(rows["nested"], '{"a": null, "z": 1}')
        self.assertEqual(rows["content"], '"v=spf1 \\u202eevil\\u2028next\\nline \\udb40\\udc01"')
        self.assertIn("na\\u200bme", rows)
        self.assertEqual(http_challenge.canonical_input_projection(projection), projection)

    def test_bounds_are_explicit_and_never_split_an_escape(self) -> None:
        many = challenges.input_projection({f"field{index:02d}": index for index in range(20)})
        self.assertEqual((len(many["fields"]), many["omitted"]), (16, 4))
        long = challenges.input_projection({"a": "x" * 500, "b" + "y" * 200: 1, "c": "x" * 396 + "‮"})
        rows = {field["name"][:1]: field for field in long["fields"]}
        self.assertTrue(rows["a"]["truncated"])
        self.assertEqual(len(rows["a"]["value"]), 400)
        self.assertTrue(rows["b"]["truncated"])
        self.assertEqual(len(rows["b"]["name"]), 128)
        # The bound falls inside the escape of the final control character, which is dropped whole.
        self.assertEqual(rows["c"]["value"], '"' + "x" * 396)
        self.assertTrue(rows["c"]["truncated"])
        # An escaped backslash before a "u" is ordinary text, so the bound keeps it.
        backslash = challenges.input_projection({"a": "x" * 396 + "\\u"})
        self.assertEqual(backslash["fields"][0]["value"], '"' + "x" * 396 + "\\\\u")
        self.assertTrue(backslash["fields"][0]["truncated"])

    def test_a_projection_the_wire_refuses_is_never_produced(self) -> None:
        colliding = {"a" * 128 + "1": 1, "a" * 128 + "2": 2}
        with self.assertRaises(ValueError):
            challenges.input_projection(colliding)

    def test_only_an_authorization_card_carries_the_projection(self) -> None:
        shown = challenges.input_projection({"zone": "example.com"})
        store = challenges.HumanChallengeStore()
        pending = store.create("team_1", dataclasses.replace(requirement(), input=shown), object())
        self.assertEqual(challenges.challenge_payload(pending)["input"], shown)
        text = human_request_fixtures.requirement(
            human_request_fixtures.request(
                "input:text", label="Zone", required=True, placeholder=None, min_length=1, max_length=64
            ),
            input=shown,
        )
        with self.assertRaises(challenges.HumanChallengeError):
            store.create("team_1", text, object())


if __name__ == "__main__":
    unittest.main()
