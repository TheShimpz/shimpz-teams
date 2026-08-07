from __future__ import annotations

import copy
import importlib.util
import os
import unittest
from decimal import InvalidOperation
from unittest import mock

from core.container import network as policy
from tests import test_network_policy as fixtures


class NetworkPolicyEdgeTests(unittest.TestCase):
    def test_resource_parsers_and_module_limits_fail_closed(self) -> None:
        self.assertEqual(policy._normalized_capabilities(None), set())
        for value in ("invalid", "0", "0.1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.hard_memory_bytes(value, setting="TEST_LIMIT")
        with (
            mock.patch.object(policy, "Decimal", side_effect=InvalidOperation),
            self.assertRaises(ValueError),
        ):
            policy.hard_memory_bytes("1m", setting="TEST_LIMIT")

        spec = importlib.util.spec_from_file_location("invalid_network_limits", policy.__file__)
        if spec is None or spec.loader is None:
            self.fail("network policy module spec is unavailable")
        module = importlib.util.module_from_spec(spec)
        with (
            mock.patch.dict(os.environ, {"SHIMPZ_TEAM_NANO_CPUS": "0"}),
            self.assertRaisesRegex(ValueError, "CPU and PID limits must be positive"),
        ):
            spec.loader.exec_module(module)

    def test_names_labels_and_container_shapes_are_strict(self) -> None:
        with self.assertRaises(ValueError):
            policy.team_assistant_container_name("alpha", "INVALID")
        self.assertTrue(policy.volume_name("alpha", policy.CONFIG_VOLUME_KIND).endswith("_config"))
        self.assertTrue(policy.volume_name("alpha", policy.WORKSPACE_VOLUME_KIND).endswith("_workspace"))
        with self.assertRaises(ValueError):
            policy.volume_name("alpha", "unknown")
        with self.assertRaises(ValueError):
            policy.volume_labels("alpha", "unknown")
        with self.assertRaises(ValueError):
            policy.network_labels("alpha", "unknown")
        self.assertEqual(policy._container_name({"Names": ["/fallback"]}), "fallback")
        self.assertEqual(policy._container_name({"Names": []}), "")
        self.assertEqual(policy._container_name({"Names": [1]}), "")

    def test_role_helpers_reject_unknown_and_cross_team_workloads(self) -> None:
        core, containers = fixtures._valid_topology()
        assistant = containers["assistant-id"]
        self.assertTrue(policy.assistant_identity_valid(assistant, fixtures.TEAM_ID, fixtures.ASSISTANT_ID))
        self.assertIsNone(policy.workload_network_kinds({}, fixtures.TEAM_ID))
        with self.assertRaises(ValueError):
            policy.shared_service_labels("unknown")
        self.assertEqual(policy.shared_service_role_for_name(policy.POSTGRES_CONTAINER), policy.POSTGRES_ROLE)
        self.assertIsNone(policy._member_role(assistant, fixtures.TEAM_ID, "unknown"))
        self.assertFalse(policy.workload_live_membership_valid(core, {}, fixtures.TEAM_ID, policy.CORE_KIND))

    def test_alias_and_member_validation_rejects_malformed_engine_data(self) -> None:
        self.assertIsNone(policy._normalized_aliases("alias"))
        self.assertIsNone(policy._normalized_aliases([""]))
        self.assertFalse(policy._aliases_valid({}, ("runtime", ""), {"Aliases": "runtime"}))

        core, containers = fixtures._valid_topology()
        invalid_identity = copy.deepcopy(core)
        invalid_identity["Name"] = "foreign"
        self.assertFalse(
            policy.network_members_valid(
                invalid_identity,
                containers,
                fixtures.TEAM_ID,
                policy.CORE_KIND,
                require_runtime=True,
                require_dependencies=True,
            )
        )

        invalid_id = copy.deepcopy(core)
        invalid_id["Id"] = 1
        self.assertFalse(
            policy.network_members_valid(
                invalid_id,
                containers,
                fixtures.TEAM_ID,
                policy.CORE_KIND,
                require_runtime=True,
                require_dependencies=True,
            )
        )

        missing_member = copy.deepcopy(containers)
        missing_member.pop("runtime-id")
        self.assertFalse(
            policy.network_members_valid(
                core,
                missing_member,
                fixtures.TEAM_ID,
                policy.CORE_KIND,
                require_runtime=True,
                require_dependencies=True,
            )
        )

        duplicate_core = copy.deepcopy(core)
        duplicate_containers = copy.deepcopy(containers)
        duplicate_runtime = copy.deepcopy(containers["runtime-id"])
        duplicate_runtime["Id"] = "runtime-id-2"
        duplicate_containers["runtime-id-2"] = duplicate_runtime
        duplicate_core["Containers"]["runtime-id-2"] = {}
        self.assertFalse(
            policy.network_members_valid(
                duplicate_core,
                duplicate_containers,
                fixtures.TEAM_ID,
                policy.CORE_KIND,
                require_runtime=True,
                require_dependencies=True,
            )
        )

    def test_security_shape_helpers_reject_malformed_engine_data(self) -> None:
        self.assertFalse(policy._security_options_valid({"SecurityOpt": "no-new-privileges"}))
        self.assertFalse(policy._tmpfs_valid({"Tmpfs": []}, size=1))
        mount_path = policy.TMPFS_MOUNT_PATH
        self.assertFalse(policy._tmpfs_valid({"Tmpfs": {mount_path: 1}}, size=1))
        self.assertFalse(policy._tmpfs_valid({"Tmpfs": {mount_path: "size"}}, size=1))
        self.assertFalse(policy._tmpfs_valid({"Tmpfs": {mount_path: "size=bad"}}, size=1))
        self.assertFalse(policy._ulimits_valid({"Ulimits": []}, nofile=1))
        self.assertFalse(policy._log_config_valid({"LogConfig": []}))

        _core, containers = fixtures._valid_topology()
        runtime = containers["runtime-id"]
        with mock.patch.object(policy, "workload_network_kinds", return_value=None):
            self.assertFalse(
                policy.workload_security_valid(
                    runtime,
                    fixtures.TEAM_ID,
                    "runsc",
                    expected_image_ref=fixtures.RUNTIME_IMAGE_REF,
                    expected_image_id=fixtures.RUNTIME_IMAGE_ID,
                )
            )
        runtime["Mounts"] = None
        self.assertFalse(
            policy.workload_security_valid(
                runtime,
                fixtures.TEAM_ID,
                "runsc",
                expected_image_ref=fixtures.RUNTIME_IMAGE_REF,
                expected_image_id=fixtures.RUNTIME_IMAGE_ID,
            )
        )
        self.assertFalse(policy.daemon_security_options_valid({"SecurityOptions": "builtin"}))
        self.assertFalse(policy.daemon_runtime_registration_valid({"Runtimes": []}, "runsc", policy.TEAM_RUNTIME_PATH))


if __name__ == "__main__":
    unittest.main()
