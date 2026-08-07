"""Network lifecycle and authorization edges for Hosted Team resources."""

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

resources = harness.hosted_resources
state = harness.runtime_state

TEAM_ID = "team_1"
OWNER = "account_1"
PRINCIPAL = ("account", OWNER)


def _container(**changes):
    values = {
        "id": "a" * 64,
        "name": "team",
        "labels": {"team.id": TEAM_ID, "team.name": "Team", "team.owner": OWNER, "team.runtime": "1"},
        "attrs": {"State": {"Running": False}, "Config": {"Labels": {"team.id": TEAM_ID}}},
        "status": "running",
        "reload": mock.Mock(),
        "stop": mock.Mock(),
        "kill": mock.Mock(),
        "remove": mock.Mock(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _network(**changes):
    values = {
        "id": "network-id",
        "attrs": {"Containers": {}},
        "reload": mock.Mock(),
        "connect": mock.Mock(),
        "disconnect": mock.Mock(),
        "remove": mock.Mock(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _api_error(*, status: int = 500, explanation: str = "failed"):
    error = resources.docker.errors.APIError(explanation)
    error.response = SimpleNamespace(status_code=status)
    error.explanation = explanation
    return error


class HostedNetworkAuthorizationEdgeTests(unittest.TestCase):
    def test_isolation_wrapper_stopped_state_and_survivor_removal_paths(self) -> None:
        container = _container()
        with mock.patch.object(resources, "_require_team_isolation_mode") as mode:
            resources._require_team_isolation(container)
        mode.assert_called_once_with(container, require_running=False, workload_spec=None)

        for error, expected in (
            (resources.docker.errors.NotFound(), True),
            (resources.docker.errors.DockerException("docker"), False),
        ):
            candidate = _container(reload=mock.Mock(side_effect=error))
            self.assertEqual(resources._team_not_running(candidate), expected)
        self.assertTrue(resources._team_not_running(container))
        self.assertFalse(resources._team_not_running(_container(attrs={"State": {"Running": True}})))

        with (
            mock.patch.object(container, "stop", side_effect=resources.docker.errors.DockerException("stop")),
            mock.patch.object(container, "kill"),
            mock.patch.object(resources, "_team_not_running", side_effect=(False, True)),
        ):
            resources._fail_stop_team(container)

        survivor = _container(id="survivor")
        with (
            mock.patch.object(resources, "_fail_stop_team"),
            mock.patch.object(resources, "_remaining_container", side_effect=(survivor, None)),
        ):
            self.assertTrue(resources._remove_team_container(container))
        survivor.remove.assert_called_once_with(force=True)

        with (
            mock.patch.object(resources, "_fail_stop_team", side_effect=(None, state.ApiError(500, "stuck"))),
            mock.patch.object(resources, "_remaining_container", return_value=survivor),
        ):
            self.assertFalse(resources._remove_team_container(container))

    def test_memory_reload_and_physical_inventory_failures_are_contained(self) -> None:
        broken = _container(reload=mock.Mock(side_effect=resources.docker.errors.DockerException("inspect")))
        with (
            mock.patch.object(resources, "_admitted_resource_containers", return_value=[broken]),
            self.assertRaises(state.ApiError),
        ):
            resources._memory_usage()

        first = _container(id="one")
        excluded = _container(id="two", labels={"team.id": "two", "team.runtime": "1"})
        state._docker.containers.list = mock.Mock(return_value=[first, excluded])
        self.assertEqual(resources._physical_teams(exclude_keys=frozenset({"team:two"})), [first])

        with (
            mock.patch.object(resources, "_CAPACITY_SNAPSHOT_MAX_ATTEMPTS", 0),
            resources._reserve_capacity("zero", OWNER, 1, team_slot=False),
        ):
            pass

    def test_network_metadata_caches_members_and_rejects_unsafe_inventory(self) -> None:
        member = _container(id="member", name="member", attrs={"Config": {}})
        network = _network(attrs={"Containers": {"member": {}}})
        state._docker.containers.get = mock.Mock(return_value=member)
        memo = {}
        metadata = resources._network_container_metadata(network, memo)
        self.assertEqual(metadata["member"]["Id"], "member")
        self.assertEqual(metadata["member"]["Name"], "/member")
        self.assertIs(resources._network_container_metadata(network, memo), metadata)
        self.assertEqual(resources._network_container_metadata(network), metadata)

        for candidate in (
            _network(attrs={"Containers": []}),
            _network(reload=mock.Mock(side_effect=resources.docker.errors.DockerException("inspect"))),
        ):
            with self.assertRaises(state.ApiError):
                resources._network_container_metadata(candidate)

        with self.assertRaises(state.ApiError):
            resources._network_container_metadata(_network(id=None), {})

        with (
            mock.patch.object(resources, "_network_container_metadata", return_value={}),
            mock.patch.object(resources.network_policy, "network_members_valid", return_value=False),
            self.assertRaises(state.ApiError),
        ):
            resources._require_network_policy(
                network,
                TEAM_ID,
                resources.network_policy.CORE_KIND,
                require_runtime=True,
                require_dependencies=True,
            )

    def test_network_ensure_creates_exact_internal_plane_and_maps_docker_failures(self) -> None:
        not_found = resources.docker.errors.NotFound()
        created = _network()
        with (
            mock.patch.object(state._docker.networks, "get", side_effect=not_found),
            mock.patch.object(state._docker.networks, "create", return_value=created, create=True) as create,
            mock.patch.object(resources, "_require_network_policy"),
        ):
            self.assertIs(resources._ensure_team_network(TEAM_ID), created)
        self.assertTrue(create.call_args.kwargs["internal"])
        self.assertFalse(create.call_args.kwargs["attachable"])

        for target in ("get", "create"):
            get_effect = not_found if target == "create" else resources.docker.errors.DockerException("inspect")
            create_effect = resources.docker.errors.DockerException("create") if target == "create" else None
            with (
                mock.patch.object(state._docker.networks, "get", side_effect=get_effect),
                mock.patch.object(state._docker.networks, "create", side_effect=create_effect, create=True),
                self.assertRaises(state.ApiError),
            ):
                resources._ensure_team_network_kind(TEAM_ID, resources.network_policy.CORE_KIND)

    def test_safe_connect_requires_shared_identity_and_handles_only_exact_idempotency(self) -> None:
        already = _api_error(status=HTTPStatus.FORBIDDEN, explanation="already exists in network")
        self.assertTrue(resources._already_connected(already))
        self.assertFalse(resources._already_connected(_api_error()))

        not_found = resources.docker.errors.NotFound()
        state._docker.containers.get = mock.Mock(side_effect=not_found)
        resources._safe_connect(_network(), "optional", required=False)
        with self.assertRaises(state.ApiError):
            resources._safe_connect(_network(), "required", required=True)

        container = _container()
        state._docker.containers.get = mock.Mock(return_value=container)
        with (
            mock.patch.object(resources.network_policy, "shared_service_role_for_name", return_value="role"),
            mock.patch.object(container, "reload", side_effect=resources.docker.errors.DockerException("inspect")),
            self.assertRaises(state.ApiError),
        ):
            resources._safe_connect(_network(), "service", required=True)
        with (
            mock.patch.object(resources.network_policy, "shared_service_role_for_name", return_value="role"),
            mock.patch.object(resources.network_policy, "shared_service_identity_valid", return_value=False),
            self.assertRaises(state.ApiError),
        ):
            resources._safe_connect(_network(), "service", required=True)

        for required in (False, True):
            network = _network(connect=mock.Mock(side_effect=_api_error()))
            state._docker.containers.get = mock.Mock(return_value=container)
            contexts = self.assertRaises(state.ApiError) if required else contextlib.nullcontext()
            with (
                mock.patch.object(resources.network_policy, "shared_service_role_for_name", return_value=None),
                contexts,
            ):
                resources._safe_connect(network, "service", required=required)

        network = _network(connect=mock.Mock(side_effect=already))
        state._docker.containers.get = mock.Mock(return_value=container)
        with mock.patch.object(resources.network_policy, "shared_service_role_for_name", return_value=None):
            resources._safe_connect(network, "service", required=True)

        network = _network()
        with (
            mock.patch.object(resources.network_policy, "shared_service_role_for_name", return_value="role"),
            mock.patch.object(resources.network_policy, "shared_service_identity_valid", return_value=True),
        ):
            resources._safe_connect(network, "service", required=True)
        network.connect.assert_called_once_with(container, aliases=None)

    def test_network_dependency_wiring_and_teardown_are_identity_safe(self) -> None:
        network = _network()
        with mock.patch.object(resources, "_safe_connect") as connect:
            resources._wire_network_deps(network, [("postgres", ["postgres"]), ("brain", ["brain"])])
        self.assertEqual(connect.call_count, 2)

        with mock.patch.object(resources, "_teardown_network", return_value=None):
            self.assertTrue(resources._teardown_team_networks(TEAM_ID))
        with mock.patch.object(resources, "_teardown_network", return_value=resources._NETWORK_LOOKUP_FAILED):
            self.assertFalse(resources._teardown_team_networks(TEAM_ID))
        with (
            mock.patch.object(resources, "_teardown_network", return_value=network),
            mock.patch.object(network, "reload", side_effect=resources.docker.errors.DockerException("inspect")),
        ):
            self.assertFalse(resources._teardown_team_networks(TEAM_ID))
        with (
            mock.patch.object(resources, "_teardown_network", return_value=network),
            mock.patch.object(resources.network_policy, "network_identity_valid", return_value=False),
        ):
            self.assertFalse(resources._teardown_team_networks(TEAM_ID))

        member = _container(id="member")
        network = _network(attrs={"Containers": {"member": {}}})
        state._docker.containers.get = mock.Mock(return_value=member)
        with (
            mock.patch.object(resources, "_teardown_network", return_value=network),
            mock.patch.object(resources.network_policy, "network_identity_valid", return_value=True),
            mock.patch.object(resources.network_policy, "network_member_managed", return_value=True),
            mock.patch.object(resources, "_remove_empty_network", return_value=True),
        ):
            self.assertTrue(resources._teardown_team_networks(TEAM_ID))
        network.disconnect.assert_called_once_with("member", force=True)

        for failure in ("inspect", "identity", "disconnect"):
            network = _network(attrs={"Containers": {"member": {}}})
            state._docker.containers.get = mock.Mock(return_value=member)
            contexts = (
                mock.patch.object(member, "reload", side_effect=resources.docker.errors.DockerException("inspect"))
                if failure == "inspect"
                else contextlib.nullcontext()
            )
            managed = failure != "identity"
            disconnect_error = resources.docker.errors.APIError("disconnect") if failure == "disconnect" else None
            with (
                mock.patch.object(resources, "_teardown_network", return_value=network),
                mock.patch.object(resources.network_policy, "network_identity_valid", return_value=True),
                mock.patch.object(resources.network_policy, "network_member_managed", return_value=managed),
                mock.patch.object(network, "disconnect", side_effect=disconnect_error),
                contexts,
            ):
                self.assertFalse(resources._teardown_team_networks(TEAM_ID))

    def test_network_lookup_removal_and_description_contain_failures(self) -> None:
        not_found = resources.docker.errors.NotFound()
        docker_error = resources.docker.errors.DockerException("docker")
        state._docker.networks.get = mock.Mock(side_effect=not_found)
        self.assertIsNone(resources._teardown_network(TEAM_ID, resources.network_policy.CORE_KIND))
        state._docker.networks.get = mock.Mock(side_effect=docker_error)
        failed = resources._teardown_network(TEAM_ID, resources.network_policy.CORE_KIND)
        self.assertIs(failed, resources._NETWORK_LOOKUP_FAILED)

        occupied = _network(attrs={"Containers": {"member": {}}})
        self.assertFalse(resources._remove_empty_network(occupied))
        broken = _network(reload=mock.Mock(side_effect=docker_error))
        self.assertFalse(resources._remove_empty_network(broken))
        empty = _network()
        self.assertTrue(resources._remove_empty_network(empty))
        empty.remove.assert_called_once_with()

        state._inference_store.load = mock.Mock(side_effect=resources.inference_config.InferenceConfigError("state"))
        described = resources._describe(_container())
        self.assertIsNone(described["provider"])
        state._inference_store.load = mock.Mock(return_value=SimpleNamespace(provider="openai", model="model"))
        self.assertEqual(resources._describe(_container())["provider"], "openai")

    def test_cleanup_and_principal_authorization_are_fail_closed(self) -> None:
        error = resources.cleanup_state.CleanupStateError("state")
        with mock.patch.object(resources.cleanup_state, "load", side_effect=error), self.assertRaises(state.ApiError):
            resources._cleanup_record(TEAM_ID)

        for principal in (None, (), ("other", OWNER), ("account", "")):
            with self.subTest(principal=principal), self.assertRaises(state.ApiError):
                resources._principal(TEAM_ID, principal)
        self.assertEqual(resources._principal(TEAM_ID, PRINCIPAL), PRINCIPAL)

        container = _container()
        with (
            mock.patch.object(resources.network_policy, "runtime_identity_valid", return_value=False),
            self.assertRaises(state.ApiError),
        ):
            resources._authorize_container(TEAM_ID, PRINCIPAL, container)
        with (
            mock.patch.object(resources.network_policy, "runtime_identity_valid", return_value=True),
            self.assertRaises(state.ApiError),
        ):
            resources._authorize_container(TEAM_ID, ("account", "other"), container)

        with mock.patch.object(resources, "_get_container", return_value=None), self.assertRaises(state.ApiError):
            resources._authorize(TEAM_ID, PRINCIPAL)
        with (
            mock.patch.object(resources, "_get_container", return_value=container),
            mock.patch.object(resources.network_policy, "runtime_identity_valid", return_value=True),
        ):
            lease = resources._authorize(TEAM_ID, PRINCIPAL)
        self.assertEqual(lease.owner, OWNER)

    def test_destroy_and_current_authorization_revalidate_exact_lifecycle(self) -> None:
        record = SimpleNamespace(nonce="nonce", runtime_id="a" * 64, owner=OWNER)
        container = _container()
        with (
            mock.patch.object(resources, "_get_container", return_value=container),
            mock.patch.object(resources, "_authorize_container", return_value="lease") as authorize,
        ):
            self.assertEqual(resources._authorize_destroy(TEAM_ID, PRINCIPAL), "lease")
        authorize.assert_called_once_with(TEAM_ID, PRINCIPAL, container)

        with (
            mock.patch.object(resources, "_get_container", return_value=None),
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            self.assertRaises(state.ApiError),
        ):
            resources._authorize_destroy(TEAM_ID, PRINCIPAL)
        with (
            mock.patch.object(resources, "_get_container", return_value=None),
            mock.patch.object(resources, "_cleanup_record", return_value=record),
            mock.patch.object(resources.cleanup_state, "principal_authorized", return_value=True),
        ):
            lease = resources._authorize_destroy(TEAM_ID, PRINCIPAL)
        self.assertEqual(lease.cleanup_nonce, "nonce")

        with (
            mock.patch.object(resources, "_cleanup_record", return_value=record),
            mock.patch.object(resources.cleanup_state, "principal_authorized", return_value=True),
            mock.patch.object(resources, "_get_container", return_value=None),
        ):
            self.assertIs(resources._require_cleanup_authorization(TEAM_ID, lease), record)
        with mock.patch.object(resources, "_cleanup_record", return_value=None), self.assertRaises(state.ApiError):
            resources._require_cleanup_authorization(TEAM_ID, lease)

        current = resources._AuthorizationLease(TEAM_ID, container.id, OWNER, PRINCIPAL)
        with (
            mock.patch.object(resources, "_get_container", return_value=container),
            mock.patch.object(resources.network_policy, "runtime_identity_valid", return_value=True),
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_require_team_isolation") as isolation,
        ):
            self.assertIs(resources._require_current_authorization(TEAM_ID, current), container)
        isolation.assert_called_once_with(container)

        with (
            mock.patch.object(resources, "_get_container", return_value=container),
            mock.patch.object(resources.network_policy, "runtime_identity_valid", return_value=True),
            mock.patch.object(resources, "_cleanup_record", return_value=record),
            self.assertRaises(state.ApiError) as pending,
        ):
            resources._require_current_authorization(TEAM_ID, current)
        self.assertEqual(pending.exception.status, HTTPStatus.CONFLICT)


if __name__ == "__main__":
    unittest.main()
