from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from docker.errors import DockerException
from local_controller_harness import TestPublicationRegistry

from local import app as local_app
from local.assistant import egress as local_egress


class _Proxy:
    def __init__(self, space_id: str) -> None:
        self.name = "local-egress-proxy"
        self.status = "running"
        self.attrs = {
            "Config": {
                "User": "10005:10005",
                "Labels": {
                    local_app.MANAGED_LABEL: "1",
                    local_app.PROFILE_LABEL: local_app.PROFILE,
                    local_app.SPACE_LABEL: space_id,
                    local_app.KIND_LABEL: local_egress.ASSISTANT_EGRESS_KIND,
                },
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "CapAdd": None,
                "SecurityOpt": ["no-new-privileges:true"],
                "Privileged": False,
                "PortBindings": {},
            },
            "Mounts": [{"Destination": "/policy", "RW": False}],
            "NetworkSettings": {"Networks": {"outbound": {"Aliases": ["local-egress-proxy"]}}},
        }

    def reload(self) -> None:
        return


class _Network:
    name = "team-network"

    def __init__(self, proxy: _Proxy) -> None:
        self.proxy = proxy
        self.attrs = {"Containers": {}}

    def reload(self) -> None:
        networks = self.proxy.attrs["NetworkSettings"]["Networks"]
        self.attrs["Containers"] = {"proxy": {"Name": self.proxy.name}} if self.name in networks else {}

    def connect(self, proxy: _Proxy, *, aliases: list[str]) -> None:
        proxy.attrs["NetworkSettings"]["Networks"][self.name] = {"Aliases": aliases}
        self.reload()

    def disconnect(self, proxy: _Proxy) -> None:
        proxy.attrs["NetworkSettings"]["Networks"].pop(self.name, None)
        self.reload()


class _Containers:
    def __init__(self, proxy: _Proxy) -> None:
        self.proxy = proxy
        self.installed: list[object] = []

    def get(self, name: str):
        if name != self.proxy.name:
            raise AssertionError(f"unexpected container {name}")
        return self.proxy

    def list(self, **_kwargs):
        return self.installed


class LocalAssistantEgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.policy_root = Path(self.directory.name) / "policy"
        self.policy_root.mkdir(mode=0o750)
        self.policy_root.chmod(0o750)
        self.proxy = _Proxy("local-space")
        self.network = _Network(self.proxy)
        self.controller = object.__new__(local_app.LocalController)
        self.controller.space_id = "local-space"
        self.controller.client = types.SimpleNamespace(containers=_Containers(self.proxy))
        self.spec = types.SimpleNamespace(
            assistant_id="shimpz-cloudflare",
            allowed_hosts=("api.open-meteo.com", "geocoding-api.open-meteo.com"),
        )
        self.controller.registry = TestPublicationRegistry({self.spec.assistant_id: self.spec})
        self.controller._wire_collaborators()
        self.patches = (
            mock.patch.object(local_egress, "ASSISTANT_EGRESS_POLICY_DIR", self.policy_root),
            mock.patch.object(local_egress, "ASSISTANT_EGRESS_POLICY_GID", os.getgid()),
            mock.patch.object(local_egress, "ASSISTANT_EGRESS_CONTAINER", self.proxy.name),
        )
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_policy_is_private_stable_exact_and_proxy_attachment_is_dynamic(self) -> None:
        environment = self.controller.assistant_lifecycle._activate_assistant_egress(
            "team_1",
            self.spec,
            self.network,
            tuple(sorted(self.spec.allowed_hosts)),
        )

        token = environment["HTTPS_PROXY"].split("@", 1)[0].rsplit("/", 1)[-1]
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.assertEqual(environment["HTTPS_PROXY"], environment["https_proxy"])
        self.assertEqual(environment["NO_PROXY"], "127.0.0.1,localhost")
        self.assertIn(self.network.name, self.proxy.attrs["NetworkSettings"]["Networks"])
        self.assertIn(
            local_egress.ASSISTANT_EGRESS_ALIAS,
            self.proxy.attrs["NetworkSettings"]["Networks"][self.network.name]["Aliases"],
        )
        policy = self.policy_root / f"{token}.json"
        self.assertEqual(json.loads(policy.read_text(encoding="ascii")), sorted(self.spec.allowed_hosts))
        self.assertEqual(policy.stat().st_mode & 0o777, 0o640)
        token_files = list((self.policy_root / ".tokens").glob("*.token"))
        self.assertEqual(len(token_files), 1)
        self.assertEqual(token_files[0].stat().st_mode & 0o777, 0o600)

        repeated = self.controller.assistant_lifecycle._activate_assistant_egress(
            "team_1",
            self.spec,
            self.network,
            tuple(sorted(self.spec.allowed_hosts)),
        )
        self.assertEqual(repeated, environment)
        self.assertEqual(
            self.controller.assistant_lifecycle._validate_egress_policy(
                "team_1",
                self.spec,
                tuple(sorted(self.spec.allowed_hosts)),
            ),
            environment,
        )

    def test_activation_constructs_one_policy_store_for_the_operation(self) -> None:
        with mock.patch.object(
            local_egress.egress_policy,
            "EgressPolicyStore",
            wraps=local_egress.egress_policy.EgressPolicyStore,
        ) as store_constructor:
            self.controller.assistant_lifecycle._activate_assistant_egress(
                "team_1",
                self.spec,
                self.network,
                tuple(sorted(self.spec.allowed_hosts)),
            )

        store_constructor.assert_called_once_with(
            self.policy_root,
            os.getgid(),
            "127.0.0.1,localhost",
            local_egress.ASSISTANT_EGRESS_ALIAS,
            local_egress.ASSISTANT_EGRESS_PORT,
        )

    def test_proxy_profile_rejects_privilege_option_drift(self) -> None:
        drifts = {
            "disabled no-new-privileges": lambda host: host.update(SecurityOpt=["no-new-privileges:false"]),
            "unconfined apparmor": lambda host: host.update(
                SecurityOpt=["no-new-privileges:true", "apparmor=unconfined"]
            ),
            "added capability": lambda host: host.update(CapAdd=["NET_RAW"]),
        }
        for name, drift in drifts.items():
            with self.subTest(name=name):
                self.proxy = _Proxy("local-space")
                self.controller.client.containers.proxy = self.proxy
                drift(self.proxy.attrs["HostConfig"])

                with self.assertRaises(local_app.ApiProblem) as caught:
                    self.controller.assistant_lifecycle._egress_proxy()

                self.assertEqual(caught.exception.code, "egress-proxy-drift")

    def test_startup_reconnects_recreated_proxy_to_owned_egress_team(self) -> None:
        team_id = "team_1"
        self.network.attrs["Labels"] = {
            local_app.MANAGED_LABEL: "1",
            local_app.PROFILE_LABEL: local_app.PROFILE,
            local_app.SPACE_LABEL: self.controller.space_id,
            local_app.KIND_LABEL: "team",
            local_app.TEAM_LABEL: team_id,
            local_app.TEAM_NAME_LABEL: "Team 1",
        }
        assistant = types.SimpleNamespace(labels={local_app.ASSISTANT_LABEL: self.spec.assistant_id})
        self.controller.client.containers.installed = [assistant]
        self.controller.client.networks = types.SimpleNamespace(list=mock.Mock(return_value=[self.network]))
        self.controller.assistant_lifecycle._validate_network = mock.Mock()
        self.controller.assistant_lifecycle._validate_container_profile = mock.Mock(return_value=({}, {}))
        self.controller.assistant_lifecycle._validate_container_egress_environment = mock.Mock(
            return_value=self.spec.allowed_hosts
        )

        self.controller.assistant_lifecycle._reconcile_egress_proxy_attachments()

        self.assertEqual(
            self.proxy.attrs["NetworkSettings"]["Networks"][self.network.name]["Aliases"],
            [local_egress.ASSISTANT_EGRESS_ALIAS],
        )
        self.controller.assistant_lifecycle._validate_container_profile.assert_called_once_with(
            assistant,
            team_id,
            self.spec,
            self.network.name,
        )

    def test_startup_detaches_egress_and_serves_after_assistant_admission_drift(self) -> None:
        team_id = "team_1"
        self.network.attrs["Labels"] = {
            local_app.MANAGED_LABEL: "1",
            local_app.PROFILE_LABEL: local_app.PROFILE,
            local_app.SPACE_LABEL: self.controller.space_id,
            local_app.KIND_LABEL: "team",
            local_app.TEAM_LABEL: team_id,
            local_app.TEAM_NAME_LABEL: "Team 1",
        }
        assistant = types.SimpleNamespace(labels={local_app.ASSISTANT_LABEL: self.spec.assistant_id})
        self.controller.client.containers.installed = [assistant]
        self.controller.client.networks = types.SimpleNamespace(list=mock.Mock(return_value=[self.network]))
        self.controller.assistant_lifecycle._validate_network = mock.Mock()
        self.controller.assistant_lifecycle._validate_container_profile = mock.Mock(return_value=({}, {}))
        self.controller.assistant_lifecycle._validate_container_egress_environment = mock.Mock(
            side_effect=local_app.ApiProblem(409, "policy drift", code="egress-policy-drift")
        )
        self.network.connect(self.proxy, aliases=[local_egress.ASSISTANT_EGRESS_ALIAS])

        self.controller.assistant_lifecycle._reconcile_egress_proxy_attachments()

        self.assertNotIn(self.network.name, self.proxy.attrs["NetworkSettings"]["Networks"])

    def test_startup_serves_when_failed_admission_cannot_detach_egress(self) -> None:
        team_id = "team_1"
        self.network.attrs["Labels"] = {
            local_app.MANAGED_LABEL: "1",
            local_app.PROFILE_LABEL: local_app.PROFILE,
            local_app.SPACE_LABEL: self.controller.space_id,
            local_app.KIND_LABEL: "team",
            local_app.TEAM_LABEL: team_id,
            local_app.TEAM_NAME_LABEL: "Team 1",
        }
        assistant = types.SimpleNamespace(labels={local_app.ASSISTANT_LABEL: self.spec.assistant_id})
        self.controller.client.containers.installed = [assistant]
        self.controller.client.networks = types.SimpleNamespace(list=mock.Mock(return_value=[self.network]))
        self.controller.assistant_lifecycle._validate_network = mock.Mock()
        self.controller.assistant_lifecycle._validate_container_profile = mock.Mock(return_value=({}, {}))
        self.controller.assistant_lifecycle._validate_container_egress_environment = mock.Mock(
            side_effect=local_app.ApiProblem(409, "policy drift", code="egress-policy-drift")
        )
        self.controller.assistant_lifecycle._disconnect_egress_proxy_if_attached = mock.Mock(
            side_effect=local_app.ApiProblem(503, "proxy unavailable", code="egress-proxy-unavailable")
        )

        self.controller.assistant_lifecycle._reconcile_egress_proxy_attachments()

        self.controller.assistant_lifecycle._disconnect_egress_proxy_if_attached.assert_called_once_with(self.network)

    def test_sibling_egress_detection_does_not_admit_a_retiring_manifest(self) -> None:
        assistant = types.SimpleNamespace(labels={local_app.ASSISTANT_LABEL: self.spec.assistant_id})
        self.controller.client.containers.installed = [assistant]
        self.controller.assistant_lifecycle._validate_container_profile = mock.Mock(return_value=({}, {}))
        self.controller.assistant_lifecycle._validate_container_security = lambda *_args: self.fail(
            "teardown must not admit sibling manifests"
        )

        self.assertTrue(self.controller.assistant_lifecycle._team_has_egress_assistant("team_1"))

    def test_valid_request_reconnects_recreated_proxy_without_restarting_team(self) -> None:
        environment = self.controller.assistant_lifecycle._activate_assistant_egress(
            "team_1",
            self.spec,
            self.network,
            tuple(sorted(self.spec.allowed_hosts)),
        )
        self.network.disconnect(self.proxy)

        with mock.patch.object(
            self.controller.assistant_lifecycle,
            "_network",
            return_value=self.network,
        ) as current_network:
            reviewed = self.controller.assistant_lifecycle._validate_container_egress(
                "team_1",
                self.spec,
                self.network.name,
                environment,
                lambda: self.proxy,
            )

        self.assertEqual(reviewed, self.spec.allowed_hosts)
        current_network.assert_called_once_with("team_1")
        self.assertEqual(
            self.proxy.attrs["NetworkSettings"]["Networks"][self.network.name]["Aliases"],
            [local_egress.ASSISTANT_EGRESS_ALIAS],
        )

    def test_request_reconciliation_accepts_a_concurrent_exact_attachment(self) -> None:
        def concurrent_connect(proxy, *, aliases):
            self.network.connect = original_connect
            original_connect(proxy, aliases=aliases)
            raise DockerException("attachment completed concurrently")

        original_connect = self.network.connect
        self.network.connect = concurrent_connect
        with mock.patch.object(
            self.controller.assistant_lifecycle,
            "_network",
            return_value=self.network,
        ):
            self.controller.assistant_lifecycle._reconcile_egress_proxy_attachment(
                "team_1",
                self.network.name,
                self.proxy,
            )

        self.assertEqual(
            self.proxy.attrs["NetworkSettings"]["Networks"][self.network.name]["Aliases"],
            [local_egress.ASSISTANT_EGRESS_ALIAS],
        )

    def test_request_reconciliation_fails_closed_on_attachment_error(self) -> None:
        with (
            mock.patch.object(
                self.controller.assistant_lifecycle,
                "_network",
                return_value=self.network,
            ),
            mock.patch.object(self.network, "connect", side_effect=DockerException("unavailable")),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            self.controller.assistant_lifecycle._reconcile_egress_proxy_attachment(
                "team_1",
                self.network.name,
                self.proxy,
            )

        self.assertEqual(caught.exception.code, "egress-proxy-unavailable")

    def test_request_reconciliation_refuses_a_wrong_existing_alias(self) -> None:
        self.proxy.attrs["NetworkSettings"]["Networks"][self.network.name] = {"Aliases": ["wrong"]}
        with (
            mock.patch.object(self.network, "connect", wraps=self.network.connect) as connect,
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            self.controller.assistant_lifecycle._reconcile_egress_proxy_attachment(
                "team_1",
                self.network.name,
                self.proxy,
            )

        self.assertEqual(caught.exception.code, "egress-proxy-drift")
        connect.assert_not_called()

    def test_last_uninstall_removes_policy_and_detaches_proxy(self) -> None:
        environment = self.controller.assistant_lifecycle._activate_assistant_egress(
            "team_1",
            self.spec,
            self.network,
            tuple(sorted(self.spec.allowed_hosts)),
        )
        token = environment["HTTPS_PROXY"].split("@", 1)[0].rsplit("/", 1)[-1]

        self.controller.assistant_lifecycle._release_assistant_egress("team_1", self.spec.assistant_id, self.network)

        self.assertFalse((self.policy_root / f"{token}.json").exists())
        self.assertEqual(list((self.policy_root / ".tokens").glob("*.token")), [])
        self.assertNotIn(self.network.name, self.proxy.attrs["NetworkSettings"]["Networks"])

    def test_policy_tampering_fails_closed(self) -> None:
        for drift in ("content", "mode", "hardlink", "oversize"):
            with self.subTest(drift=drift):
                environment = self.controller.assistant_lifecycle._activate_assistant_egress(
                    "team_1",
                    self.spec,
                    self.network,
                    tuple(sorted(self.spec.allowed_hosts)),
                )
                token = environment["HTTPS_PROXY"].split("@", 1)[0].rsplit("/", 1)[-1]
                policy = self.policy_root / f"{token}.json"
                if drift == "content":
                    policy.write_text('["evil.example"]', encoding="ascii")
                elif drift == "mode":
                    policy.chmod(0o660)
                elif drift == "hardlink":
                    policy.with_name("policy-hardlink.json").hardlink_to(policy)
                else:
                    policy.write_bytes(b"x" * (local_egress.MAX_EGRESS_POLICY_BYTES + 1))

                with self.assertRaises(local_app.ApiProblem) as caught:
                    self.controller.assistant_lifecycle._validate_egress_policy(
                        "team_1",
                        self.spec,
                        tuple(sorted(self.spec.allowed_hosts)),
                    )

                self.assertEqual(caught.exception.code, "egress-policy-drift")

                if drift == "hardlink":
                    policy.with_name("policy-hardlink.json").unlink()
                self.controller.assistant_lifecycle._write_egress_policy(
                    "team_1",
                    self.spec,
                    tuple(sorted(self.spec.allowed_hosts)),
                )

    def test_policy_adapter_maps_unavailable_storage_and_missing_token(self) -> None:
        unavailable = types.SimpleNamespace(
            token=mock.Mock(side_effect=local_egress.egress_policy.EgressPolicyUnavailableError("unavailable")),
            proxy_environment=mock.Mock(
                side_effect=local_egress.egress_policy.EgressPolicyUnavailableError("unavailable")
            ),
            remove=mock.Mock(side_effect=local_egress.egress_policy.EgressPolicyUnavailableError("unavailable")),
        )
        lifecycle = self.controller.assistant_lifecycle
        for operation in (
            lambda: lifecycle._egress_token("team_1", self.spec.assistant_id, create=False, store=unavailable),
            lambda: lifecycle._proxy_environment("token", unavailable),
            lambda: lifecycle._remove_egress_policy("team_1", self.spec.assistant_id, unavailable),
        ):
            with self.subTest(operation=operation), self.assertRaises(local_app.ApiProblem) as caught:
                operation()
            self.assertEqual(caught.exception.code, "egress-policy-unavailable")

        missing_token = types.SimpleNamespace(token=lambda *_args, **_kwargs: None)
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._write_egress_policy("team_1", self.spec, self.spec.allowed_hosts, missing_token)
        self.assertEqual(caught.exception.code, "egress-policy-unavailable")

    def test_proxy_lookup_connection_and_disconnection_fail_closed(self) -> None:
        lifecycle = self.controller.assistant_lifecycle
        with (
            mock.patch.object(local_egress, "ASSISTANT_EGRESS_CONTAINER", ""),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            lifecycle._egress_proxy()
        self.assertEqual(caught.exception.code, "egress-proxy-unavailable")

        self.controller.client.containers.get = mock.Mock(side_effect=DockerException("unavailable"))
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._egress_proxy()
        self.assertEqual(caught.exception.code, "egress-proxy-unavailable")
        self.controller.client.containers.get = _Containers(self.proxy).get

        broken_proxy = _Proxy("local-space")
        broken_proxy.reload = mock.Mock(side_effect=DockerException("unavailable"))
        with (
            mock.patch.object(self.network, "connect", side_effect=DockerException("unavailable")),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            lifecycle._connect_egress_proxy(self.network, broken_proxy)
        self.assertEqual(caught.exception.code, "egress-proxy-unavailable")

        with (
            mock.patch.object(self.network, "connect"),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            lifecycle._connect_egress_proxy(self.network, self.proxy)
        self.assertEqual(caught.exception.code, "egress-proxy-drift")

        lifecycle._disconnect_egress_proxy(self.network)
        self.network.connect(self.proxy, aliases=[local_egress.ASSISTANT_EGRESS_ALIAS])
        with (
            mock.patch.object(self.network, "disconnect", side_effect=DockerException("unavailable")),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            lifecycle._disconnect_egress_proxy(self.network)
        self.assertEqual(caught.exception.code, "egress-proxy-unavailable")

        with (
            mock.patch.object(self.network, "disconnect"),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            lifecycle._disconnect_egress_proxy(self.network)
        self.assertEqual(caught.exception.code, "egress-proxy-drift")

    def test_network_attachment_inventory_and_identity_errors_are_rejected(self) -> None:
        lifecycle = self.controller.assistant_lifecycle
        wrong_network = types.SimpleNamespace(name="wrong")
        lifecycle._network = mock.Mock(return_value=wrong_network)
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._reconcile_egress_proxy_attachment("team_1", "expected", self.proxy)
        self.assertEqual(caught.exception.code, "ownership-conflict")

        failing_network = mock.Mock()
        failing_network.reload.side_effect = DockerException("unavailable")
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._disconnect_egress_proxy_if_attached(failing_network)
        self.assertEqual(caught.exception.code, "docker-unavailable")

        invalid_inventory = mock.Mock(attrs={"Containers": ["invalid"]})
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._disconnect_egress_proxy_if_attached(invalid_inventory)
        self.assertEqual(caught.exception.code, "ownership-conflict")

        self.controller.client.networks = types.SimpleNamespace(
            list=mock.Mock(side_effect=DockerException("unavailable"))
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._managed_team_networks()
        self.assertEqual(caught.exception.code, "docker-unavailable")

    def test_team_egress_inventory_rejects_docker_registry_and_duplicate_drift(self) -> None:
        lifecycle = self.controller.assistant_lifecycle
        self.controller.client.containers.list = mock.Mock(side_effect=DockerException("unavailable"))
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._team_requires_egress_proxy("team_1", self.network)
        self.assertEqual(caught.exception.code, "docker-unavailable")
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._team_has_egress_assistant("team_1")
        self.assertEqual(caught.exception.code, "docker-unavailable")

        assistant = types.SimpleNamespace(labels={local_app.ASSISTANT_LABEL: self.spec.assistant_id})
        self.controller.client.containers.list.side_effect = None
        self.controller.client.containers.list.return_value = [assistant, assistant]
        lifecycle._validate_container_profile = mock.Mock(return_value=({}, {}))
        lifecycle._validate_container_egress_environment = mock.Mock(return_value=())
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._team_requires_egress_proxy("team_1", self.network)
        self.assertEqual(caught.exception.code, "assistant-registry-drift")

        direct_spec = types.SimpleNamespace(assistant_id="direct", allowed_hosts=())
        direct = types.SimpleNamespace(labels={local_app.ASSISTANT_LABEL: direct_spec.assistant_id})
        lifecycle.registry = TestPublicationRegistry(
            {direct_spec.assistant_id: direct_spec, self.spec.assistant_id: self.spec}
        )
        self.controller.client.containers.list.return_value = [direct, assistant]
        self.assertTrue(lifecycle._team_has_egress_assistant("team_1"))

        lifecycle.registry = TestPublicationRegistry({})
        self.controller.client.containers.list.return_value = [assistant]
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._team_has_egress_assistant("team_1")
        self.assertEqual(caught.exception.code, "assistant-registry-drift")

        self.controller.client.containers.list.return_value = [assistant]
        self.assertFalse(lifecycle._team_has_egress_assistant("team_1", excluding=self.spec.assistant_id))

    def test_reconciliation_release_and_activation_cover_no_egress_paths(self) -> None:
        lifecycle = self.controller.assistant_lifecycle
        network = types.SimpleNamespace(
            attrs={"Labels": {local_app.TEAM_LABEL: "INVALID"}},
        )
        lifecycle._managed_team_networks = mock.Mock(return_value=[network])
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._reconcile_egress_proxy_attachments()
        self.assertEqual(caught.exception.code, "ownership-conflict")

        valid_network = types.SimpleNamespace(attrs={"Labels": {local_app.TEAM_LABEL: "team_1"}})
        lifecycle._managed_team_networks.return_value = [valid_network, valid_network]
        lifecycle._validate_network = mock.Mock()
        lifecycle._team_requires_egress_proxy = mock.Mock(return_value=False)
        lifecycle._disconnect_egress_proxy_if_attached = mock.Mock()
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._reconcile_egress_proxy_attachments()
        self.assertEqual(caught.exception.code, "ownership-conflict")
        lifecycle._disconnect_egress_proxy_if_attached.assert_called_once_with(valid_network)

        lifecycle._remove_egress_policy = mock.Mock()
        lifecycle._disconnect_egress_proxy = mock.Mock()
        lifecycle._release_assistant_egress(
            "team_1",
            self.spec.assistant_id,
            self.network,
            remaining_egress=True,
        )
        lifecycle._disconnect_egress_proxy.assert_not_called()
        lifecycle._remove_assistant_policy_if_needed("team_1", self.spec.assistant_id, self.spec)
        lifecycle._remove_egress_policy.assert_called()

        self.assertEqual(lifecycle._activate_assistant_egress("team_1", self.spec, self.network, ()), {})
        lifecycle._write_egress_policy = mock.Mock(return_value={"HTTPS_PROXY": "proxy"})
        lifecycle._connect_egress_proxy = mock.Mock(
            side_effect=local_app.ApiProblem(503, "unavailable", code="egress-proxy-unavailable")
        )
        with self.assertRaises(local_app.ApiProblem):
            lifecycle._activate_assistant_egress(
                "team_1",
                self.spec,
                self.network,
                self.spec.allowed_hosts,
                types.SimpleNamespace(),
            )

    def test_network_validation_and_optional_lookup_fail_closed(self) -> None:
        lifecycle = self.controller.assistant_lifecycle
        invalid = types.SimpleNamespace(reload=mock.Mock(), attrs={})
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._validate_network(invalid, "team_1")
        self.assertEqual(caught.exception.code, "ownership-conflict")

        labels = lifecycle._base_labels("team_1", "team")
        labels[local_app.TEAM_NAME_LABEL] = ""
        invalid_name = types.SimpleNamespace(
            reload=mock.Mock(),
            attrs={
                "Labels": labels,
                "Name": lifecycle._network_name("team_1"),
                "Driver": "bridge",
                "Internal": True,
                "Attachable": False,
            },
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._validate_network(invalid_name, "team_1")
        self.assertEqual(caught.exception.code, "ownership-conflict")

        self.controller.client.networks = types.SimpleNamespace(
            get=mock.Mock(side_effect=local_egress.NotFound("missing"))
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._network("team_1")
        self.assertEqual(caught.exception.code, "team-not-found")
        self.assertIsNone(lifecycle._network("team_1", required=False))


if __name__ == "__main__":
    unittest.main()
