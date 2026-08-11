from __future__ import annotations

import unittest
from http import HTTPStatus
from types import SimpleNamespace
from unittest import mock

from hosted_assistant_fixture import runtime_state as state


class HostedStateEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {
            "active_tokens": dict(state._active_chat_tokens),
            "active_containers": dict(state._active_chat_container_ids),
            "active_actions": dict(state._active_action_container_ids),
            "blocked": set(state._blocked_action_workloads),
            "cancelled": set(state._cancelled_chat_tokens),
            "storage": state._storage_instance,
        }

    def tearDown(self) -> None:
        state._active_chat_tokens.clear()
        state._active_chat_tokens.update(self.saved["active_tokens"])
        state._active_chat_container_ids.clear()
        state._active_chat_container_ids.update(self.saved["active_containers"])
        state._active_action_container_ids.clear()
        state._active_action_container_ids.update(self.saved["active_actions"])
        state._blocked_action_workloads.clear()
        state._blocked_action_workloads.update(self.saved["blocked"])
        state._cancelled_chat_tokens.clear()
        state._cancelled_chat_tokens.update(self.saved["cancelled"])
        state._storage_instance = self.saved["storage"]

    def test_environment_and_memory_budget_values_must_be_positive_and_nested(self) -> None:
        with (
            mock.patch.dict(state.os.environ, {"LIMIT": "0"}),
            self.assertRaises(ValueError),
        ):
            state._positive_int_env("LIMIT", 1)
        with self.assertRaises(ValueError):
            state._validate_memory_budgets(10, 10, 11)
        with self.assertRaises(ValueError):
            state._validate_memory_budgets(20, 9, 10)
        state._validate_memory_budgets(20, 10, 10)

    def test_rate_storage_and_developer_dependencies_are_initialized_lazily(self) -> None:
        with self.assertRaises(ValueError):
            state._FixedWindowRateLimiter(0, 1)
        limiter = state._FixedWindowRateLimiter(1, 10)
        limiter._counts["key"] = (0, 1)
        limiter._last_bucket = 1
        self.assertEqual(limiter.consume("key", now=15), 0)
        limiter._counts["key"] = (0, 1)
        limiter._last_bucket = 1
        self.assertEqual(limiter.consume("key", now=15), 0)

        self.assertEqual(state._rate_key(("account", None)), "account:absent")
        with (
            mock.patch.dict(
                state._rate_limiters,
                {"test": SimpleNamespace(consume=mock.Mock(return_value=3))},
            ),
            self.assertRaises(state.ApiError) as caught,
        ):
            state._enforce_rate("test", ("account", "a" * 32))
        self.assertEqual(caught.exception.status, HTTPStatus.TOO_MANY_REQUESTS)
        with mock.patch.dict(
            state._rate_limiters,
            {"test": SimpleNamespace(consume=mock.Mock(return_value=0))},
        ):
            self.assertIsNone(state._enforce_rate("test", ("account", "a" * 32)))

        state._storage_instance = None
        storage = object()
        with mock.patch.object(state.team_storage, "TeamStorage", return_value=storage) as constructor:
            self.assertIs(state._storage(), storage)
            self.assertIs(state._storage(), storage)
        constructor.assert_called_once_with(state.TEAM_STORAGE_ROOT)

        delegation = object()
        client = object()
        registry = object()
        trust = object()
        with (
            mock.patch.object(state.developers_delegation, "DevelopersDelegationVerifier", return_value=delegation),
            mock.patch.object(state.developers_client, "DevelopersClient", return_value=client),
            mock.patch.object(state.registry_auth.RegistryAuth, "from_files", return_value=registry),
            mock.patch.object(state.artifact_trust, "ArtifactTrustVerifier", return_value=trust),
        ):
            state._initialize_developers_integration()
        self.assertIs(state._developers_delegation, delegation)
        self.assertIs(state._developers_client, client)
        self.assertIs(state._registry_auth, registry)
        self.assertIs(state._artifact_trust, trust)

    def test_terminal_state_cleanup_and_commit_cover_cancelled_and_active_turns(self) -> None:
        state._active_chat_tokens["team_1"] = "token"
        state._active_chat_container_ids["team_1"] = "container"
        state._active_action_container_ids["team_1"] = ("token", "action")
        state._blocked_action_workloads.update({("team_1", "action"), ("team_2", "action")})
        state._cancelled_chat_tokens.add("token")
        state._clear_team_id_runtime_state("team_1")
        self.assertNotIn("team_1", state._active_chat_tokens)
        self.assertEqual(state._blocked_action_workloads, {("team_2", "action")})
        self.assertNotIn("token", state._cancelled_chat_tokens)
        state._clear_team_id_runtime_state("missing")

        state._cancelled_chat_tokens.add("cancelled")
        self.assertFalse(state._commit_chat_terminal("team_1", "cancelled"))
        state._active_chat_tokens["team_1"] = "active"
        state._active_chat_container_ids["team_1"] = "container"
        self.assertTrue(state._commit_chat_terminal("team_1", "active"))
        self.assertNotIn("team_1", state._active_chat_container_ids)
        self.assertTrue(state._commit_chat_terminal("team_1", "other"))


if __name__ == "__main__":
    unittest.main()
