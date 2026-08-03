from __future__ import annotations

import sys
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LocalContractCase

from local import app as local_app
from local.install import runtime as local_runtime

LOOKUP_INPUT = {"page": 1, "per_page": 25}
LOOKUP_RESULT = {
    "zones": [],
    "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0},
}
DNS_INPUT = {"zone_id": "a" * 32, "page": 1, "per_page": 25}
DNS_RESULT = {
    "records": [],
    "pagination": {"page": 1, "per_page": 25, "count": 0, "total_count": 0, "total_pages": 0},
}
TEST_SECRET_VALUES = {
    "service-token": "service-test-credential-123456789",
    "client-key": "client-key-test-credential-123456789",
    "client-secret": "client-secret-test-credential-123456789",
    "session-token": "session-token-test-credential-123456789",
    "session-secret": "session-secret-test-credential-123456789",
}
TEST_ACCOUNT_ACCESS_TOKEN = "-".join(("oauth", "access", "test", "token", "123456789"))
TEST_ACCOUNT_REFRESH_TOKEN = "-".join(("oauth", "refresh", "test", "token", "123456789"))
CURRENT_ASSISTANT_IMAGE = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "b" * 64
OUTDATED_ASSISTANT_IMAGE = "ghcr.io/theshimpz/shimpz-assistant@sha256:" + "a" * 64


