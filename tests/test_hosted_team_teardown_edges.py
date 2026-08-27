"""Storage and teardown edge coverage for Hosted Team lifecycle."""

from __future__ import annotations

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


def _record(*, dropped: bool = False):
    return lifecycle.cleanup_state.Record(1, TEAM_ID, OWNER, RUNTIME_ID, "b" * 32, dropped)


class HostedTeamTeardownEdgeTests(unittest.TestCase):
    def test_file_operations_validate_body_and_map_storage_error_families(self) -> None:
        lease = object()
        for data in (b"", "text", b"x" * (lifecycle.hosted_assistants.MAX_INBOX_FILE_BYTES + 1)):
            with self.subTest(data_type=type(data)), self.assertRaises(state.ApiError):
                lifecycle._put_inbox_file(TEAM_ID, "file", data, "text/plain", lease)

        storage = mock.Mock()
        storage.put.return_value = {"id": "file"}
        storage.list.return_value = {"files": []}
        storage.delete.return_value = {"deleted": True}
        with (
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(state, "_storage", return_value=storage),
        ):
            self.assertEqual(
                lifecycle._put_inbox_file(TEAM_ID, "file", b"x", "text/plain", lease)["file"]["id"], "file"
            )
            self.assertEqual(lifecycle._list_team_files(TEAM_ID, lease)["files"], [])
            self.assertTrue(lifecycle._delete_team_file(TEAM_ID, "file", lease)["deleted"])

        operations = (
            (
                "put",
                lifecycle.team_storage.StorageQuotaError("quota"),
                lambda: lifecycle._put_inbox_file(TEAM_ID, "f", b"x", "t", lease),
            ),
            (
                "put",
                lifecycle.team_storage.StorageInputError("input"),
                lambda: lifecycle._put_inbox_file(TEAM_ID, "f", b"x", "t", lease),
            ),
            (
                "put",
                lifecycle.team_storage.StorageError("storage"),
                lambda: lifecycle._put_inbox_file(TEAM_ID, "f", b"x", "t", lease),
            ),
            (
                "list",
                lifecycle.team_storage.StorageError("storage"),
                lambda: lifecycle._list_team_files(TEAM_ID, lease),
            ),
            (
                "delete",
                lifecycle.team_storage.StorageNotFoundError("missing"),
                lambda: lifecycle._delete_team_file(TEAM_ID, "f", lease),
            ),
            (
                "delete",
                lifecycle.team_storage.StorageError("storage"),
                lambda: lifecycle._delete_team_file(TEAM_ID, "f", lease),
            ),
        )
        for method, error, invoke in operations:
            failed = mock.Mock()
            setattr(failed, method, mock.Mock(side_effect=error))
            with (
                mock.patch.object(resources, "_require_current_authorization"),
                mock.patch.object(state, "_storage", return_value=failed),
                self.assertRaises(state.ApiError),
            ):
                invoke()

    def test_volume_and_runtime_ownership_are_exact_and_retry_safe(self) -> None:
        volume = mock.Mock(attrs={})
        state._docker.volumes.get = mock.Mock(return_value=volume)
        with mock.patch.object(lifecycle.network_policy, "volume_identity_valid", return_value=False):
            self.assertFalse(lifecycle._remove_volume(TEAM_ID, "config"))
        with mock.patch.object(lifecycle.network_policy, "volume_identity_valid", return_value=True):
            self.assertTrue(lifecycle._remove_volume(TEAM_ID, "config"))
        state._docker.volumes.get = mock.Mock(side_effect=lifecycle.docker.errors.NotFound())
        self.assertTrue(lifecycle._remove_volume(TEAM_ID, "config"))
        state._docker.volumes.get = mock.Mock(side_effect=lifecycle.docker.errors.DockerException("docker"))
        self.assertFalse(lifecycle._remove_volume(TEAM_ID, "config"))

        runtime = SimpleNamespace(id=RUNTIME_ID, labels={"team.owner": OWNER}, attrs={}, reload=mock.Mock())
        with mock.patch.object(
            resources, "_get_container", side_effect=lifecycle.docker.errors.DockerException("docker")
        ):
            self.assertEqual(lifecycle._owned_teardown_runtime(TEAM_ID, OWNER, RUNTIME_ID), (False, None))
        with mock.patch.object(resources, "_get_container", return_value=None):
            self.assertEqual(lifecycle._owned_teardown_runtime(TEAM_ID, OWNER, RUNTIME_ID), (True, None))
        with (
            mock.patch.object(resources, "_get_container", return_value=runtime),
            mock.patch.object(runtime, "reload", side_effect=lifecycle.docker.errors.DockerException("inspect")),
        ):
            self.assertEqual(lifecycle._owned_teardown_runtime(TEAM_ID, OWNER, RUNTIME_ID), (False, None))
        with (
            mock.patch.object(resources, "_get_container", return_value=runtime),
            mock.patch.object(lifecycle.network_policy, "runtime_identity_valid", return_value=True),
        ):
            self.assertEqual(lifecycle._owned_teardown_runtime(TEAM_ID, OWNER, RUNTIME_ID), (True, runtime))

    def test_runtime_stop_remove_volume_and_small_teardown_adapters(self) -> None:
        self.assertTrue(lifecycle._stop_teardown_runtime(None))
        with mock.patch.object(resources, "_fail_stop_team"):
            self.assertTrue(lifecycle._stop_teardown_runtime(object()))
        with mock.patch.object(resources, "_fail_stop_team", side_effect=state.ApiError(500, "stuck")):
            self.assertFalse(lifecycle._stop_teardown_runtime(object()))
        self.assertTrue(lifecycle._remove_teardown_runtime(None))
        with mock.patch.object(resources, "_remove_team_container", return_value=False):
            self.assertFalse(lifecycle._remove_teardown_runtime(object()))
        with mock.patch.object(lifecycle, "_remove_volume", side_effect=(True, False)):
            self.assertFalse(lifecycle._teardown_volumes(TEAM_ID))
        with mock.patch.object(resources, "_teardown_team_networks", return_value=True):
            self.assertTrue(lifecycle._teardown_network_planes(TEAM_ID))

    def test_assistant_teardown_contains_invalid_identity_and_store_failures(self) -> None:
        invalid = SimpleNamespace(labels={"team.assistant": "INVALID"})
        valid = SimpleNamespace(labels={"team.assistant": "assistant"})
        with mock.patch.object(
            lifecycle.assistant_lifecycle,
            "_team_assistant_containers",
            side_effect=lifecycle.docker.errors.DockerException("docker"),
        ):
            self.assertFalse(lifecycle._teardown_assistants(TEAM_ID))

        with (
            mock.patch.object(
                lifecycle.assistant_lifecycle, "_team_assistant_containers", return_value=[invalid, valid]
            ),
            mock.patch.object(
                lifecycle.assistant_lifecycle, "_teardown_assistant", return_value=resources._CleanupResult(True, True)
            ),
            mock.patch.object(
                state._dynamic_assistants,
                "delete",
                side_effect=lifecycle.dynamic_assistants.DynamicAssistantError("state"),
            ),
            mock.patch.object(state._dynamic_assistants, "list", return_value=()),
        ):
            self.assertFalse(lifecycle._teardown_assistants(TEAM_ID))

        with (
            mock.patch.object(lifecycle.assistant_lifecycle, "_team_assistant_containers", return_value=[]),
            mock.patch.object(
                state._dynamic_assistants,
                "list",
                side_effect=lifecycle.dynamic_assistants.DynamicAssistantError("state"),
            ),
        ):
            self.assertFalse(lifecycle._teardown_assistants(TEAM_ID))

        with (
            mock.patch.object(lifecycle.assistant_lifecycle, "_team_assistant_containers", return_value=[valid]),
            mock.patch.object(
                lifecycle.assistant_lifecycle,
                "_teardown_assistant",
                return_value=resources._CleanupResult(False, True),
            ),
            mock.patch.object(state._dynamic_assistants, "list", return_value=()),
        ):
            self.assertFalse(lifecycle._teardown_assistants(TEAM_ID))

        binding = SimpleNamespace(assistant_id="assistant")
        for removed in (False, True):
            delete_error = lifecycle.dynamic_assistants.DynamicAssistantError("state") if removed else None
            with (
                mock.patch.object(lifecycle.assistant_lifecycle, "_team_assistant_containers", return_value=[]),
                mock.patch.object(state._dynamic_assistants, "list", return_value=(binding,)),
                mock.patch.object(
                    lifecycle.assistant_lifecycle,
                    "_teardown_assistant",
                    return_value=resources._CleanupResult(removed, True),
                ),
                mock.patch.object(state._dynamic_assistants, "delete", side_effect=delete_error),
            ):
                self.assertFalse(lifecycle._teardown_assistants(TEAM_ID))

    def test_storage_inference_and_integration_teardown_fail_closed(self) -> None:
        with (
            mock.patch.object(state, "_storage_instance", None),
            mock.patch.object(Path, "exists", return_value=False),
        ):
            self.assertTrue(lifecycle._teardown_storage(TEAM_ID))
        storage = mock.Mock()
        with (
            mock.patch.object(state, "_storage_instance", object()),
            mock.patch.object(state, "_storage", return_value=storage),
        ):
            self.assertTrue(lifecycle._teardown_storage(TEAM_ID))
        storage.destroy.side_effect = lifecycle.team_storage.StorageError("storage")
        with (
            mock.patch.object(state, "_storage_instance", object()),
            mock.patch.object(state, "_storage", return_value=storage),
        ):
            self.assertFalse(lifecycle._teardown_storage(TEAM_ID))

        state._inference_store.delete = mock.Mock()
        self.assertTrue(lifecycle._teardown_inference(TEAM_ID))
        state._inference_store.delete.side_effect = lifecycle.inference_config.InferenceConfigError("state")
        self.assertFalse(lifecycle._teardown_inference(TEAM_ID))

        state._assistant_integrations.delete_team = mock.Mock()
        self.assertTrue(lifecycle._teardown_assistant_integrations(TEAM_ID))
        state._assistant_integrations.delete_team.side_effect = lifecycle.integration_store.OAuthIntegrationStoreError(
            "state"
        )
        self.assertFalse(lifecycle._teardown_assistant_integrations(TEAM_ID))

    def test_database_drop_and_finalize_are_idempotent_and_contain_failures(self) -> None:
        dropped = _record(dropped=True)
        self.assertIs(lifecycle._drop_teardown_database(TEAM_ID, dropped), dropped)
        current = _record()
        with (
            mock.patch.object(lifecycle.postgresql_service_client, "drop_team"),
            mock.patch.object(lifecycle.cleanup_state, "mark_db_dropped", return_value=dropped),
        ):
            self.assertIs(lifecycle._drop_teardown_database(TEAM_ID, current), dropped)
        with mock.patch.object(
            lifecycle.postgresql_service_client,
            "drop_team",
            side_effect=lifecycle.postgresql_service_client.PostgreSQLServiceError("database"),
        ):
            self.assertIsNone(lifecycle._drop_teardown_database(TEAM_ID, current))

        with (
            mock.patch.object(lifecycle.postgresql_service_client, "finalize_team_drop"),
            mock.patch.object(lifecycle.cleanup_state, "finish"),
        ):
            self.assertTrue(lifecycle._finalize_teardown(TEAM_ID, dropped))
        with mock.patch.object(
            lifecycle.postgresql_service_client,
            "finalize_team_drop",
            side_effect=lifecycle.postgresql_service_client.PostgreSQLServiceError("database"),
        ):
            self.assertFalse(lifecycle._finalize_teardown(TEAM_ID, dropped))

    def test_artifact_and_full_teardown_report_exact_residue_progress(self) -> None:
        with mock.patch.object(lifecycle, "_stop_teardown_runtime", return_value=False):
            self.assertEqual(lifecycle._teardown_artifacts(TEAM_ID, None), (False, set()))
        with mock.patch.multiple(
            lifecycle,
            _stop_teardown_runtime=lambda _runtime: True,
            _teardown_assistants=lambda _team: True,
            _teardown_storage=lambda _team: True,
            _teardown_inference=lambda _team: True,
            _teardown_assistant_integrations=lambda _team: True,
            _teardown_assistant_stored_inputs=lambda _team: True,
            _teardown_network_planes=lambda _team: True,
            _remove_teardown_runtime=lambda _runtime: True,
            _teardown_volumes=lambda _team: True,
        ):
            complete, absent = lifecycle._teardown_artifacts(TEAM_ID, None)
        self.assertTrue(complete)
        self.assertIn("team_volumes", absent)

        with mock.patch.object(lifecycle, "_owned_teardown_runtime", return_value=(False, None)):
            self.assertFalse(lifecycle._teardown(TEAM_ID, owner=OWNER, runtime_id=RUNTIME_ID).complete)
        with (
            mock.patch.object(lifecycle, "_owned_teardown_runtime", return_value=(True, None)),
            mock.patch.object(
                lifecycle.cleanup_state, "begin", side_effect=lifecycle.cleanup_state.CleanupStateError("state")
            ),
        ):
            self.assertFalse(lifecycle._teardown(TEAM_ID, owner=OWNER, runtime_id=RUNTIME_ID).complete)

        record = _record()
        with (
            mock.patch.object(lifecycle, "_owned_teardown_runtime", return_value=(True, None)),
            mock.patch.object(lifecycle.cleanup_state, "begin", return_value=record),
            mock.patch.object(lifecycle, "_teardown_artifacts", return_value=(False, {"team_storage"})),
        ):
            result = lifecycle._teardown(TEAM_ID, owner=OWNER, runtime_id=RUNTIME_ID)
        self.assertEqual(result.residue_absent, ("team_storage",))

        with (
            mock.patch.object(lifecycle, "_owned_teardown_runtime", return_value=(True, None)),
            mock.patch.object(lifecycle.cleanup_state, "begin", return_value=record),
            mock.patch.object(lifecycle, "_teardown_artifacts", return_value=(True, {"team_storage"})),
            mock.patch.object(lifecycle, "_drop_teardown_database", return_value=None),
        ):
            self.assertFalse(lifecycle._teardown(TEAM_ID, owner=OWNER, runtime_id=RUNTIME_ID).complete)
        with (
            mock.patch.object(lifecycle, "_owned_teardown_runtime", return_value=(True, None)),
            mock.patch.object(lifecycle.cleanup_state, "begin", return_value=record),
            mock.patch.object(lifecycle, "_teardown_artifacts", return_value=(True, set())),
            mock.patch.object(lifecycle, "_drop_teardown_database", return_value=_record(dropped=True)),
            mock.patch.object(lifecycle, "_finalize_teardown", return_value=False),
        ):
            result = lifecycle._teardown(TEAM_ID, owner=OWNER, runtime_id=RUNTIME_ID)
        self.assertTrue(result.db_dropped)


if __name__ == "__main__":
    unittest.main()
