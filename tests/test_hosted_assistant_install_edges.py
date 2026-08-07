"""Transactional install and uninstall edges for Hosted Assistants."""

from __future__ import annotations

import contextlib
import sys
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

lifecycle = harness.assistant_lifecycle
resources = harness.hosted_resources
state = harness.runtime_state

TEAM_ID = "team_1"
ASSISTANT_ID = "shimpz-cloudflare"
SPEC = harness.HOSTED_SPEC
BINDING = harness.HOSTED_BINDING
OWNER = "account_1"


def _container(**changes):
    labels = {
        "team.assistant.runtime": "1",
        "team.assistant.dynamic": "1",
        "team.id": TEAM_ID,
        "team.assistant": ASSISTANT_ID,
        "team.owner": OWNER,
        "team.assistant.source": BINDING.resolution["source_digest"],
        "team.assistant.image": SPEC.image,
    }
    values = {
        "id": "c" * 64,
        "labels": labels,
        "attrs": {"Config": {"Image": SPEC.image, "Env": []}},
        "status": "running",
        "reload": mock.Mock(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _lease(owner: str = OWNER):
    return SimpleNamespace(owner=owner, container_id="runtime")


class HostedAssistantInstallEdgeTests(unittest.TestCase):
    def test_teardown_handles_absent_and_failed_container_removal(self) -> None:
        with (
            mock.patch.object(resources, "_get_container", return_value=None),
            mock.patch.object(lifecycle, "_remove_egress_policy", return_value=True),
        ):
            self.assertEqual(
                lifecycle._teardown_assistant(TEAM_ID, ASSISTANT_ID),
                resources._CleanupResult(True, True),
            )

        container = _container()
        with (
            mock.patch.object(lifecycle.network_policy, "assistant_identity_valid", return_value=True),
            mock.patch.object(lifecycle, "_remove_egress_policy", return_value=True),
            mock.patch.object(resources, "_remove_team_container", return_value=False),
        ):
            self.assertEqual(
                lifecycle._teardown_assistant(TEAM_ID, ASSISTANT_ID, container=container),
                resources._CleanupResult(False, True),
            )

    def test_install_rejects_cross_team_binding_and_maps_binding_store_failures(self) -> None:
        foreign = SimpleNamespace(**(BINDING.__dict__ | {"team_id": "other"}))
        with self.assertRaises(state.ApiError) as missing:
            lifecycle._install_assistant(TEAM_ID, foreign, OWNER, _lease(), authorize_start=lambda: None)
        self.assertEqual(missing.exception.status, HTTPStatus.NOT_FOUND)

        cases = (
            (
                lifecycle.dynamic_assistants.DynamicAssistantConflictError("conflict"),
                HTTPStatus.CONFLICT,
            ),
            (
                lifecycle.dynamic_assistants.DynamicAssistantError("state"),
                HTTPStatus.SERVICE_UNAVAILABLE,
            ),
        )
        for error, status in cases:
            with (
                mock.patch.object(state._dynamic_assistants, "get", return_value=None),
                mock.patch.object(state._dynamic_assistants, "put", side_effect=error),
                self.assertRaises(state.ApiError) as caught,
            ):
                lifecycle._install_assistant(TEAM_ID, BINDING, OWNER, _lease(), authorize_start=lambda: None)
            self.assertEqual(caught.exception.status, status)

    def test_install_rolls_back_only_new_complete_failures_and_returns_resolution(self) -> None:
        with (
            mock.patch.object(state._dynamic_assistants, "get", return_value=None),
            mock.patch.object(state._dynamic_assistants, "put", return_value=BINDING),
            mock.patch.object(lifecycle.publication, "assistant_spec", return_value=SPEC),
            mock.patch.object(lifecycle, "_install_assistant_locked", side_effect=RuntimeError("failed")),
            mock.patch.object(state._dynamic_assistants, "delete") as delete,
            self.assertRaises(RuntimeError),
        ):
            lifecycle._install_assistant(TEAM_ID, BINDING, OWNER, _lease(), authorize_start=lambda: None)
        delete.assert_called_once_with(TEAM_ID, ASSISTANT_ID)

        incomplete = lifecycle._IncompleteInstallRollback(500, "incomplete")
        with (
            mock.patch.object(state._dynamic_assistants, "get", return_value=None),
            mock.patch.object(state._dynamic_assistants, "put", return_value=BINDING),
            mock.patch.object(lifecycle.publication, "assistant_spec", return_value=SPEC),
            mock.patch.object(lifecycle, "_install_assistant_locked", side_effect=incomplete),
            mock.patch.object(state._dynamic_assistants, "delete") as delete,
            self.assertRaises(lifecycle._IncompleteInstallRollback),
        ):
            lifecycle._install_assistant(TEAM_ID, BINDING, OWNER, _lease(), authorize_start=lambda: None)
        delete.assert_not_called()

        installed = {"team_id": TEAM_ID, "assistant": ASSISTANT_ID, "installed": True}
        with (
            mock.patch.object(state._dynamic_assistants, "get", return_value=None),
            mock.patch.object(state._dynamic_assistants, "put", return_value=BINDING),
            mock.patch.object(lifecycle.publication, "assistant_spec", return_value=SPEC),
            mock.patch.object(lifecycle, "_install_assistant_locked", return_value=installed),
        ):
            result = lifecycle._install_assistant(TEAM_ID, BINDING, OWNER, _lease(), authorize_start=lambda: None)
        self.assertEqual(result["source_digest"], BINDING.resolution["source_digest"])
        self.assertEqual(result["binding_digest"], BINDING.binding_digest)

    def test_locked_install_requires_owner_and_routes_existing_or_new_container(self) -> None:
        with (
            mock.patch.object(resources, "_require_current_authorization"),
            self.assertRaises(state.ApiError) as hidden,
        ):
            lifecycle._install_assistant_locked(
                TEAM_ID,
                BINDING,
                SPEC,
                OWNER,
                _lease("other"),
                authorize_start=lambda: None,
            )
        self.assertEqual(hidden.exception.status, HTTPStatus.NOT_FOUND)

        existing = _container()
        for current, routed in ((existing, "existing"), (None, "new")):
            with (
                mock.patch.object(resources, "_require_current_authorization"),
                mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
                mock.patch.object(resources, "_prepare_assistant_image"),
                mock.patch.object(resources, "_get_container", return_value=current),
                mock.patch.object(lifecycle, "_admit_existing_assistant", return_value={"route": "existing"}),
                mock.patch.object(lifecycle, "_provision_assistant", return_value={"route": "new"}),
            ):
                result = lifecycle._install_assistant_locked(
                    TEAM_ID,
                    BINDING,
                    SPEC,
                    OWNER,
                    _lease(),
                    authorize_start=lambda: None,
                )
            self.assertEqual(result["route"], routed)

    def test_existing_admission_rejects_inspection_labels_image_and_readiness(self) -> None:
        docker_error = lifecycle.docker.errors.DockerException("inspect")
        with (
            mock.patch.object(lifecycle, "_egress_store", return_value=object()),
            self.assertRaises(state.ApiError) as unavailable,
        ):
            lifecycle._admit_existing_assistant(
                TEAM_ID,
                BINDING,
                SPEC,
                OWNER,
                _container(reload=mock.Mock(side_effect=docker_error)),
            )
        self.assertEqual(unavailable.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

        invalid_labels = _container(labels={})
        with mock.patch.object(lifecycle, "_egress_store", return_value=object()), self.assertRaises(state.ApiError):
            lifecycle._admit_existing_assistant(TEAM_ID, BINDING, SPEC, OWNER, invalid_labels)

        wrong_image = _container(attrs={"Config": {"Image": "other"}})
        with (
            mock.patch.object(lifecycle, "_egress_store", return_value=object()),
            mock.patch.object(resources, "_require_team_isolation"),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._admit_existing_assistant(TEAM_ID, BINDING, SPEC, OWNER, wrong_image)

        with (
            mock.patch.object(lifecycle, "_egress_store", return_value=object()),
            mock.patch.object(resources, "_require_team_isolation"),
            mock.patch.object(lifecycle, "_admit_assistant_contract", return_value=()),
            mock.patch.object(lifecycle, "_validate_admitted_egress", return_value=None),
            mock.patch.object(lifecycle, "_validate_assistant_proxy_environment"),
            mock.patch.object(lifecycle, "_assistant_ready_now", return_value=(False, "stopped")),
            self.assertRaises(state.ApiError) as stopped,
        ):
            lifecycle._admit_existing_assistant(TEAM_ID, BINDING, SPEC, OWNER, _container())
        self.assertEqual(stopped.exception.status, HTTPStatus.CONFLICT)

    def test_existing_admission_commits_only_after_all_contracts_pass(self) -> None:
        container = _container()
        store = object()
        with (
            mock.patch.object(lifecycle, "_egress_store", return_value=store),
            mock.patch.object(resources, "_require_team_isolation"),
            mock.patch.object(lifecycle, "_admit_assistant_contract", return_value=("api.example",)),
            mock.patch.object(lifecycle, "_validate_admitted_egress", return_value="token"),
            mock.patch.object(lifecycle, "_validate_assistant_proxy_environment") as proxy,
            mock.patch.object(lifecycle, "_assistant_ready_now", return_value=(True, "running")),
            mock.patch.object(lifecycle, "_retain_admitted_assistant_integrations") as retain,
        ):
            result = lifecycle._admit_existing_assistant(TEAM_ID, BINDING, SPEC, OWNER, container)
        self.assertFalse(result["installed"])
        proxy.assert_called_once_with(container, "token", ("api.example",), store)
        retain.assert_called_once_with(TEAM_ID, ASSISTANT_ID, SPEC)

    def test_provision_enforces_team_limit_and_capacity_reservation(self) -> None:
        with (
            mock.patch.object(
                lifecycle,
                "_team_assistant_containers",
                return_value=[object()] * state.MAX_ASSISTANTS_PER_TEAM,
            ),
            self.assertRaises(state.ApiError) as limited,
        ):
            lifecycle._provision_assistant(
                TEAM_ID,
                BINDING,
                SPEC,
                OWNER,
                authorize_start=lambda: None,
            )
        self.assertEqual(limited.exception.status, HTTPStatus.TOO_MANY_REQUESTS)

        with (
            mock.patch.object(lifecycle, "_team_assistant_containers", return_value=[]),
            mock.patch.object(resources, "_reserve_capacity", return_value=contextlib.nullcontext()) as reserve,
            mock.patch.object(lifecycle, "_provision_assistant_transaction", return_value="running"),
        ):
            result = lifecycle._provision_assistant(
                TEAM_ID,
                BINDING,
                SPEC,
                OWNER,
                authorize_start=lambda: None,
            )
        self.assertTrue(result["installed"])
        reserve.assert_called_once()

    def test_provision_transaction_creates_connects_starts_and_commits(self) -> None:
        container = _container()
        network = mock.Mock()
        state._docker.containers.create = mock.Mock(return_value=container)
        authorize = mock.Mock()
        with (
            mock.patch.object(lifecycle, "_egress_store", return_value=object()),
            mock.patch.object(resources, "_ensure_team_network", return_value=network),
            mock.patch.object(lifecycle, "_reserve_egress_environment", return_value=(None, {})),
            mock.patch.object(lifecycle.container_spec, "build_assistant_kwargs", return_value={"name": "assistant"}),
            mock.patch.object(resources, "_require_team_runtime"),
            mock.patch.object(lifecycle, "_admit_assistant_contract", return_value=()),
            mock.patch.object(lifecycle, "_validate_assistant_proxy_environment"),
            mock.patch.object(lifecycle, "_activate_admitted_egress"),
            mock.patch.object(resources, "_start_team_with_isolation"),
            mock.patch.object(lifecycle, "_wait_assistant_ready", return_value=(True, "running")),
            mock.patch.object(resources, "_require_team_isolation"),
            mock.patch.object(lifecycle, "_assistant_ready_now", return_value=(True, "running")),
            mock.patch.object(lifecycle, "_retain_admitted_assistant_integrations"),
        ):
            status = lifecycle._provision_assistant_transaction(
                TEAM_ID,
                BINDING,
                SPEC,
                OWNER,
                authorize_start=authorize,
            )
        self.assertEqual(status, "running")
        authorize.assert_called_once_with()
        network.disconnect.assert_called_once_with(container)
        network.connect.assert_called_once_with(container, aliases=[ASSISTANT_ID, f"{ASSISTANT_ID}.team"])

    def test_provision_transaction_maps_readiness_and_rollback_outcomes(self) -> None:
        with (
            mock.patch.object(lifecycle, "_egress_store", return_value=object()),
            mock.patch.object(resources, "_ensure_team_network", side_effect=RuntimeError("failed")),
            mock.patch.object(lifecycle, "_teardown_assistant", return_value=resources._CleanupResult(False, True)),
            self.assertRaises(lifecycle._IncompleteInstallRollback),
        ):
            lifecycle._provision_assistant_transaction(
                TEAM_ID,
                BINDING,
                SPEC,
                OWNER,
                authorize_start=lambda: None,
            )

        with (
            mock.patch.object(lifecycle, "_egress_store", return_value=object()),
            mock.patch.object(resources, "_ensure_team_network", side_effect=RuntimeError("failed")),
            mock.patch.object(lifecycle, "_teardown_assistant", return_value=resources._CleanupResult(True, True)),
            self.assertRaises(state.ApiError) as wrapped,
        ):
            lifecycle._provision_assistant_transaction(
                TEAM_ID,
                BINDING,
                SPEC,
                OWNER,
                authorize_start=lambda: None,
            )
        self.assertIn("rolled back", wrapped.exception.message)

        api_error = state.ApiError(409, "contract")
        with (
            mock.patch.object(lifecycle, "_egress_store", return_value=object()),
            mock.patch.object(resources, "_ensure_team_network", side_effect=api_error),
            mock.patch.object(lifecycle, "_teardown_assistant", return_value=resources._CleanupResult(True, True)),
            self.assertRaises(state.ApiError) as preserved,
        ):
            lifecycle._provision_assistant_transaction(
                TEAM_ID,
                BINDING,
                SPEC,
                OWNER,
                authorize_start=lambda: None,
            )
        self.assertIs(preserved.exception, api_error)

        def run_readiness_failure(wait_result, committed_result) -> state.ApiError:
            container = _container()
            network = mock.Mock()
            patches = (
                mock.patch.object(lifecycle, "_egress_store", return_value=object()),
                mock.patch.object(resources, "_ensure_team_network", return_value=network),
                mock.patch.object(lifecycle, "_reserve_egress_environment", return_value=(None, {})),
                mock.patch.object(lifecycle.container_spec, "build_assistant_kwargs", return_value={}),
                mock.patch.object(state._docker.containers, "create", return_value=container),
                mock.patch.object(resources, "_require_team_runtime"),
                mock.patch.object(lifecycle, "_admit_assistant_contract", return_value=()),
                mock.patch.object(lifecycle, "_validate_assistant_proxy_environment"),
                mock.patch.object(lifecycle, "_activate_admitted_egress"),
                mock.patch.object(resources, "_start_team_with_isolation"),
                mock.patch.object(lifecycle, "_wait_assistant_ready", return_value=wait_result),
                mock.patch.object(resources, "_require_team_isolation"),
                mock.patch.object(lifecycle, "_assistant_ready_now", return_value=committed_result),
                mock.patch.object(
                    lifecycle,
                    "_teardown_assistant",
                    return_value=resources._CleanupResult(True, True),
                ),
            )
            with contextlib.ExitStack() as stack:
                for current in patches:
                    stack.enter_context(current)
                with self.assertRaises(state.ApiError) as failed:
                    lifecycle._provision_assistant_transaction(
                        TEAM_ID,
                        BINDING,
                        SPEC,
                        OWNER,
                        authorize_start=lambda: None,
                    )
            return failed.exception

        self.assertIn("failed readiness", run_readiness_failure((False, "stopped"), (True, "running")).message)
        self.assertIn("lost readiness", run_readiness_failure((True, "running"), (False, "stopped")).message)

    def test_uninstall_requires_complete_cleanup_and_maps_private_store_failures(self) -> None:
        lease = _lease()
        with (
            mock.patch.object(state._dynamic_assistants, "get", return_value=None),
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
            mock.patch.object(lifecycle, "_teardown_assistant", return_value=resources._CleanupResult(False, True)),
            self.assertRaises(state.ApiError) as incomplete,
        ):
            lifecycle._uninstall_assistant(TEAM_ID, ASSISTANT_ID, lease)
        self.assertEqual(incomplete.exception.status, HTTPStatus.INTERNAL_SERVER_ERROR)

        store_error = lifecycle.integration_store.OAuthIntegrationStoreError("state")
        with (
            mock.patch.object(state._dynamic_assistants, "get", return_value=None),
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
            mock.patch.object(lifecycle, "_teardown_assistant", return_value=resources._CleanupResult(True, True)),
            mock.patch.object(state._assistant_integrations, "delete_assistant", side_effect=store_error),
            self.assertRaises(state.ApiError) as unavailable,
        ):
            lifecycle._uninstall_assistant(TEAM_ID, ASSISTANT_ID, lease)
        self.assertEqual(unavailable.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

        metadata_error = lifecycle.dynamic_assistants.DynamicAssistantError("state")
        with (
            mock.patch.object(state._dynamic_assistants, "get", return_value=None),
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
            mock.patch.object(lifecycle, "_teardown_assistant", return_value=resources._CleanupResult(True, True)),
            mock.patch.object(state._assistant_integrations, "delete_assistant"),
            mock.patch.object(state._dynamic_assistants, "delete", side_effect=metadata_error),
            self.assertRaises(state.ApiError) as metadata,
        ):
            lifecycle._uninstall_assistant(TEAM_ID, ASSISTANT_ID, lease)
        self.assertEqual(metadata.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

    def test_uninstall_deletes_binding_integrations_and_unreferenced_icon(self) -> None:
        lease = _lease()
        with (
            mock.patch.object(state._dynamic_assistants, "get", return_value=None),
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
            mock.patch.object(lifecycle, "_teardown_assistant", return_value=resources._CleanupResult(True, True)),
            mock.patch.object(state._assistant_integrations, "delete_assistant"),
            mock.patch.object(state._dynamic_assistants, "delete"),
            mock.patch.object(lifecycle.publication, "discard_icon") as icon,
        ):
            result = lifecycle._uninstall_assistant(TEAM_ID, ASSISTANT_ID, lease)
        self.assertTrue(result["uninstalled"])
        icon.assert_not_called()

        with (
            mock.patch.object(state._dynamic_assistants, "get", return_value=BINDING),
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
            mock.patch.object(lifecycle, "_teardown_assistant", return_value=resources._CleanupResult(True, True)),
            mock.patch.object(state._assistant_integrations, "delete_assistant") as integrations,
            mock.patch.object(state._dynamic_assistants, "delete") as binding,
            mock.patch.object(lifecycle.publication, "discard_icon") as icon,
        ):
            result = lifecycle._uninstall_assistant(TEAM_ID, ASSISTANT_ID, lease)
        self.assertTrue(result["uninstalled"])
        integrations.assert_called_once_with(TEAM_ID, ASSISTANT_ID)
        binding.assert_called_once_with(TEAM_ID, ASSISTANT_ID)
        icon.assert_called_once()


if __name__ == "__main__":
    unittest.main()
