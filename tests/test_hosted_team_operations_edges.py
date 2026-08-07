"""Create, destroy, status, and runtime operation edges for Hosted Teams."""

from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

lifecycle = harness.hosted_lifecycle
resources = harness.hosted_resources
state = harness.runtime_state

TEAM_ID = "team_1"
OWNER = "account_1"
RUNTIME_ID = "a" * 64


def _container(**changes):
    values = {
        "id": RUNTIME_ID,
        "name": "team",
        "labels": {"team.id": TEAM_ID, "team.name": "Team", "team.owner": OWNER, "team.runtime": "1"},
        "attrs": {},
        "status": "running",
        "reload": mock.Mock(),
        "logs": mock.Mock(return_value=b"logs"),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _lease(**changes):
    values = {
        "team_id": TEAM_ID,
        "container_id": RUNTIME_ID,
        "owner": OWNER,
        "principal": ("account", OWNER),
        "cleanup_nonce": "",
    }
    values.update(changes)
    return resources._AuthorizationLease(**values)


class HostedTeamOperationEdgeTests(unittest.TestCase):
    def test_create_validates_name_inference_and_pending_cleanup_ownership(self) -> None:
        with (
            mock.patch.object(resources, "_validated_team_name", side_effect=ValueError("name")),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._create(TEAM_ID, {}, OWNER)
        with (
            mock.patch.object(
                lifecycle.inference_config,
                "normalize",
                side_effect=lifecycle.inference_config.InferenceConfigError("model"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._create(TEAM_ID, {}, OWNER)

        for cleanup_owner in ("other", OWNER):
            pending = SimpleNamespace(owner=cleanup_owner)
            with (
                mock.patch.object(resources, "_cleanup_record", return_value=pending),
                self.assertRaises(state.ApiError),
            ):
                lifecycle._create(TEAM_ID, {}, OWNER)

    def test_idempotent_create_preserves_owner_name_and_updates_inference(self) -> None:
        existing = _container()
        inference = SimpleNamespace(provider="openai", model="model")
        with (
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_get_container", return_value=_container(labels={"team.owner": "other"})),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._create(TEAM_ID, {}, OWNER)

        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=inference),
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_get_container", return_value=existing),
            mock.patch.object(resources, "_require_team_runtime"),
            mock.patch.object(resources, "_require_team_isolation"),
            mock.patch.object(resources, "_team_name_from_anchor", return_value="Persisted"),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._create(TEAM_ID, {"team_name": "Changed"}, OWNER)

        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=inference),
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_get_container", return_value=existing),
            mock.patch.object(resources, "_require_team_runtime"),
            mock.patch.object(resources, "_require_team_isolation"),
            mock.patch.object(resources, "_team_name_from_anchor", return_value="Persisted"),
            mock.patch.object(state._inference_store, "save") as save,
        ):
            result = lifecycle._create(TEAM_ID, {}, OWNER)
        self.assertFalse(result["created"])
        self.assertEqual(result["team_name"], "Persisted")
        save.assert_called_once_with(TEAM_ID, inference)

    def test_new_create_requires_clean_storage_and_commits_all_resources(self) -> None:
        inference = SimpleNamespace(provider="openai", model="model")
        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=inference),
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_get_container", return_value=None),
            mock.patch.object(lifecycle, "_teardown_storage", return_value=False),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._create(TEAM_ID, {}, OWNER)

        container = _container()
        network = object()
        state._docker.containers.create = mock.Mock(return_value=container)
        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=inference),
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_get_container", return_value=None),
            mock.patch.object(lifecycle, "_teardown_storage", return_value=True),
            mock.patch.object(resources, "_reserve_capacity", return_value=contextlib.nullcontext()),
            mock.patch.object(resources, "_require_team_runtime"),
            mock.patch.object(lifecycle.postgresql_service_client, "provision_team"),
            mock.patch.object(resources, "_ensure_team_network", return_value=network),
            mock.patch.object(resources, "_wire_network_deps"),
            mock.patch.object(resources, "_require_network_policy"),
            mock.patch.object(lifecycle.container_spec, "build_team_kwargs", return_value={"name": "team"}),
            mock.patch.object(resources, "_start_team_with_isolation"),
            mock.patch.object(state._inference_store, "save"),
        ):
            result = lifecycle._create(TEAM_ID, {"team_name": "Team"}, OWNER)
        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "running")

    def test_create_rollback_distinguishes_incomplete_api_and_generic_failures(self) -> None:
        inference = SimpleNamespace(provider="openai", model="model")

        def run(error: Exception, cleanup_complete: bool) -> state.ApiError:
            patches = (
                mock.patch.object(lifecycle.inference_config, "normalize", return_value=inference),
                mock.patch.object(resources, "_cleanup_record", return_value=None),
                mock.patch.object(resources, "_get_container", return_value=None),
                mock.patch.object(lifecycle, "_teardown_storage", return_value=True),
                mock.patch.object(resources, "_reserve_capacity", return_value=contextlib.nullcontext()),
                mock.patch.object(resources, "_require_team_runtime"),
                mock.patch.object(lifecycle.postgresql_service_client, "provision_team", side_effect=error),
                mock.patch.object(
                    lifecycle,
                    "_teardown",
                    return_value=resources._CleanupResult(cleanup_complete, cleanup_complete),
                ),
            )
            with contextlib.ExitStack() as stack:
                for current in patches:
                    stack.enter_context(current)
                with self.assertRaises(state.ApiError) as caught:
                    lifecycle._create(TEAM_ID, {}, OWNER)
            return caught.exception

        self.assertIn("rollback is incomplete", run(RuntimeError("failed"), False).message)
        api_error = state.ApiError(409, "contract")
        self.assertIs(run(api_error, True), api_error)
        self.assertIn("rolled back", run(RuntimeError("failed"), True).message)

    def test_generation_state_deletion_contains_brain_and_journal_failures(self) -> None:
        self.assertEqual(
            lifecycle._delete_generation_state(TEAM_ID, ""),
            {"brain_checkpoints", "power_checkpoints"},
        )
        with (
            mock.patch.object(
                state._brain_runtime,
                "delete_thread",
                side_effect=lifecycle.brain_runtime_client.BrainRuntimeError("brain"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._delete_generation_state(TEAM_ID, RUNTIME_ID)

        journal = mock.Mock()
        journal.purge.side_effect = lifecycle.power_journal.PowerJournalError("journal")
        with (
            mock.patch.object(state._brain_runtime, "delete_thread"),
            mock.patch.object(state, "_power_execution_journal", return_value=journal),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._delete_generation_state(TEAM_ID, RUNTIME_ID)
        journal.purge.side_effect = None
        with (
            mock.patch.object(state._brain_runtime, "delete_thread"),
            mock.patch.object(state, "_power_execution_journal", return_value=journal),
        ):
            self.assertEqual(
                lifecycle._delete_generation_state(TEAM_ID, RUNTIME_ID),
                {"brain_checkpoints", "power_checkpoints"},
            )

    def test_destroy_revalidates_cleanup_anchor_and_stops_running_runtime(self) -> None:
        container = _container()
        lock = mock.Mock()
        lock.acquire.return_value = False
        with (
            mock.patch.object(resources, "_require_current_authorization", return_value=container),
            mock.patch.object(lifecycle.cleanup_state, "begin"),
            mock.patch.object(resources, "_fail_stop_team") as stop,
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._destroy(TEAM_ID, _lease())
        stop.assert_called_once_with(container, timeout=30)

        stopped_container = _container(status="stopped")
        lock.acquire.return_value = False
        with (
            mock.patch.object(resources, "_require_current_authorization", return_value=stopped_container),
            mock.patch.object(lifecycle.cleanup_state, "begin"),
            mock.patch.object(resources, "_fail_stop_team") as stop,
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._destroy(TEAM_ID, _lease())
        stop.assert_not_called()

        cleanup_lease = _lease(cleanup_nonce="nonce")
        lock.acquire.return_value = False
        with (
            mock.patch.object(resources, "_require_cleanup_authorization") as require,
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._destroy(TEAM_ID, cleanup_lease)
        require.assert_called_once_with(TEAM_ID, cleanup_lease)

        with (
            mock.patch.object(resources, "_require_current_authorization", return_value=container),
            mock.patch.object(
                lifecycle.cleanup_state,
                "begin",
                side_effect=lifecycle.cleanup_state.CleanupStateError("state"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._destroy(TEAM_ID, _lease())

    def test_destroy_requires_complete_teardown_and_exact_residue_proof(self) -> None:
        lock = mock.Mock()
        lock.acquire.return_value = True
        complete = resources._CleanupResult(True, True, tuple(lifecycle._TEAM_RESIDUE_ABSENCE))
        base = (
            mock.patch.object(resources, "_require_cleanup_authorization"),
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            mock.patch.object(lifecycle, "_delete_generation_state", return_value=set()),
            mock.patch.object(state, "_clear_team_id_runtime_state"),
        )
        for cleanup in (
            resources._CleanupResult(False, False),
            resources._CleanupResult(True, True, ("database",)),
            complete,
        ):
            with contextlib.ExitStack() as stack:
                for current in base:
                    stack.enter_context(current)
                stack.enter_context(mock.patch.object(lifecycle, "_teardown", return_value=cleanup))
                if cleanup is complete:
                    result = lifecycle._destroy(TEAM_ID, _lease(cleanup_nonce="nonce"))
                    self.assertTrue(result["destroyed"])
                else:
                    with self.assertRaises(state.ApiError):
                        lifecycle._destroy(TEAM_ID, _lease(cleanup_nonce="nonce"))
        self.assertEqual(lock.release.call_count, 3)

    def test_list_status_inference_logs_and_lifecycle_operations_map_failures(self) -> None:
        own = _container()
        foreign = _container(id="b", labels={"team.owner": "other"})
        state._docker.containers.list = mock.Mock(return_value=[own, foreign])
        with mock.patch.object(resources, "_describe", side_effect=lambda item: {"id": item.id}):
            self.assertEqual(len(lifecycle._list(owner=None)["teams"]), 2)
            self.assertEqual(lifecycle._list(owner=OWNER)["teams"], [{"id": own.id}])

        lease = _lease()
        with (
            mock.patch.object(resources, "_require_current_authorization", return_value=own),
            mock.patch.object(resources, "_describe", return_value={"status": "running"}),
        ):
            self.assertEqual(lifecycle._status(TEAM_ID, lease)["status"], "running")

        with (
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(
                state._inference_store,
                "load",
                side_effect=lifecycle.inference_config.InferenceConfigError("missing"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._inference_status(TEAM_ID, lease)
        config = SimpleNamespace(provider="openai", model="model")
        with (
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(state._inference_store, "load", return_value=config),
        ):
            self.assertEqual(lifecycle._inference_status(TEAM_ID, lease)["model"], "model")

        for body in (None, {}, {"provider": "openai", "model": "m", "extra": True}):
            with self.assertRaises(state.ApiError):
                lifecycle._configure_inference(TEAM_ID, body, lease)
        with (
            mock.patch.object(
                lifecycle.inference_config,
                "normalize",
                side_effect=lifecycle.inference_config.InferenceConfigError("invalid"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._configure_inference(TEAM_ID, {"provider": "openai", "model": "m"}, lease)
        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=config),
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
            mock.patch.object(
                state._inference_store,
                "save",
                side_effect=lifecycle.inference_config.InferenceConfigError("save"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._configure_inference(TEAM_ID, {"provider": "openai", "model": "m"}, lease)
        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=config),
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
            mock.patch.object(state._inference_store, "save"),
        ):
            self.assertEqual(
                lifecycle._configure_inference(TEAM_ID, {"provider": "openai", "model": "m"}, lease)["model"],
                "model",
            )

        with mock.patch.object(resources, "_require_current_authorization", return_value=own):
            self.assertEqual(lifecycle._logs(TEAM_ID, 10, lease)["logs"], "logs")

    def test_runtime_lifecycle_stops_and_starts_only_when_required(self) -> None:
        lease = _lease()
        for op, status, stop_count, start_count in (
            ("stop", "running", 1, 0),
            ("start", "stopped", 0, 1),
            ("restart", "running", 1, 1),
        ):
            container = _container(status=status)
            container.reload = mock.Mock(side_effect=lambda: None)
            if op == "restart":
                statuses = iter(("running", "stopped"))
                container.reload = mock.Mock(
                    side_effect=lambda current=container, values=statuses: setattr(current, "status", next(values))
                )
            with (
                mock.patch.object(resources, "_require_current_authorization", return_value=container),
                mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
                mock.patch.object(resources, "_require_team_runtime"),
                mock.patch.object(resources, "_fail_stop_team") as stop,
                mock.patch.object(resources, "_start_team_with_isolation") as start,
            ):
                lifecycle._lifecycle(TEAM_ID, op, lease)
            self.assertEqual(stop.call_count, stop_count)
            self.assertEqual(start.call_count, start_count)


if __name__ == "__main__":
    unittest.main()
