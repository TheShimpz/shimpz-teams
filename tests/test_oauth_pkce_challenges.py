from __future__ import annotations

import base64
import hashlib
import unittest
from unittest import mock

from integrations import pkce as integration_pkce

SESSION_ONE = "session-binding-one-123456789"
SESSION_TWO = "session-binding-two-123456789"
SCOPES = ("dns.read", "offline_access", "zone.read")


def create(
    store: integration_pkce.OAuthPKCEChallengeStore,
    *,
    session: str = SESSION_ONE,
    team: str = "team_1",
    assistant: str = "shimpz-cloudflare",
    integration: str = "cloudflare",
    resource_binding=None,
):
    return store.create(
        session_binding=session,
        team_id=team,
        assistant_id=assistant,
        integration_id=integration,
        provider_id="cloudflare",
        scopes=SCOPES,
        resource_binding=resource_binding,
    )


class OAuthPKCEChallengeTests(unittest.TestCase):
    def test_s256_verifier_is_private_bound_and_single_use(self) -> None:
        store = integration_pkce.OAuthPKCEChallengeStore()
        challenge = create(store)

        self.assertEqual(challenge.code_challenge_method, "S256")
        self.assertEqual(challenge.expires_in, 600)
        self.assertNotIn("verifier", repr(challenge).lower())
        self.assertNotIn(SESSION_ONE, repr(store._pending))
        for mismatched in (
            {"session_binding": SESSION_TWO},
            {"team_id": "team_2"},
            {"assistant_id": "other-assistant"},
            {"integration_id": "other"},
        ):
            binding = {
                "session_binding": SESSION_ONE,
                "team_id": "team_1",
                "assistant_id": "shimpz-cloudflare",
                "integration_id": "cloudflare",
            }
            binding.update(mismatched)
            with (
                self.subTest(mismatched=mismatched),
                self.assertRaises(integration_pkce.OAuthChallengeNotFoundError),
            ):
                store.claim(state=challenge.state, **binding)

        exchange = store.claim(
            state=challenge.state,
            session_binding=SESSION_ONE,
            team_id="team_1",
            assistant_id="shimpz-cloudflare",
            integration_id="cloudflare",
        )
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(exchange.code_verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        self.assertEqual(expected, challenge.code_challenge)
        self.assertEqual(exchange.provider_id, "cloudflare")
        self.assertEqual(exchange.scopes, tuple(sorted(SCOPES)))
        self.assertEqual(
            (exchange.team_id, exchange.assistant_id, exchange.integration_id),
            ("team_1", "shimpz-cloudflare", "cloudflare"),
        )
        with self.assertRaises(integration_pkce.OAuthChallengeNotFoundError):
            store.claim(
                state=challenge.state,
                session_binding=SESSION_ONE,
                team_id="team_1",
                assistant_id="shimpz-cloudflare",
                integration_id="cloudflare",
            )

    def test_callback_recovers_private_binding_only_for_the_starting_browser(self) -> None:
        store = integration_pkce.OAuthPKCEChallengeStore()
        resource_binding = ("a" * 32, "shimpz-team-team_1")
        challenge = create(store, resource_binding=resource_binding)

        with self.assertRaises(integration_pkce.OAuthChallengeNotFoundError):
            store.claim_callback(state=challenge.state, session_binding=SESSION_TWO)
        inspected = store.inspect_callback(state=challenge.state, session_binding=SESSION_ONE)
        self.assertEqual(
            inspected,
            integration_pkce.OAuthCallbackBinding(
                team_id="team_1",
                assistant_id="shimpz-cloudflare",
                integration_id="cloudflare",
                resource_binding=resource_binding,
            ),
        )
        self.assertNotIn("verifier", repr(inspected).lower())
        self.assertNotIn(SESSION_ONE, repr(inspected))
        exchange = store.claim_callback(state=challenge.state, session_binding=SESSION_ONE)
        self.assertEqual(exchange.provider_id, "cloudflare")
        self.assertEqual(exchange.team_id, "team_1")
        self.assertEqual(exchange.assistant_id, "shimpz-cloudflare")
        self.assertEqual(exchange.integration_id, "cloudflare")
        self.assertEqual(exchange.resource_binding, resource_binding)
        self.assertNotIn(SESSION_ONE, repr(exchange))
        with self.assertRaises(integration_pkce.OAuthChallengeNotFoundError):
            store.claim_callback(state=challenge.state, session_binding=SESSION_ONE)

    def test_expiry_and_binding_collision_fail_closed(self) -> None:
        store = integration_pkce.OAuthPKCEChallengeStore(ttl_seconds=30)
        with mock.patch.object(integration_pkce.time, "monotonic", return_value=100.0):
            challenge = create(store)
            with self.assertRaisesRegex(integration_pkce.OAuthChallengeError, "pending"):
                create(store)
        with (
            mock.patch.object(integration_pkce.time, "monotonic", return_value=130.0),
            self.assertRaises(integration_pkce.OAuthChallengeNotFoundError),
        ):
            store.claim(
                state=challenge.state,
                session_binding=SESSION_ONE,
                team_id="team_1",
                assistant_id="shimpz-cloudflare",
                integration_id="cloudflare",
            )

    def test_global_session_and_team_caps_are_independent(self) -> None:
        global_store = integration_pkce.OAuthPKCEChallengeStore(
            capacity=1,
            per_session=1,
            per_team=1,
        )
        create(global_store)
        with self.assertRaisesRegex(integration_pkce.OAuthChallengeError, "capacity"):
            create(global_store, session=SESSION_TWO, team="team_2")

        session_store = integration_pkce.OAuthPKCEChallengeStore(
            capacity=3,
            per_session=1,
            per_team=3,
        )
        create(session_store)
        with self.assertRaisesRegex(integration_pkce.OAuthChallengeError, "session"):
            create(session_store, team="team_2")

        team_store = integration_pkce.OAuthPKCEChallengeStore(
            capacity=3,
            per_session=3,
            per_team=1,
        )
        create(team_store)
        with self.assertRaisesRegex(integration_pkce.OAuthChallengeError, "Team"):
            create(team_store, session=SESSION_TWO, assistant="other-assistant")

    def test_cancel_is_scoped_and_invalid_inputs_never_create_state(self) -> None:
        store = integration_pkce.OAuthPKCEChallengeStore(capacity=4, per_session=4, per_team=4)
        first = create(store)
        second = create(store, session=SESSION_TWO, team="team_2")
        self.assertEqual(store.cancel_session(SESSION_ONE), 1)
        with self.assertRaises(integration_pkce.OAuthChallengeNotFoundError):
            store.claim(
                state=first.state,
                session_binding=SESSION_ONE,
                team_id="team_1",
                assistant_id="shimpz-cloudflare",
                integration_id="cloudflare",
            )
        self.assertEqual(store.cancel_team("team_2"), 1)
        self.assertEqual(store.cancel_all(), 0)
        with self.assertRaises(integration_pkce.OAuthChallengeNotFoundError):
            store.claim(
                state=second.state,
                session_binding=SESSION_TWO,
                team_id="team_2",
                assistant_id="shimpz-cloudflare",
                integration_id="cloudflare",
            )

        for invalid in (
            {"session_binding": "short"},
            {"team_id": "../team"},
            {"assistant_id": "Assistant"},
            {"integration_id": "x/evil"},
            {"provider_id": "evil"},
            {"scopes": ("dm.read",)},
            {"resource_binding": ("only-one",)},
            {"resource_binding": ("valid", "\ninvalid")},
        ):
            arguments = {
                "session_binding": SESSION_ONE,
                "team_id": "team_1",
                "assistant_id": "shimpz-cloudflare",
                "integration_id": "cloudflare",
                "provider_id": "cloudflare",
                "scopes": SCOPES,
            }
            arguments.update(invalid)
            with self.subTest(invalid=invalid), self.assertRaises(RuntimeError):
                store.create(**arguments)
        self.assertEqual(store.cancel_all(), 0)

    def test_session_state_and_constructor_shape_edges_fail_closed(self) -> None:
        self.assertEqual(
            integration_pkce._session_digest(SESSION_ONE.encode("ascii")),
            integration_pkce._session_digest(SESSION_ONE),
        )
        for value in ("\ud800" * 16, object()):
            with (
                self.subTest(value=type(value).__name__),
                self.assertRaisesRegex(
                    integration_pkce.OAuthChallengeError,
                    "session binding",
                ),
            ):
                integration_pkce._session_digest(value)
        with self.assertRaises(integration_pkce.OAuthChallengeNotFoundError):
            integration_pkce._state("invalid")

        for options in (
            {"capacity": 0},
            {"per_session": 0},
            {"per_team": 0},
            {"ttl_seconds": 29},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                integration_pkce.OAuthPKCEChallengeStore(**options)

    def test_state_collision_remove_and_inspect_mismatch_edges(self) -> None:
        store = integration_pkce.OAuthPKCEChallengeStore()
        first = create(store)
        store._by_binding[
            integration_pkce.OAuthPKCEChallengeStore._binding(
                SESSION_ONE,
                "team_1",
                "shimpz-cloudflare",
                "cloudflare",
            )
        ] = "different-state"
        self.assertIsNotNone(store._remove(first.state))
        self.assertIsNone(store._remove(first.state))
        store._by_binding.clear()

        existing = create(store)
        with mock.patch.object(
            integration_pkce.secrets,
            "token_urlsafe",
            side_effect=(existing.state, "b" * 43, "v" * 64),
        ):
            created = create(store, session=SESSION_TWO, team="team_2")
        self.assertEqual(created.state, "b" * 43)

        with self.assertRaises(integration_pkce.OAuthChallengeNotFoundError):
            store.inspect_callback(state=created.state, session_binding=SESSION_ONE)


if __name__ == "__main__":
    unittest.main()
