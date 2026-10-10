"""Team's confirmation request, its transcript slot, and the escaped input projection of a confirmation card."""

import unittest
from types import SimpleNamespace

from action import confirmation, human
from protocol.http.v1 import challenge as http_challenge
from tests import human_request_fixtures

BINDING = ("shimpz-cloudflare", "sha256:" + "a" * 64, "container")


def _request(arguments: dict[str, object], binding: tuple[str, str, str] = BINDING) -> human.HumanRequest:
    return confirmation.request("team_1", binding, "ensure-record", "action-1", arguments)


class ConfirmationRequestTests(unittest.TestCase):
    def test_the_request_binds_every_argument_and_the_exact_binding(self) -> None:
        base = _request({"name": "www", "ttl": 300})
        self.assertEqual(base, _request({"ttl": 300, "name": "www"}))
        self.assertEqual(
            (base.kind, base.ordinal, base.catalog, base.payload()["policy"]),
            ("confirmation", 0, b"[]", "mutating-actions"),
        )
        changed = (
            _request({"name": "www", "ttl": 301}),
            _request({"name": "www", "ttl": 300, "proxied": True}),
            _request({"name": "www", "ttl": 300}, (BINDING[0], "sha256:" + "b" * 64, BINDING[2])),
            _request({"name": "www", "ttl": 300}, (BINDING[0], BINDING[1], "replaced")),
            confirmation.request("team_2", BINDING, "ensure-record", "action-1", {"name": "www", "ttl": 300}),
            confirmation.request("team_1", BINDING, "delete-record", "action-1", {"name": "www", "ttl": 300}),
            confirmation.request("team_1", BINDING, "ensure-record", "action-2", {"name": "www", "ttl": 300}),
        )
        self.assertEqual(len({item.fingerprint for item in (base, *changed)}), len(changed) + 1)
        confirmed = human.ActionTranscript("action-1").confirm(base, True)
        self.assertTrue(confirmed.confirmed(base))
        self.assertFalse(any(confirmed.confirmed(item) for item in changed))
        self.assertFalse(human.ActionTranscript("action-1").confirmed(base))

    def test_the_confirmation_is_given_once_before_the_action_runs_and_never_replayed(self) -> None:
        request = _request({"name": "www"})
        approval = human_request_fixtures.request("approval")
        admitted = human.append_response((), "action-1", request, True, 0)
        transcript = admitted.transcripts[0]
        self.assertEqual((admitted.requests_used, transcript.payloads()), (1, ()))
        for refused in (
            lambda: transcript.confirm(request, True),
            lambda: human.ActionTranscript("action-1").confirm(request, False),
            lambda: human.ActionTranscript("action-1").confirm(approval, True),
            lambda: human.ActionTranscript("action-1").append(approval, True).confirm(request, True),
        ):
            with self.subTest(refused=refused), self.assertRaises(human.HumanRequestError):
                refused()
        # The Action's own first request still has ordinal 0 after Team's confirmation.
        answered = human.append_response(admitted.transcripts, "action-1", approval, True, 1)
        self.assertEqual(answered.transcripts[0].payloads()[0]["ordinal"], 0)
        self.assertEqual(answered.transcripts[0].confirmation, transcript.confirmation)

    def test_a_recorded_request_is_restored_only_with_its_own_fingerprint(self) -> None:
        request = _request({"name": "www"})
        self.assertEqual(human.restore_confirmation_request(request.payload()), request)
        valid = request.payload()
        for value in (
            None,
            {**valid, "extra": 1},
            {**valid, "kind": "approval"},
            {**valid, "ordinal": 1},
            {**valid, "ordinal": False},
            {**valid, "policy": "other"},
            {**valid, "binding": "A" * 64},
            {**valid, "fingerprint": 1},
            {**valid, "fingerprint": "0" * 64},
        ):
            with self.subTest(value=value), self.assertRaises(human.HumanRequestError):
                human.restore_confirmation_request(value)

    def test_the_policy_confirms_only_mutating_actions_without_authorization(self) -> None:
        cases = (
            (SimpleNamespace(effect="mutating", human_requests=()), True, True),
            (SimpleNamespace(effect="mutating", human_requests=("input:text",)), True, True),
            (SimpleNamespace(effect="mutating", human_requests=()), False, False),
            (SimpleNamespace(effect="read_only", human_requests=()), True, False),
            (SimpleNamespace(effect="mutating", human_requests=("auth:password",)), True, False),
            (SimpleNamespace(effect="mutating", human_requests=("approval",)), True, False),
        )
        for action, enabled, expected in cases:
            with self.subTest(action=action, enabled=enabled):
                self.assertIs(confirmation.required(action, enabled), expected)


class InputProjectionTests(unittest.TestCase):
    def test_every_argument_is_shown_as_escaped_quoted_json(self) -> None:
        projection = confirmation.input_projection(
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
        many = confirmation.input_projection({f"field{index:02d}": index for index in range(20)})
        self.assertEqual((len(many["fields"]), many["omitted"]), (16, 4))
        long = confirmation.input_projection({"a": "x" * 500, "b" + "y" * 200: 1, "c": "x" * 396 + "‮"})
        rows = {field["name"][:1]: field for field in long["fields"]}
        self.assertTrue(rows["a"]["truncated"])
        self.assertEqual(len(rows["a"]["value"]), 400)
        self.assertTrue(rows["b"]["truncated"])
        self.assertEqual(len(rows["b"]["name"]), 128)
        # The bound falls inside the escape of the final control character, which is dropped whole.
        self.assertEqual(rows["c"]["value"], '"' + "x" * 396)
        self.assertTrue(rows["c"]["truncated"])
        # An escaped backslash before a "u" is ordinary text, so the bound keeps it.
        backslash = confirmation.input_projection({"a": "x" * 396 + "\\u"})
        self.assertEqual(backslash["fields"][0]["value"], '"' + "x" * 396 + "\\\\u")
        self.assertTrue(backslash["fields"][0]["truncated"])

    def test_a_projection_the_wire_refuses_is_never_produced(self) -> None:
        colliding = {"a" * 128 + "1": 1, "a" * 128 + "2": 2}
        with self.assertRaises(ValueError):
            confirmation.input_projection(colliding)


if __name__ == "__main__":
    unittest.main()
