"""Edge coverage for Hosted Team resource and isolation primitives."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

resources = harness.hosted_resources
state = harness.runtime_state

TEAM_ID = "team_1"
OWNER = "account_1"


def _container(**changes):
    values = {
        "id": "a" * 64,
        "name": "team",
        "labels": {"team.id": TEAM_ID, "team.name": "Team", "team.owner": OWNER, "team.runtime": "1"},
        "attrs": {
            "HostConfig": {"Runtime": resources.container_spec.RUNTIME, "Memory": 128},
            "State": {"Running": True},
            "Config": {"Labels": {"team.id": TEAM_ID, "team.runtime": "1"}},
        },
        "reload": mock.Mock(),
        "stop": mock.Mock(),
        "kill": mock.Mock(),
        "remove": mock.Mock(),
        "start": mock.Mock(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


class HostedResourceEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        state._capacity_reservations.clear()

    def tearDown(self) -> None:
        state._capacity_reservations.clear()

    def test_team_name_thread_identity_and_container_lookup_validate_inputs(self) -> None:
        for value in ("", " x", "x ", "x\n", "x" * 81, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resources._validated_team_name(value)
        self.assertEqual(resources._validated_team_name("Gestão"), "Gestão")
        with self.assertRaises(state.ApiError):
            resources._team_name_from_anchor(_container(labels={}))

        for team_id, anchor in (("INVALID", "a" * 64), (TEAM_ID, "short"), (TEAM_ID, "z" * 64)):
            with self.subTest(team_id=team_id, anchor=anchor), self.assertRaises(state.ApiError):
                resources._brain_thread_id(team_id, anchor)
        self.assertEqual(resources._brain_thread_id(TEAM_ID, "a" * 64), f"hosted:{TEAM_ID}:{'a' * 64}:default")

        state._docker.containers.get = mock.Mock(side_effect=resources.docker.errors.NotFound())
        self.assertIsNone(resources._get_container("missing"))
        found = object()
        state._docker.containers.get = mock.Mock(return_value=found)
        self.assertIs(resources._get_container("team"), found)

    def test_daemon_runtime_and_container_runtime_fail_closed(self) -> None:
        docker_error = resources.docker.errors.DockerException("docker")
        with mock.patch.object(state._docker, "info", side_effect=docker_error, create=True), self.assertRaises(
            state.ApiError
        ):
            resources._require_team_runtime()
        for info in (None, {}):
            with (
                mock.patch.object(state._docker, "info", return_value=info, create=True),
                mock.patch.object(resources.network_policy, "daemon_runtime_registration_valid", return_value=False),
                self.assertRaises(state.ApiError),
            ):
                resources._require_team_runtime()
        with (
            mock.patch.object(state._docker, "info", return_value={}, create=True),
            mock.patch.object(resources.network_policy, "daemon_runtime_registration_valid", return_value=True),
            mock.patch.object(resources.network_policy, "daemon_security_options_valid", return_value=False),
            self.assertRaises(state.ApiError),
        ):
            resources._require_team_runtime()
        with (
            mock.patch.object(state._docker, "info", return_value={}, create=True),
            mock.patch.object(resources.network_policy, "daemon_runtime_registration_valid", return_value=True),
            mock.patch.object(resources.network_policy, "daemon_security_options_valid", return_value=True),
        ):
            resources._require_team_runtime()

        with self.assertRaises(state.ApiError):
            resources._team_runtime(_container(reload=mock.Mock(side_effect=docker_error)))
        self.assertEqual(resources._team_runtime(_container(attrs={}), refresh=False), "runc")

    def test_trusted_image_and_artifact_materialization_contain_failures(self) -> None:
        with self.assertRaises(state.ApiError):
            resources._trusted_image_id("")
        image = SimpleNamespace(id="sha256:image")
        state._docker.images.get = mock.Mock(return_value=image)
        self.assertEqual(resources._trusted_image_id("image"), "sha256:image")
        for result in (SimpleNamespace(), SimpleNamespace(id="")):
            state._docker.images.get = mock.Mock(return_value=result)
            with self.assertRaises(state.ApiError):
                resources._trusted_image_id("image")

        non_digest = SimpleNamespace(image="image:tag")
        with mock.patch.object(resources.assistant_artifact, "ensure_digest_artifact") as ensure:
            resources._prepare_assistant_image(non_digest)
        ensure.assert_not_called()
        trust_error = resources.assistant_artifact.ImageTrustError("untrusted")
        with (
            mock.patch.object(resources.assistant_registry, "is_digest_image", return_value=True),
            mock.patch.object(resources.assistant_artifact, "ensure_digest_artifact", side_effect=trust_error),
            self.assertRaises(state.ApiError),
        ):
            resources._prepare_assistant_image(harness.HOSTED_SPEC)

    def test_workload_image_role_resolves_runtime_explicit_and_dynamic_assistant(self) -> None:
        container = _container()
        with (
            mock.patch.object(resources.network_policy, "runtime_identity_valid", return_value=True),
            mock.patch.object(resources, "_trusted_image_id", return_value="id"),
        ):
            result = resources._trusted_workload_image(container, TEAM_ID)
        self.assertEqual(result, (resources.container_spec.IMAGE, "id", False))

        assistant = _container(attrs={"Config": {"Labels": {"team.assistant": "assistant"}}})
        with (
            mock.patch.object(resources.network_policy, "runtime_identity_valid", return_value=False),
            mock.patch.object(resources, "_trusted_image_id", return_value="id"),
        ):
            self.assertEqual(
                resources._trusted_workload_image(assistant, TEAM_ID, harness.HOSTED_SPEC),
                (harness.HOSTED_SPEC.image, "id", True),
            )

        dynamic_error = resources.dynamic_assistants.DynamicAssistantError("state")
        with (
            mock.patch.object(resources.network_policy, "runtime_identity_valid", return_value=False),
            mock.patch.object(state._dynamic_assistants, "get", side_effect=dynamic_error),
            self.assertRaises(state.ApiError),
        ):
            resources._trusted_workload_image(assistant, TEAM_ID)
        with (
            mock.patch.object(resources.network_policy, "runtime_identity_valid", return_value=False),
            mock.patch.object(state._dynamic_assistants, "get", return_value=None),
            self.assertRaises(state.ApiError),
        ):
            resources._trusted_workload_image(assistant, TEAM_ID)

    def test_isolation_mode_rejects_runtime_state_identity_security_and_network_drift(self) -> None:
        container = _container()
        with mock.patch.object(resources, "_team_runtime", return_value="runc"), self.assertRaises(state.ApiError):
            resources._require_running_team_isolation(container)
        for current_state in ({}, {"Running": "yes"}, {"Running": False}):
            candidate = _container(attrs=container.attrs | {"State": current_state})
            with (
                mock.patch.object(resources, "_team_runtime", return_value=resources.container_spec.RUNTIME),
                self.assertRaises(state.ApiError),
            ):
                resources._require_running_team_isolation(candidate)

        missing_team = _container(attrs=container.attrs | {"Config": {"Labels": {}}})
        with (
            mock.patch.object(resources, "_team_runtime", return_value=resources.container_spec.RUNTIME),
            self.assertRaises(state.ApiError),
        ):
            resources._require_running_team_isolation(missing_team)

        with (
            mock.patch.object(resources, "_team_runtime", return_value=resources.container_spec.RUNTIME),
            mock.patch.object(resources, "_trusted_workload_image", return_value=("image", "id", False)),
            mock.patch.object(resources.network_policy, "workload_security_valid", return_value=False),
            self.assertRaises(state.ApiError),
        ):
            resources._require_running_team_isolation(container)

        with (
            mock.patch.object(resources, "_team_runtime", return_value=resources.container_spec.RUNTIME),
            mock.patch.object(resources, "_trusted_workload_image", return_value=("image", "id", False)),
            mock.patch.object(resources.network_policy, "workload_security_valid", return_value=True),
            mock.patch.object(
                state._docker.networks,
                "get",
                side_effect=resources.docker.errors.DockerException("network"),
            ),
            self.assertRaises(state.ApiError),
        ):
            resources._require_running_team_isolation(container)

    def test_isolation_mode_caches_network_and_checks_endpoint_and_live_membership(self) -> None:
        container = _container()
        network = SimpleNamespace(attrs={})
        memo = {}

        def run(endpoint: bool, membership: bool) -> None:
            with (
                mock.patch.object(resources, "_team_runtime", return_value=resources.container_spec.RUNTIME),
                mock.patch.object(resources, "_trusted_workload_image", return_value=("image", "id", False)),
                mock.patch.object(resources.network_policy, "workload_security_valid", return_value=True),
                mock.patch.object(resources.network_policy, "runtime_identity_valid", return_value=True),
                mock.patch.object(state._docker.networks, "get", return_value=network),
                mock.patch.object(resources, "_require_network_policy"),
                mock.patch.object(resources.network_policy, "workload_endpoint_valid", return_value=endpoint),
                mock.patch.object(resources.network_policy, "workload_live_membership_valid", return_value=membership),
            ):
                resources._require_running_team_isolation(container, memo, refreshed=True)

        with self.assertRaises(state.ApiError):
            run(False, True)
        with self.assertRaises(state.ApiError):
            run(True, False)
        run(True, True)
        self.assertIn(f"network:{TEAM_ID}:{resources.network_policy.CORE_KIND}", memo)

    def test_stop_kill_remove_and_remaining_container_paths_are_bounded(self) -> None:
        not_found = resources.docker.errors.NotFound()
        docker_error = resources.docker.errors.DockerException("docker")
        resources._fail_stop_team(_container(stop=mock.Mock(side_effect=not_found)))

        stopped = _container(stop=mock.Mock(side_effect=docker_error))
        with mock.patch.object(resources, "_team_not_running", return_value=True):
            resources._fail_stop_team(stopped)

        killed = _container(stop=mock.Mock(side_effect=docker_error), kill=mock.Mock(side_effect=not_found))
        with mock.patch.object(resources, "_team_not_running", return_value=False):
            resources._fail_stop_team(killed)

        stuck = _container(stop=mock.Mock(side_effect=docker_error), kill=mock.Mock(side_effect=docker_error))
        with mock.patch.object(resources, "_team_not_running", return_value=False), self.assertRaises(state.ApiError):
            resources._fail_stop_team(stuck)

        with mock.patch.object(resources, "_fail_stop_team", side_effect=state.ApiError(500, "stuck")):
            self.assertFalse(resources._remove_team_container(_container()))
        with (
            mock.patch.object(resources, "_fail_stop_team"),
            mock.patch.object(resources, "_remaining_container", return_value=None),
        ):
            self.assertTrue(resources._remove_team_container(_container()))
        with (
            mock.patch.object(resources, "_fail_stop_team"),
            mock.patch.object(resources, "_remaining_container", return_value=resources._CONTAINER_LOOKUP_FAILED),
        ):
            self.assertFalse(resources._remove_team_container(_container()))

        state._docker.containers.get = mock.Mock(side_effect=not_found)
        self.assertIsNone(resources._remaining_container("id"))
        state._docker.containers.get = mock.Mock(side_effect=docker_error)
        self.assertIs(resources._remaining_container("id"), resources._CONTAINER_LOOKUP_FAILED)

    def test_start_fail_stops_ambiguous_start_and_live_isolation_failure(self) -> None:
        container = _container()
        docker_error = resources.docker.errors.DockerException("start")
        with (
            mock.patch.object(resources, "_require_team_isolation"),
            mock.patch.object(resources, "_require_team_runtime"),
            mock.patch.object(container, "start", side_effect=docker_error),
            mock.patch.object(resources, "_fail_stop_team") as fail_stop,
            self.assertRaises(state.ApiError),
        ):
            resources._start_team_with_isolation(container)
        fail_stop.assert_called_once_with(container)

        with (
            mock.patch.object(resources, "_require_team_isolation"),
            mock.patch.object(resources, "_require_team_runtime"),
            mock.patch.object(resources, "_require_running_team_isolation", side_effect=state.ApiError(503, "drift")),
            mock.patch.object(resources, "_fail_stop_team") as fail_stop,
            self.assertRaises(state.ApiError),
        ):
            resources._start_team_with_isolation(container)
        fail_stop.assert_called_once_with(container)

    def test_memory_inventory_validates_limits_and_deduplicates_resources(self) -> None:
        assistant = _container(
            id="assistant",
            name="assistant",
            labels={"team.id": TEAM_ID, "team.owner": OWNER, "team.assistant.runtime": "1", "team.assistant": "a"},
        )
        team = _container(id="team")
        state._docker.containers.list = mock.Mock(side_effect=([team, assistant], [assistant]))
        self.assertEqual({item.id for item in resources._admitted_resource_containers()}, {"team", "assistant"})

        state._docker.containers.list = mock.Mock(side_effect=resources.docker.errors.DockerException("inventory"))
        with self.assertRaises(state.ApiError):
            resources._admitted_resource_containers()

        with mock.patch.object(resources, "_admitted_resource_containers", return_value=[team, assistant]):
            usage = resources._memory_usage(exclude_keys=frozenset({"assistant:team_1:a"}))
        self.assertEqual(usage.total, 128)

        for raw_limit in (0, True, None):
            invalid = _container(attrs={"HostConfig": {"Memory": raw_limit}})
            with (
                mock.patch.object(resources, "_admitted_resource_containers", return_value=[invalid]),
                self.assertRaises(state.ApiError),
            ):
                resources._memory_usage()

    def test_physical_inventory_and_capacity_limits_fail_closed(self) -> None:
        state._docker.containers.list = mock.Mock(side_effect=resources.docker.errors.DockerException("inventory"))
        with self.assertRaises(state.ApiError):
            resources._physical_teams(exclude_keys=frozenset())

        reservation = resources._CapacityReservation("team:new", OWNER, 60, True)
        physical = [_container()]
        existing = (resources._CapacityReservation("team:pending", OWNER, 10, True),)
        usage = resources._MemoryUsage(20, {OWNER: 20})
        with (
            mock.patch.object(state, "MAX_TEAMS", 2),
            self.assertRaises(state.ApiError),
        ):
            resources._validate_capacity(reservation, physical, usage, existing)
        with (
            mock.patch.object(state, "MAX_TEAMS", 10),
            mock.patch.object(state, "MAX_TEAMS_PER_OWNER", 2),
            self.assertRaises(state.ApiError),
        ):
            resources._validate_capacity(reservation, physical, usage, existing)
        with (
            mock.patch.object(state, "MAX_TEAMS", 10),
            mock.patch.object(state, "MAX_TEAMS_PER_OWNER", 10),
            mock.patch.object(state, "GLOBAL_MEMORY_BUDGET_BYTES", 80),
            self.assertRaises(state.ApiError),
        ):
            resources._validate_capacity(reservation, [], usage, existing)
        with (
            mock.patch.object(state, "MAX_TEAMS", 10),
            mock.patch.object(state, "MAX_TEAMS_PER_OWNER", 10),
            mock.patch.object(state, "GLOBAL_MEMORY_BUDGET_BYTES", 1000),
            mock.patch.object(state, "OWNER_MEMORY_BUDGET_BYTES", 80),
            self.assertRaises(state.ApiError),
        ):
            resources._validate_capacity(reservation, [], usage, existing)

    def test_capacity_detects_second_phase_duplicate_and_preserves_foreign_replacement(self) -> None:
        def inject_duplicate(**_kwargs):
            state._capacity_reservations["team:new"] = object()
            return resources._MemoryUsage(0, {})

        with (
            mock.patch.object(resources, "_memory_usage", side_effect=inject_duplicate),
            self.assertRaises(state.ApiError),
            resources._reserve_capacity("team:new", OWNER, 1, team_slot=False),
        ):
            self.fail("duplicate admission was accepted")
        state._capacity_reservations.clear()

        replacement = object()
        with (
            mock.patch.object(resources, "_memory_usage", return_value=resources._MemoryUsage(0, {})),
            resources._reserve_capacity("team:new", OWNER, 1, team_slot=False),
        ):
            state._capacity_reservations["team:new"] = replacement
        self.assertIs(state._capacity_reservations["team:new"], replacement)


if __name__ == "__main__":
    unittest.main()
