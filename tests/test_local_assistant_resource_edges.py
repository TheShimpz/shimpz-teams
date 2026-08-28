from __future__ import annotations

import types
import unittest
from unittest import mock

from docker.errors import DockerException, ImageNotFound, NotFound

from local.assistant import isolation, resources
from local.errors import ApiProblemError


class LocalAssistantResourceEdgeTests(unittest.TestCase):
    @staticmethod
    def _controller() -> types.SimpleNamespace:
        return types.SimpleNamespace(
            space_id="local",
            _network=mock.Mock(),
            _assistant_filters=lambda _team_id: {},
            _container_name=lambda team_id, assistant_id: f"{team_id}-{assistant_id}",
            _base_labels=lambda team_id, kind: {"team": team_id, "kind": kind},
            _labels_include=lambda actual, expected: all(actual.get(key) == value for key, value in expected.items()),
            registry=types.SimpleNamespace(get=mock.Mock()),
            client=types.SimpleNamespace(
                containers=types.SimpleNamespace(get=mock.Mock(), list=mock.Mock()),
                images=types.SimpleNamespace(get=mock.Mock(), pull=mock.Mock()),
            ),
        )

    def test_container_lookup_distinguishes_required_and_optional_absence(self) -> None:
        controller = self._controller()
        controller.client.containers.get.side_effect = NotFound("missing")
        with self.assertRaisesRegex(ApiProblemError, "not installed"):
            resources._assistant_container(controller, "team_1", "helper")
        self.assertIsNone(resources._assistant_container(controller, "team_1", "helper", required=False))
        container = object()
        controller.client.containers.get.side_effect = None
        controller.client.containers.get.return_value = container
        self.assertIs(resources._assistant_container(controller, "team_1", "helper"), container)

    def test_assistant_inventory_rejects_docker_registry_identity_and_duplicate_drift(self) -> None:
        controller = self._controller()
        controller.client.containers.list.side_effect = DockerException("unavailable")
        with self.assertRaisesRegex(ApiProblemError, "Docker is unavailable"):
            resources._assistant_ids(controller, "team_1")

        controller.client.containers.list.side_effect = None
        missing = types.SimpleNamespace(labels={}, name="unknown", status="running")
        controller.client.containers.list.return_value = [missing]
        controller.registry.get.return_value = None
        with self.assertRaisesRegex(ApiProblemError, "no longer allowlisted"):
            resources._assistant_ids(controller, "team_1")

        spec = types.SimpleNamespace(assistant_id="helper")
        invalid = types.SimpleNamespace(
            labels={resources.ASSISTANT_LABEL: "helper"},
            name="wrong",
            status="running",
        )
        controller.client.containers.list.return_value = [invalid]
        controller.registry.get.return_value = spec
        with self.assertRaisesRegex(ApiProblemError, "isolation profile"):
            resources._assistant_ids(controller, "team_1")

        labels = {"team": "team_1", "kind": "assistant", resources.ASSISTANT_LABEL: "helper"}
        stopped = types.SimpleNamespace(labels=labels, name="team_1-helper", status="exited")
        controller.client.containers.list.return_value = [stopped]
        self.assertEqual(resources._assistant_ids(controller, "team_1", running_only=True), ())

        duplicate = types.SimpleNamespace(labels=labels, name="team_1-helper", status="running")
        controller.client.containers.list.return_value = [duplicate, duplicate]
        with self.assertRaisesRegex(ApiProblemError, "isolation profile"):
            resources._assistant_ids(controller, "team_1")

    def test_resolution_and_image_trust_fail_closed(self) -> None:
        controller = self._controller()
        controller.registry.get.return_value = None
        with self.assertRaisesRegex(ApiProblemError, "not allowlisted"):
            resources._resolve(controller, "team_1", "helper")

        spec = types.SimpleNamespace(
            image="registry/image@sha256:digest",
            required_image_labels=(("assistant", "helper"),),
            provenance="published",
        )
        image = types.SimpleNamespace(
            attrs={
                "Config": {"Labels": {"assistant": "helper"}},
                "RepoDigests": [spec.image],
            },
            reload=mock.Mock(),
        )
        controller._image_labels_valid = resources._image_labels_valid
        controller.client.images.get.side_effect = ImageNotFound("missing")
        controller.client.images.pull.return_value = image
        self.assertIs(resources._trusted_image(controller, spec), image)

        controller.client.images.pull.side_effect = DockerException("pull failed")
        with self.assertRaisesRegex(ApiProblemError, "could not be pulled"):
            resources._trusted_image(controller, spec)

        controller.client.images.get.side_effect = DockerException("unavailable")
        with self.assertRaisesRegex(ApiProblemError, "Docker is unavailable"):
            resources._trusted_image(controller, spec)

    def test_staged_image_never_pulls_and_requires_exact_local_identity(self) -> None:
        controller = self._controller()
        controller._image_labels_valid = resources._image_labels_valid
        spec = types.SimpleNamespace(
            image="sha256:" + ("a" * 64),
            platform="linux/amd64",
            provenance="local",
            required_image_labels=(("local", "assistant-v1"),),
        )
        image = types.SimpleNamespace(
            id=spec.image,
            attrs={
                "Id": spec.image,
                "Architecture": "amd64",
                "RepoDigests": [],
                "RepoTags": [],
                "Config": {"Labels": {"local": "assistant-v1"}},
            },
            reload=mock.Mock(),
        )
        controller.client.images.get.return_value = image

        self.assertIs(resources._staged_image(controller, spec), image)
        controller.client.images.pull.assert_not_called()

        image.attrs["RepoTags"] = ["attacker/latest"]
        with self.assertRaisesRegex(ApiProblemError, "does not match"):
            resources._staged_image(controller, spec)
        controller.client.images.get.side_effect = ImageNotFound("missing")
        with self.assertRaisesRegex(ApiProblemError, "no longer available"):
            resources._staged_image(controller, spec)
        controller.client.images.pull.assert_not_called()

    def test_egress_contract_rejects_invalid_manifest_and_environment(self) -> None:
        controller = types.SimpleNamespace(
            _validate_egress_policy=mock.Mock(return_value={}),
        )
        invalid_spec = types.SimpleNamespace(allowed_hosts=("INVALID",))
        with self.assertRaisesRegex(ApiProblemError, "allowed_hosts contract is invalid"):
            resources._validate_container_egress_environment(controller, "team_1", invalid_spec, {})

        direct_spec = types.SimpleNamespace(allowed_hosts=())
        with self.assertRaisesRegex(ApiProblemError, "isolation profile"):
            resources._validate_container_egress_environment(
                controller,
                "team_1",
                direct_spec,
                {"HTTPS_PROXY": "http://attacker"},
            )

    def test_local_isolation_accepts_only_the_bound_image_id(self) -> None:
        image_id = "sha256:" + ("a" * 64)
        local = isolation.ImageIdentity(image_id, "local")

        self.assertTrue(isolation._installed_image_valid(image_id, local))
        self.assertFalse(isolation._installed_image_valid("sha256:" + ("b" * 64), local))
        self.assertFalse(isolation._installed_image_valid(image_id, isolation.ImageIdentity(image_id, "unknown")))


if __name__ == "__main__":
    unittest.main()