class LocalLifecycleTeardownTests(LocalContractCase):
    def test_manifest_mismatch_removes_stopped_container_without_activating_egress(self) -> None:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.cpuset_cpus = "0"
        spec = SimpleNamespace(
            assistant_id="shimpz-cloudflare",
            image=CURRENT_ASSISTANT_IMAGE,
            allowed_hosts=("api.open-meteo.com",),
        )
        image = SimpleNamespace(id="sha256:" + "d" * 64)
        container = SimpleNamespace(
            id="assistant-generation",
            attrs={"Image": image.id},
            reload=lambda: events.append("reload"),
            start=lambda: events.append("start"),
            remove=lambda *, force: events.append(("remove", force)),
        )
        controller.client = SimpleNamespace(containers=SimpleNamespace(create=lambda **_kwargs: container))
        controller._wire_collaborators()
        network = SimpleNamespace(name=controller.assistant_lifecycle._network_name("team_1"))
        controller.assistant_lifecycle._egress_token = lambda *_args, **_kwargs: "a" * 32
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = lambda *_args: (_ for _ in ()).throw(
            local_app.ApiProblem(
                HTTPStatus.CONFLICT,
                "installed Assistant manifest failed its reviewed contract",
                code="assistant-manifest-invalid",
            )
        )
        controller.assistant_lifecycle._activate_assistant_egress = lambda *_args: events.append("activate-egress")
        controller.assistant_lifecycle._release_assistant_egress = lambda *_args: events.append("release-egress")

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle._create_assistant_container("team_1", spec, network, image)

        self.assertEqual(caught.exception.code, "assistant-manifest-invalid")
        self.assertNotIn("start", events)
        self.assertNotIn("activate-egress", events)
        self.assertEqual(events, ["reload", ("remove", True), "release-egress"])

    def test_failed_install_removal_still_revokes_egress_and_reports_incomplete_rollback(self) -> None:
        events: list[object] = []
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller.cpuset_cpus = "0"
        spec = SimpleNamespace(
            assistant_id="shimpz-cloudflare",
            image=CURRENT_ASSISTANT_IMAGE,
            allowed_hosts=("api.open-meteo.com",),
        )
        image = SimpleNamespace(id="sha256:" + "d" * 64)

        class Container:
            id = "assistant-generation"

            def __init__(self) -> None:
                self.attrs = {"Image": image.id, "State": {"Running": False}}

            def reload(self) -> None:
                events.append("reload")

            def remove(self, *, force: bool) -> None:
                events.append(("remove", force))
                raise local_app.DockerException("ambiguous removal")

            def stop(self, *, timeout: int) -> None:
                events.append(("stop", timeout))

            def kill(self) -> None:
                self.fail("a proved stopped container must not be killed")

        container = Container()
        controller.client = SimpleNamespace(containers=SimpleNamespace(create=lambda **_kwargs: container))
        controller._wire_collaborators()
        network = SimpleNamespace(name=controller.assistant_lifecycle._network_name("team_1"))
        controller.assistant_lifecycle._egress_token = lambda *_args, **_kwargs: "a" * 32
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = lambda *_args: (_ for _ in ()).throw(
            local_app.ApiProblem(
                HTTPStatus.CONFLICT,
                "installed Assistant manifest failed its reviewed contract",
                code="assistant-manifest-invalid",
            )
        )
        controller.assistant_lifecycle._activate_assistant_egress = lambda *_args: events.append("activate-egress")
        controller.assistant_lifecycle._release_assistant_egress = lambda *_args: events.append("release-egress")

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle._create_assistant_container("team_1", spec, network, image)

        self.assertEqual(caught.exception.code, "assistant-install-rollback-incomplete")
        self.assertNotIn("activate-egress", events)
        self.assertEqual(
            events,
            ["reload", ("remove", True), ("stop", 3), "reload", "release-egress"],
        )

    def test_uninstall_removes_an_outdated_release_after_current_contract_admission(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        controller.assistant_integrations.put(
            "team_1",
            "shimpz-cloudflare",
            "cloudflare",
            "cloudflare",
            ("zone.read",),
            SimpleNamespace(
                access_token=TEST_ACCOUNT_ACCESS_TOKEN,
                refresh_token=TEST_ACCOUNT_REFRESH_TOKEN,
                scopes=("zone.read",),
                expires_in=3600,
            ),
        )

        result = controller.assistant_lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")

        self.assertEqual(result, {"assistant": "shimpz-cloudflare", "uninstalled": True})
        self.assertEqual(
            events,
            ["reload", ("remove", True), ("residue-add", "sha256:" + "a" * 64), "residue-sweep"],
        )
        self.assertFalse(controller.assistant_integrations.delete_assistant("team_1", "shimpz-cloudflare"))

    def test_uninstall_removes_an_isolated_container_with_an_invalid_manifest(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        controller.assistant_lifecycle._admit_assistant_allowed_hosts = lambda *_args: self.fail(
            "teardown must not admit a retiring Assistant manifest"
        )

        result = controller.assistant_lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")

        self.assertEqual(result, {"assistant": "shimpz-cloudflare", "uninstalled": True})
        self.assertEqual(
            events,
            ["reload", ("remove", True), ("residue-add", "sha256:" + "a" * 64), "residue-sweep"],
        )

    def test_uninstall_does_not_strand_an_owned_container_without_a_cleanup_image_id(self) -> None:
        controller, container, events = self._lifecycle_controller()
        container.attrs["Image"] = "not-a-docker-image-id"

        result = controller.assistant_lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")

        self.assertEqual(result, {"assistant": "shimpz-cloudflare", "uninstalled": True})
        self.assertEqual(events, ["reload", ("remove", True), "residue-sweep"])

    def test_uninstall_sweeps_queued_residue_after_retrying_a_partial_teardown(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        controller.assistant_lifecycle._assistant_container = lambda *_args, **_kwargs: None
        controller.assistant_lifecycle._egress_token = lambda *_args, **_kwargs: None

        result = controller.assistant_lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")

        self.assertEqual(result, {"assistant": "shimpz-cloudflare", "uninstalled": False})
        self.assertEqual(events, ["residue-sweep"])

    def test_team_teardown_does_not_require_a_retiring_egress_policy(self) -> None:
        controller, container, events = self._lifecycle_controller()
        controller.registry["shimpz-cloudflare"].allowed_hosts = ("api.example.com",)
        controller.assistant_lifecycle._validate_container_isolation = lambda *_args: self.fail(
            "teardown must not admit a retiring egress policy"
        )
        network = controller.assistant_lifecycle._network("team_1")

        controller._validate_destroy_containers([container], "team_1", network)

        self.assertEqual(events, ["reload"])

    def test_install_rejects_security_drift_without_resolving_or_removing(self) -> None:
        controller, container, events = self._lifecycle_controller()
        container.attrs["HostConfig"]["Privileged"] = True
        controller.assistant_lifecycle._trusted_image = lambda _spec: self.fail(
            "security drift reached image resolution"
        )

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle.install_assistant("team_1", "shimpz-cloudflare")

        self.assertEqual(
            (caught.exception.status, caught.exception.code),
            (HTTPStatus.CONFLICT, "assistant-isolation-drift"),
        )
        self.assertEqual(events, ["reload"])

    def test_container_profile_rejects_privilege_and_kernel_limit_drift(self) -> None:
        drifts = {
            "disabled no-new-privileges": lambda host: host.update(SecurityOpt=["no-new-privileges:false"]),
            "unconfined apparmor": lambda host: host.update(
                SecurityOpt=["no-new-privileges:true", "apparmor=unconfined"]
            ),
            "added capability": lambda host: host.update(CapAdd=["NET_RAW"]),
            "raised file limit": lambda host: host.update(Ulimits=[{"Name": "nofile", "Soft": 65536, "Hard": 65536}]),
            "kernel sysctl": lambda host: host.update(Sysctls={"net.ipv4.ip_forward": "1"}),
        }
        for name, drift in drifts.items():
            with self.subTest(name=name):
                controller, container, events = self._lifecycle_controller()
                drift(container.attrs["HostConfig"])

                with self.assertRaises(local_app.ApiProblem) as caught:
                    controller.list_assistants("team_1")

                self.assertEqual(caught.exception.code, "assistant-isolation-drift")
                self.assertEqual(events, ["reload"])

    def test_uninstall_never_removes_a_container_with_wrong_ownership(self) -> None:
        controller, container, events = self._lifecycle_controller()
        container.labels[local_app.SPACE_LABEL] = "other-space"

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")

        self.assertEqual(caught.exception.code, "assistant-isolation-drift")
        self.assertEqual(events, ["reload"])

    def test_list_marks_only_artifact_drift_outdated_and_rejects_security_drift(self) -> None:
        controller, container, events = self._lifecycle_controller()

        self.assertEqual(
            controller.list_assistants("team_1"),
            {"assistants": [{"assistant": "shimpz-cloudflare", "status": "outdated"}]},
        )
        with self.assertRaises(local_app.ApiProblem) as update_required:
            controller.assistant_lifecycle._validate_container(
                container,
                "team_1",
                controller.registry["shimpz-cloudflare"],
                controller.assistant_lifecycle._network_name("team_1"),
            )
        self.assertEqual(update_required.exception.code, "assistant-update-required")
        self.assertEqual(update_required.exception.message, "the installed Assistant must be updated")
        container.attrs["HostConfig"]["ReadonlyRootfs"] = False
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.list_assistants("team_1")

        self.assertEqual(caught.exception.code, "assistant-isolation-drift")
        self.assertEqual(events, ["reload", "reload", "reload"])

    def test_uninstall_does_not_require_a_retiring_egress_policy(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        controller.registry["shimpz-cloudflare"].allowed_hosts = ("api.example.com",)
        controller.assistant_lifecycle._validate_container_isolation = lambda *_args: self.fail(
            "teardown must not admit a retiring egress policy"
        )
        controller.assistant_lifecycle._team_has_egress_assistant = mock.Mock(return_value=False)
        controller.assistant_lifecycle._release_assistant_egress = lambda *_args, **_kwargs: events.append(
            "release-egress"
        )

        result = controller.assistant_lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")

        self.assertEqual(result, {"assistant": "shimpz-cloudflare", "uninstalled": True})
        self.assertEqual(
            events,
            [
                "reload",
                ("remove", True),
                ("residue-add", "sha256:" + "a" * 64),
                "release-egress",
                "residue-sweep",
            ],
        )

    def test_list_marks_an_invalid_retired_manifest_for_removal(self) -> None:
        controller, container, _events = self._lifecycle_controller()
        container.labels[local_app.IMAGE_LABEL] = CURRENT_ASSISTANT_IMAGE
        container.attrs["Config"]["Image"] = CURRENT_ASSISTANT_IMAGE

        def reject(*_args):
            raise local_app.ApiProblem(
                HTTPStatus.CONFLICT,
                "installed Assistant manifest failed its reviewed contract",
                code="assistant-manifest-invalid",
            )

        controller.assistant_lifecycle._admit_assistant_allowed_hosts = reject

        self.assertEqual(
            controller.list_assistants("team_1"),
            {"assistants": [{"assistant": "shimpz-cloudflare", "status": "invalid"}]},
        )

    def test_outdated_release_lineage_is_closed_before_lifecycle_actions(self) -> None:
        self.assertTrue(local_runtime.is_digest_ref(OUTDATED_ASSISTANT_IMAGE))
        self.assertFalse(local_runtime.is_digest_ref("ghcr.io/theshimpz/shimpz-assistant@sha256:" + "0" * 64))
        self.assertFalse(local_runtime.is_digest_ref("ghcr.io/theshimpz/shimpz-assistant:latest"))

        for drift in ("missing-label", "image-label-mismatch", "foreign-repository", "wrong-name"):
            with self.subTest(drift=drift):
                controller, container, events = self._lifecycle_controller()
                if drift == "missing-label":
                    container.labels.pop(local_app.IMAGE_LABEL)
                elif drift == "image-label-mismatch":
                    container.attrs["Config"]["Image"] = CURRENT_ASSISTANT_IMAGE
                elif drift == "foreign-repository":
                    foreign = "evil.example/shimpz-assistant@sha256:" + "c" * 64
                    container.labels[local_app.IMAGE_LABEL] = foreign
                    container.attrs["Config"]["Image"] = foreign
                else:
                    container.name = "foreign-container"

                with self.assertRaises(local_app.ApiProblem) as caught:
                    controller.list_assistants("team_1")

                self.assertEqual(caught.exception.code, "assistant-isolation-drift")
                self.assertEqual(events, ["reload"])


if __name__ == "__main__":
    unittest.main()
