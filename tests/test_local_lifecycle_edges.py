from __future__ import annotations

import types
import unittest
from http import HTTPStatus
from unittest import mock

from docker.errors import DockerException
from local_controller_harness import LocalContractCase, TestPublicationRegistry

from inference import client as brain_runtime_client
from inference import config as inference_config
from local import app as local_app
from local import lifecycle as local_lifecycle
from storage import files as team_storage


class LocalLifecycleEdgeTests(LocalContractCase):
    def test_team_inventory_and_conversation_fail_closed(self) -> None:
        subject = types.SimpleNamespace(
            client=types.SimpleNamespace(
                containers=types.SimpleNamespace(list=mock.Mock(side_effect=DockerException("unavailable")))
            ),
            assistant_lifecycle=types.SimpleNamespace(_assistant_filters=lambda _team_id: {}),
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._team_assistant_containers(subject, "team_1")
        self.assertEqual(caught.exception.code, "docker-unavailable")

        container = types.SimpleNamespace(labels={local_app.ASSISTANT_LABEL: "assistant"})
        subject.registry = TestPublicationRegistry({})
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._validate_destroy_containers(subject, [container], "team_1", object())
        self.assertEqual(caught.exception.code, "ownership-conflict")

        subject.registry = TestPublicationRegistry({"assistant": object()})
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._validate_destroy_containers(subject, [container], "team_1", None)
        self.assertEqual(caught.exception.code, "ownership-conflict")

        self.assertIsNone(local_lifecycle._delete_team_conversation(subject, "team_1", None))

        subject.space_id = "local-space"
        subject.brain_runtime = types.SimpleNamespace(
            delete_thread=mock.Mock(side_effect=brain_runtime_client.BrainRuntimeError("unavailable"))
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._delete_team_conversation(
                subject,
                "team_1",
                types.SimpleNamespace(id="a" * 64),
            )
        self.assertEqual(caught.exception.code, "brain-runtime-failed")

    def test_assistant_removal_rejects_registry_and_docker_drift(self) -> None:
        container = types.SimpleNamespace(
            id="container",
            labels={local_app.ASSISTANT_LABEL: "assistant"},
            remove=mock.Mock(),
        )
        lifecycle = types.SimpleNamespace(
            _retired_image_id=lambda _container: None,
            _blocked_action_workloads=set(),
            _remove_assistant_policy_if_needed=mock.Mock(),
            _queue_residue=mock.Mock(),
            sweep_residues=mock.Mock(),
        )
        subject = types.SimpleNamespace(
            registry=TestPublicationRegistry({}),
            assistant_lifecycle=lifecycle,
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._remove_team_assistants(subject, "team_1", [container])
        self.assertEqual(caught.exception.code, "ownership-conflict")

        subject.registry = TestPublicationRegistry({"assistant": object()})
        container.remove.side_effect = DockerException("unavailable")
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._remove_team_assistants(subject, "team_1", [container])
        self.assertEqual(caught.exception.code, "docker-remove-failed")

        container.remove.side_effect = None
        self.assertEqual(
            local_lifecycle._remove_team_assistants(subject, "team_1", [container]),
            1,
        )
        lifecycle._queue_residue.assert_not_called()

    def test_binding_only_assistants_are_removed_for_the_exact_team(self) -> None:
        own_spec = object()
        registry = TestPublicationRegistry({"own": own_spec, "other": object()})
        registry.identities = lambda: {("other_team", "other"), ("team_1", "own")}
        lifecycle = types.SimpleNamespace(
            _blocked_action_workloads=set(),
            _remove_assistant_policy_if_needed=mock.Mock(),
            sweep_residues=mock.Mock(),
        )
        subject = types.SimpleNamespace(registry=registry, assistant_lifecycle=lifecycle)

        self.assertEqual(local_lifecycle._remove_team_assistants(subject, "team_1", []), 0)

        lifecycle._remove_assistant_policy_if_needed.assert_called_once_with("team_1", "own", own_spec)
        self.assertIsNone(registry.get("team_1", "own"))
        self.assertIsNotNone(registry.get("other_team", "other"))

        missing = TestPublicationRegistry({})
        missing.identities = lambda: {("team_1", "missing")}
        subject.registry = missing
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._remove_team_assistants(subject, "team_1", [])
        self.assertEqual(caught.exception.code, "ownership-conflict")

    def test_persistence_and_network_failures_are_mapped(self) -> None:
        unavailable_storage = types.SimpleNamespace(
            storage=types.SimpleNamespace(destroy=mock.Mock(side_effect=team_storage.StorageError("unavailable"))),
            inference_store=types.SimpleNamespace(delete=mock.Mock()),
            _raise_storage_problem=mock.Mock(
                side_effect=local_app.ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "storage unavailable",
                    code="storage-unavailable",
                )
            ),
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._delete_team_persistence(unavailable_storage, "team_1")
        self.assertEqual(caught.exception.code, "storage-unavailable")

        unavailable_inference = types.SimpleNamespace(
            storage=types.SimpleNamespace(destroy=lambda _team_id: True),
            inference_store=types.SimpleNamespace(
                delete=mock.Mock(side_effect=inference_config.InferenceConfigError("unavailable"))
            ),
            _raise_inference_problem=mock.Mock(
                side_effect=local_app.ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "inference unavailable",
                    code="inference-config-unavailable",
                )
            ),
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._delete_team_persistence(unavailable_inference, "team_1")
        self.assertEqual(caught.exception.code, "inference-config-unavailable")

        subject = types.SimpleNamespace(
            assistant_lifecycle=types.SimpleNamespace(_disconnect_egress_proxy_if_attached=mock.Mock())
        )
        self.assertFalse(local_lifecycle._remove_team_network(subject, None))
        network = types.SimpleNamespace(remove=mock.Mock(side_effect=DockerException("unavailable")))
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._remove_team_network(subject, network)
        self.assertEqual(caught.exception.code, "docker-remove-failed")

    def test_destroy_requires_lock_and_complete_teardown_proof(self) -> None:
        controller, _container, _events = self._lifecycle_controller()
        chat_lock = mock.Mock()
        chat_lock.acquire.return_value = False
        controller.chat_turn_service._chat_lock = lambda _team_id: chat_lock
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.destroy_team("team_1")
        self.assertEqual(caught.exception.code, "chat-active")
        chat_lock.release.assert_not_called()

        controller, _container, _events = self._lifecycle_controller()
        network = types.SimpleNamespace(id="a" * 64, name="team-network")
        controller.assistant_lifecycle._network = lambda _team_id, *, required=False: network
        controller._team_assistant_containers = lambda _team_id: []
        controller._validate_destroy_containers = mock.Mock()
        controller._delete_team_conversation = mock.Mock()
        controller._remove_team_assistants = mock.Mock(return_value=0)
        controller._delete_team_persistence = mock.Mock(return_value=False)
        controller._remove_team_network = mock.Mock(return_value=True)
        controller._delete_team_private_state = mock.Mock()
        controller._clear_team_runtime_state = mock.Mock()
        with (
            mock.patch.object(
                local_lifecycle,
                "_TEAM_RESIDUE_ABSENCE",
                local_lifecycle._TEAM_RESIDUE_ABSENCE | {"unexpected"},
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            controller.destroy_team("team_1")
        self.assertEqual(caught.exception.code, "teardown-incomplete")

    def test_reset_inventory_rejects_invalid_owned_resources(self) -> None:
        invalid_container = types.SimpleNamespace(
            name="wrong",
            attrs={},
            reload=mock.Mock(),
        )
        subject = types.SimpleNamespace(
            assistant_lifecycle=types.SimpleNamespace(
                _labels_include=mock.Mock(return_value=False),
                _base_labels=mock.Mock(return_value={}),
                _container_name=mock.Mock(return_value="expected"),
            )
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._validate_reset_container(subject, invalid_container)
        self.assertEqual(caught.exception.code, "ownership-conflict")

        network = types.SimpleNamespace(attrs={"Labels": {local_app.TEAM_LABEL: 7}})
        subject.registry = TestPublicationRegistry({})
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_lifecycle._reset_assistant_identities(subject, [], [network])
        self.assertEqual(caught.exception.code, "ownership-conflict")

    def test_space_resource_removal_handles_images_without_cleanup_identity(self) -> None:
        events: list[object] = []
        container = types.SimpleNamespace(
            id="container",
            attrs={
                "Config": {
                    "Labels": {
                        local_app.TEAM_LABEL: "team_1",
                        local_app.ASSISTANT_LABEL: "assistant",
                    }
                }
            },
            remove=lambda *, force: events.append(("remove", force)),
        )
        network = types.SimpleNamespace(
            attrs={"Labels": {local_app.TEAM_LABEL: "team_1"}},
            remove=lambda: events.append("network-remove"),
        )
        subject = types.SimpleNamespace(
            chat_turn_service=types.SimpleNamespace(
                _delete_all_integration_state=lambda: events.append("integration-delete"),
                _delete_all_stored_input_state=lambda: events.append("stored-input-delete"),
            ),
            _delete_team_conversation=lambda *_args: events.append("conversation-delete"),
            assistant_lifecycle=types.SimpleNamespace(
                _retired_image_id=lambda _container: None,
                _blocked_action_workloads=set(),
                _queue_residue=mock.Mock(),
                _remove_egress_policy=lambda *_args: events.append("policy-delete"),
                sweep_residues=lambda: events.append("residue-sweep"),
                _disconnect_egress_proxy_if_attached=lambda _network: events.append("proxy-disconnect"),
            ),
            registry=TestPublicationRegistry({}),
            storage=types.SimpleNamespace(destroy_all=lambda: True),
            inference_store=types.SimpleNamespace(delete=lambda _team_id: events.append("inference-delete")),
            _clear_team_runtime_state=lambda _team_id: events.append("runtime-clear"),
        )

        storage_removed, absent = local_lifecycle._remove_space_resources(
            subject,
            [container],
            [network],
            {("team_1", "assistant")},
        )

        self.assertTrue(storage_removed)
        self.assertIn("runtime_state", absent)
        subject.assistant_lifecycle._queue_residue.assert_not_called()

    def test_reset_maps_each_failure_and_requires_complete_proof(self) -> None:
        controller, _container, _events = self._lifecycle_controller()
        controller.chat_turn_service._clear_chat_continuations = mock.Mock()
        controller.chat_turn_service.human_challenges.cancel_all = mock.Mock()
        cases = (
            (
                local_app.ApiProblem(
                    HTTPStatus.CONFLICT,
                    "ownership conflict",
                    code="ownership-conflict",
                ),
                "ownership-conflict",
            ),
            (team_storage.StorageError("unavailable"), "storage-safety-failed"),
            (
                inference_config.InferenceConfigError("unavailable"),
                "inference-store-failed",
            ),
            (DockerException("unavailable"), "docker-reset-failed"),
        )
        for failure, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                controller._reset_inventory = mock.Mock(side_effect=failure)
                with self.assertRaises(local_app.ApiProblem) as caught:
                    controller.reset_space()
                self.assertEqual(caught.exception.code, expected_code)

        controller._reset_inventory = mock.Mock(return_value=([], []))
        controller._reset_assistant_identities = mock.Mock(return_value=set())
        controller._remove_space_resources = mock.Mock(return_value=(False, set()))
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.reset_space()
        self.assertEqual(caught.exception.code, "teardown-incomplete")


if __name__ == "__main__":
    unittest.main()
