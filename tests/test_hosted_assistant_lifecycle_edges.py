"""Admission, egress, teardown, and inventory edges for Hosted Assistants."""

from __future__ import annotations

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


def _container(**changes):
    values = {
        "id": "c" * 64,
        "name": "assistant",
        "labels": {"team.assistant": ASSISTANT_ID},
        "attrs": {"Config": {"Env": [], "Image": SPEC.image}},
        "status": "running",
        "reload": mock.Mock(),
    }
    values.update(changes)
    return SimpleNamespace(**values)


class HostedAssistantAdmissionEdgeTests(unittest.TestCase):
    def test_egress_store_uses_hosted_authority_and_maps_drift_separately(self) -> None:
        store = lifecycle._egress_store()
        self.assertEqual(store.root, state.ASSISTANT_EGRESS_POLICY_DIR)

        for error, status in (
            (lifecycle.egress_policy.EgressPolicyDriftError("drift"), HTTPStatus.CONFLICT),
            (lifecycle.egress_policy.EgressPolicyError("unavailable"), HTTPStatus.SERVICE_UNAVAILABLE),
        ):
            with self.assertRaises(state.ApiError) as caught:
                lifecycle._raise_egress_error(error)
            self.assertEqual(caught.exception.status, status)

    def test_manifest_and_genesis_admission_hide_package_details(self) -> None:
        container = _container()
        genesis_error = lifecycle.assistant_genesis.GenesisError("private")
        with (
            mock.patch.object(state._assistant_genesis_cache, "get", side_effect=genesis_error),
            self.assertRaises(state.ApiError) as genesis,
        ):
            lifecycle._require_assistant_genesis(container)
        self.assertEqual(genesis.exception.status, HTTPStatus.CONFLICT)

        manifest_error = lifecycle.assistant_manifest.ManifestError("private")
        with (
            mock.patch.object(lifecycle.assistant_manifest, "reviewed_manifest_contract", side_effect=manifest_error),
            self.assertRaises(state.ApiError) as manifest,
        ):
            lifecycle._require_assistant_allowed_hosts(SPEC, container)
        self.assertEqual(manifest.exception.status, HTTPStatus.CONFLICT)

        with (
            mock.patch.object(lifecycle, "_require_assistant_allowed_hosts", return_value=("api.example",)),
            mock.patch.object(lifecycle, "_require_assistant_genesis", return_value="genesis") as genesis_check,
        ):
            self.assertEqual(lifecycle._admit_assistant_contract(SPEC, container), ("api.example",))
        genesis_check.assert_called_once_with(container)

    def test_team_container_and_binding_resolution_are_team_scoped(self) -> None:
        state._docker.containers.list = mock.Mock(return_value=["container"])
        self.assertEqual(lifecycle._team_assistant_containers(TEAM_ID), ["container"])
        state._docker.containers.list.assert_called_once_with(
            all=True,
            filters={"label": ["team.assistant.runtime", f"team.id={TEAM_ID}"]},
        )

        for assistant_id in ("postgres", "missing"):
            with self.subTest(assistant_id=assistant_id), self.assertRaises(
                lifecycle.assistant_registry.AssistantSpecError
        ):
                lifecycle._resolve_team_assistant(TEAM_ID, assistant_id, {})

        with mock.patch.object(lifecycle.publication, "assistant_spec", return_value=SPEC):
            resolved = lifecycle._resolve_team_assistant(TEAM_ID, ASSISTANT_ID, {ASSISTANT_ID: BINDING})
        self.assertEqual(resolved, (ASSISTANT_ID, SPEC))

        dynamic_error = lifecycle.dynamic_assistants.DynamicAssistantError("state")
        with (
            mock.patch.object(state._dynamic_assistants, "get", side_effect=dynamic_error),
            self.assertRaises(state.ApiError) as unavailable,
        ):
            lifecycle._resolve_team_assistant(TEAM_ID, ASSISTANT_ID)
        self.assertEqual(unavailable.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

    def test_binding_snapshot_is_bounded_and_contains_store_failure(self) -> None:
        self.assertEqual(lifecycle._dynamic_binding_snapshot(TEAM_ID, ()), {})
        with mock.patch.object(state._dynamic_assistants, "list", return_value=(BINDING,)):
            self.assertEqual(lifecycle._dynamic_binding_snapshot(TEAM_ID, (ASSISTANT_ID,)), {ASSISTANT_ID: BINDING})

        error = lifecycle.dynamic_assistants.DynamicAssistantError("state")
        with (
            mock.patch.object(state._dynamic_assistants, "list", side_effect=error),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._dynamic_binding_snapshot(TEAM_ID, (ASSISTANT_ID,))

    def test_egress_token_policy_and_environment_adapters_map_store_failures(self) -> None:
        store = mock.Mock()
        store.token.return_value = "token"
        store.validate.return_value = "token"
        store.proxy_environment.return_value = {"HTTPS_PROXY": "proxy"}

        self.assertEqual(lifecycle._assistant_egress_token(TEAM_ID, ASSISTANT_ID, store=store), "token")
        lifecycle._write_egress_policy("token", ("api.example",), store)
        self.assertEqual(lifecycle._validate_egress_policy(TEAM_ID, ASSISTANT_ID, ("api.example",), store), "token")
        self.assertEqual(lifecycle._validate_admitted_egress(TEAM_ID, ASSISTANT_ID, (), store), None)
        self.assertEqual(lifecycle._egress_proxy_environment("token", store), {"HTTPS_PROXY": "proxy"})

        for method, invoke in (
            ("token", lambda: lifecycle._assistant_egress_token(TEAM_ID, ASSISTANT_ID, store=store)),
            ("write", lambda: lifecycle._write_egress_policy("token", (), store)),
            ("validate", lambda: lifecycle._validate_egress_policy(TEAM_ID, ASSISTANT_ID, (), store)),
            ("proxy_environment", lambda: lifecycle._egress_proxy_environment("token", store)),
        ):
            getattr(store, method).side_effect = lifecycle.egress_policy.EgressPolicyError("failed")
            with self.subTest(method=method), self.assertRaises(state.ApiError):
                invoke()
            getattr(store, method).side_effect = None

    def test_proxy_environment_requires_exact_proxy_projection(self) -> None:
        store = mock.Mock()
        store.proxy_environment.return_value = {"HTTPS_PROXY": "proxy"}
        lifecycle._validate_assistant_proxy_environment(
            _container(attrs={"Config": {"Env": ["HTTPS_PROXY=proxy", "OTHER=value"]}}),
            "token",
            ("api.example",),
            store,
        )

        cases = (
            (_container(attrs={}), None, ()),
            (_container(attrs={"Config": {"Env": []}}), None, ("api.example",)),
            (_container(attrs={"Config": {"Env": ["HTTPS_PROXY=other"]}}), "token", ("api.example",)),
        )
        for container, token, allowed_hosts in cases:
            with self.subTest(token=token, allowed_hosts=allowed_hosts), self.assertRaises(state.ApiError):
                lifecycle._validate_assistant_proxy_environment(container, token, allowed_hosts, store)

    def test_egress_reservation_and_activation_are_capability_bound(self) -> None:
        store = mock.Mock()
        self.assertEqual(lifecycle._reserve_egress_environment(TEAM_ID, ASSISTANT_ID, (), store), (None, {}))
        with (
            mock.patch.object(lifecycle, "_assistant_egress_token", return_value=None),
            self.assertRaises(state.ApiError) as unavailable,
        ):
            lifecycle._reserve_egress_environment(TEAM_ID, ASSISTANT_ID, ("api.example",), store)
        self.assertEqual(unavailable.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

        with (
            mock.patch.object(lifecycle, "_assistant_egress_token", return_value="token"),
            mock.patch.object(lifecycle, "_egress_proxy_environment", return_value={"HTTPS_PROXY": "proxy"}),
        ):
            self.assertEqual(
                lifecycle._reserve_egress_environment(TEAM_ID, ASSISTANT_ID, ("api.example",), store),
                ("token", {"HTTPS_PROXY": "proxy"}),
            )

        network = object()
        lifecycle._activate_admitted_egress(network, None, (), store)
        with self.assertRaises(state.ApiError) as internal:
            lifecycle._activate_admitted_egress(network, None, ("api.example",), store)
        self.assertEqual(internal.exception.status, HTTPStatus.INTERNAL_SERVER_ERROR)
        with (
            mock.patch.object(lifecycle, "_write_egress_policy") as write,
            mock.patch.object(resources, "_safe_connect") as connect,
        ):
            lifecycle._activate_admitted_egress(network, "token", ("api.example",), store)
        write.assert_called_once_with("token", ("api.example",), store)
        connect.assert_called_once()

    def test_policy_removal_readiness_and_wait_contracts(self) -> None:
        store = mock.Mock()
        with mock.patch.object(lifecycle, "_egress_store", return_value=store):
            self.assertTrue(lifecycle._remove_egress_policy(TEAM_ID, ASSISTANT_ID))
        store.remove.side_effect = lifecycle.egress_policy.EgressPolicyError("failed")
        with mock.patch.object(lifecycle, "_egress_store", return_value=store):
            self.assertFalse(lifecycle._remove_egress_policy(TEAM_ID, ASSISTANT_ID))

        running = _container(status="running")
        self.assertEqual(lifecycle._wait_assistant_ready(running), (True, "running"))
        exited = _container(status="exited")
        self.assertEqual(lifecycle._wait_assistant_ready(exited), (False, "container not running (status=exited)"))
        restarting = _container(status="restarting")
        with (
            mock.patch.object(state, "HEALTH_RETRIES", 2),
            mock.patch.object(lifecycle.time, "sleep") as sleep,
        ):
            self.assertEqual(lifecycle._wait_assistant_ready(restarting), (False, "Assistant container never started"))
        sleep.assert_called_once()

        docker_error = lifecycle.docker.errors.DockerException("inspect")
        self.assertEqual(
            lifecycle._assistant_ready_now(_container(reload=mock.Mock(side_effect=docker_error))),
            (False, "container readiness could not be verified"),
        )
        self.assertEqual(
            lifecycle._assistant_ready_now(_container(status="created")),
            (False, "container not running (status=created)"),
        )
        self.assertEqual(lifecycle._assistant_ready_now(running), (True, "running"))

    def test_teardown_contains_discovery_inspection_identity_and_policy_failures(self) -> None:
        docker_error = lifecycle.docker.errors.DockerException("docker")
        with mock.patch.object(resources, "_get_container", side_effect=docker_error):
            result = lifecycle._teardown_assistant(TEAM_ID, ASSISTANT_ID)
        self.assertEqual(result, resources._CleanupResult(False, True))

        broken = _container(reload=mock.Mock(side_effect=docker_error))
        self.assertEqual(
            lifecycle._teardown_assistant(TEAM_ID, ASSISTANT_ID, container=broken),
            resources._CleanupResult(False, True),
        )
        with mock.patch.object(lifecycle.network_policy, "assistant_identity_valid", return_value=False):
            self.assertEqual(
                lifecycle._teardown_assistant(TEAM_ID, ASSISTANT_ID, container=_container()),
                resources._CleanupResult(False, True),
            )

        container = _container()
        with (
            mock.patch.object(lifecycle.network_policy, "assistant_identity_valid", return_value=True),
            mock.patch.object(lifecycle, "_remove_egress_policy", return_value=False),
            mock.patch.object(resources, "_fail_stop_team", side_effect=state.ApiError(503, "stopped")) as fail_stop,
        ):
            result = lifecycle._teardown_assistant(TEAM_ID, ASSISTANT_ID, container=container)
        self.assertEqual(result, resources._CleanupResult(False, True))
        fail_stop.assert_called_once_with(container)

    def test_successful_teardown_discards_all_container_contract_caches(self) -> None:
        container = _container()
        with (
            mock.patch.object(lifecycle.network_policy, "assistant_identity_valid", return_value=True),
            mock.patch.object(lifecycle, "_remove_egress_policy", return_value=True),
            mock.patch.object(resources, "_remove_team_container", return_value=True),
            mock.patch.object(state._assistant_genesis_cache, "discard") as genesis,
            mock.patch.object(state._assistant_allowed_hosts_cache, "discard") as hosts,
            mock.patch.object(state._assistant_machine_contract_cache, "discard") as machine,
        ):
            result = lifecycle._teardown_assistant(TEAM_ID, ASSISTANT_ID, container=container)
        self.assertEqual(result, resources._CleanupResult(True, True))
        for discarded in (genesis, hosts, machine):
            discarded.assert_called_once_with(container.id)

    def test_integration_retention_cancels_only_after_pruning(self) -> None:
        error = lifecycle.integration_store.OAuthIntegrationStoreError("state")
        with (
            mock.patch.object(state._assistant_integrations, "retain_declared", side_effect=error),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._retain_admitted_assistant_integrations(TEAM_ID, ASSISTANT_ID, SPEC)

        for pruned in (False, True):
            with (
                mock.patch.object(state._assistant_integrations, "retain_declared", return_value=pruned),
                mock.patch.object(state._integration_challenges, "cancel_team") as cancel,
            ):
                lifecycle._retain_admitted_assistant_integrations(TEAM_ID, ASSISTANT_ID, SPEC)
            self.assertEqual(cancel.call_count, int(pruned))

    def test_list_and_icon_inventory_require_current_binding(self) -> None:
        lease = SimpleNamespace()
        container = _container()
        with (
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(lifecycle, "_team_assistant_containers", return_value=[container]),
            mock.patch.object(lifecycle, "_dynamic_binding_snapshot", return_value={ASSISTANT_ID: BINDING}),
            mock.patch.object(lifecycle, "_resolve_team_assistant", return_value=(ASSISTANT_ID, SPEC)),
        ):
            result = lifecycle._list_assistants(TEAM_ID, lease)
        self.assertEqual(result["assistants"][0]["assistant"], ASSISTANT_ID)

        with (
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(state._dynamic_assistants, "get", return_value=None),
            self.assertRaises(state.ApiError) as absent,
        ):
            lifecycle._assistant_icon(TEAM_ID, ASSISTANT_ID, lease)
        self.assertEqual(absent.exception.status, HTTPStatus.NOT_FOUND)

        icon_error = lifecycle.assistant_icons.AssistantIconError("icon")
        with (
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(state._dynamic_assistants, "get", return_value=BINDING),
            mock.patch.object(state._assistant_icons, "read", side_effect=icon_error),
            self.assertRaises(state.ApiError) as unavailable,
        ):
            lifecycle._assistant_icon(TEAM_ID, ASSISTANT_ID, lease)
        self.assertEqual(unavailable.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)

        with (
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(state._dynamic_assistants, "get", return_value=BINDING),
            mock.patch.object(state._assistant_icons, "read", return_value=b"icon"),
        ):
            self.assertEqual(lifecycle._assistant_icon(TEAM_ID, ASSISTANT_ID, lease), b"icon")


if __name__ == "__main__":
    unittest.main()
