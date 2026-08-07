from __future__ import annotations

import copy
import types
import unittest
from contextlib import nullcontext
from http import HTTPStatus
from unittest import mock

from docker.errors import DockerException, ImageNotFound, NotFound
from local_controller_harness import LocalContractCase

from install import bindings
from local import app as local_app
from local.assistant import lifecycle as assistant_lifecycle


class LocalAssistantLifecycleHelperEdgeTests(unittest.TestCase):
    @staticmethod
    def _rollback_subject() -> types.SimpleNamespace:
        cache = types.SimpleNamespace(discard=mock.Mock())
        return types.SimpleNamespace(
            _assistant_genesis_cache=cache,
            _assistant_allowed_hosts_cache=types.SimpleNamespace(discard=mock.Mock()),
            _assistant_machine_contract_cache=types.SimpleNamespace(discard=mock.Mock()),
            _fail_stop_power=mock.Mock(),
            _release_assistant_egress=mock.Mock(),
        )

    def test_install_rollback_accepts_absence_and_reports_egress_failure(self) -> None:
        subject = self._rollback_subject()
        container = types.SimpleNamespace(
            id="container",
            remove=mock.Mock(side_effect=NotFound("gone")),
        )
        spec = types.SimpleNamespace(assistant_id="assistant")
        self.assertIsNone(
            assistant_lifecycle._rollback_assistant_install(
                subject,
                "team_1",
                spec,
                object(),
                container,
                egress_prepared=False,
            )
        )

        subject._release_assistant_egress.side_effect = local_app.ApiProblem(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "unavailable",
            code="egress-proxy-unavailable",
        )
        problem = assistant_lifecycle._rollback_assistant_install(
            subject,
            "team_1",
            spec,
            object(),
            None,
            egress_prepared=True,
        )
        self.assertEqual(problem.code, "assistant-install-rollback-incomplete")

    def test_container_creation_rejects_missing_token_image_drift_and_docker_failure(self) -> None:
        spec = types.SimpleNamespace(
            assistant_id="assistant",
            image="image@sha256:" + "a" * 64,
            allowed_hosts=("api.example.com",),
        )
        subject = types.SimpleNamespace(
            _reserve_assistant_egress_environment=lambda *_args: (None, {}, object()),
            _rollback_assistant_install=mock.Mock(return_value=None),
            client=types.SimpleNamespace(containers=types.SimpleNamespace(create=mock.Mock())),
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._create_assistant_container(
                subject,
                "team_1",
                spec,
                types.SimpleNamespace(name="network"),
                types.SimpleNamespace(id="sha256:" + "a" * 64),
            )
        self.assertEqual(caught.exception.code, "egress-policy-unavailable")

        spec.allowed_hosts = ()
        container = types.SimpleNamespace(
            attrs={"Image": "sha256:" + "b" * 64},
            reload=mock.Mock(),
        )
        subject.client.containers.create.return_value = container
        subject.cpuset_cpus = "0"
        subject._container_name = mock.Mock(return_value="container")
        subject._assistant_labels = mock.Mock(return_value={})
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._create_assistant_container(
                subject,
                "team_1",
                spec,
                types.SimpleNamespace(name="network"),
                types.SimpleNamespace(id="sha256:" + "a" * 64),
            )
        self.assertEqual(caught.exception.code, "image-resolution-mismatch")

        subject.client.containers.create.side_effect = DockerException("unavailable")
        subject._rollback_assistant_install.return_value = None
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._create_assistant_container(
                subject,
                "team_1",
                spec,
                types.SimpleNamespace(name="network"),
                types.SimpleNamespace(id="sha256:" + "a" * 64),
            )
        self.assertEqual(caught.exception.code, "docker-install-failed")

        rollback = local_app.ApiProblem(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "rollback incomplete",
            code="assistant-install-rollback-incomplete",
        )
        subject._rollback_assistant_install.return_value = rollback
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._create_assistant_container(
                subject,
                "team_1",
                spec,
                types.SimpleNamespace(name="network"),
                types.SimpleNamespace(id="sha256:" + "a" * 64),
            )
        self.assertIs(caught.exception, rollback)

        container.attrs["Image"] = "sha256:" + "a" * 64
        container.id = "container"
        container.start = mock.Mock()
        subject.client.containers.create.side_effect = None
        subject.client.containers.create.return_value = container
        subject._rollback_assistant_install.return_value = None
        subject._admit_assistant_allowed_hosts = mock.Mock(return_value=())
        subject._validate_container = mock.Mock()
        subject._wait_ready = mock.Mock()
        subject._active_assistant_genesis = mock.Mock()
        authorize = mock.Mock()
        assistant_lifecycle._create_assistant_container(
            subject,
            "team_1",
            spec,
            types.SimpleNamespace(name="network"),
            types.SimpleNamespace(id="sha256:" + "a" * 64),
            authorize_start=authorize,
        )
        authorize.assert_called_once_with()

    @staticmethod
    def _replacement_subject() -> types.SimpleNamespace:
        return types.SimpleNamespace(
            _trusted_image=mock.Mock(return_value=object()),
            _validate_container=mock.Mock(),
            _validate_container_isolation=mock.Mock(return_value={}),
            _has_current_assistant_artifact=mock.Mock(return_value=False),
            _assistant_genesis_cache=types.SimpleNamespace(discard=mock.Mock()),
            _assistant_allowed_hosts_cache=types.SimpleNamespace(discard=mock.Mock()),
            _assistant_machine_contract_cache=types.SimpleNamespace(discard=mock.Mock()),
            _create_assistant_container=mock.Mock(),
            _team_has_egress_assistant=mock.Mock(return_value=False),
            _release_assistant_egress=mock.Mock(),
            chat_turn_service=types.SimpleNamespace(
                _retain_declared_assistant_integration_state=mock.Mock()
            ),
        )

    def test_unready_replacement_maps_removal_and_forwards_authorization(self) -> None:
        subject = self._replacement_subject()
        existing = types.SimpleNamespace(
            id="container",
            remove=mock.Mock(side_effect=DockerException("unavailable")),
        )
        spec = types.SimpleNamespace(assistant_id="assistant")
        network = types.SimpleNamespace(name="network")
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._replace_unready_assistant(
                subject,
                "team_1",
                spec,
                network,
                existing,
            )
        self.assertEqual(caught.exception.code, "docker-remove-failed")

        existing.remove.side_effect = None
        authorize = mock.Mock()
        assistant_lifecycle._replace_unready_assistant(
            subject,
            "team_1",
            spec,
            network,
            existing,
            authorize_start=authorize,
        )
        self.assertIs(
            subject._create_assistant_container.call_args.kwargs["authorize_start"],
            authorize,
        )

        subject._create_assistant_container.reset_mock()
        assistant_lifecycle._replace_unready_assistant(
            subject,
            "team_1",
            spec,
            network,
            existing,
        )
        self.assertEqual(subject._create_assistant_container.call_args.kwargs, {})

    def test_outdated_replacement_detects_race_maps_removal_and_releases_egress(self) -> None:
        subject = self._replacement_subject()
        existing = types.SimpleNamespace(id="container", remove=mock.Mock())
        spec = types.SimpleNamespace(
            assistant_id="assistant",
            allowed_hosts=("api.example.com",),
        )
        network = types.SimpleNamespace(name="network")
        subject._has_current_assistant_artifact.return_value = True
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._replace_outdated_assistant(
                subject,
                "team_1",
                spec,
                network,
                existing,
            )
        self.assertEqual(caught.exception.code, "assistant-update-conflict")

        subject._has_current_assistant_artifact.return_value = False
        existing.remove.side_effect = DockerException("unavailable")
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._replace_outdated_assistant(
                subject,
                "team_1",
                spec,
                network,
                existing,
            )
        self.assertEqual(caught.exception.code, "docker-remove-failed")

        existing.remove.side_effect = None
        authorize = mock.Mock()
        assistant_lifecycle._replace_outdated_assistant(
            subject,
            "team_1",
            spec,
            network,
            existing,
            authorize_start=authorize,
        )
        subject._release_assistant_egress.assert_called_once()
        self.assertIs(
            subject._create_assistant_container.call_args.kwargs["authorize_start"],
            authorize,
        )

    def test_previous_restore_and_image_inventory_fail_closed(self) -> None:
        subject = types.SimpleNamespace(
            _create_assistant_container=mock.Mock(
                side_effect=local_app.ApiProblem(
                    HTTPStatus.BAD_GATEWAY,
                    "failed",
                    code="assistant-not-ready",
                )
            )
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._restore_previous_assistant(
                subject,
                "team_1",
                object(),
                object(),
                object(),
            )
        self.assertEqual(caught.exception.code, "assistant-update-rollback-incomplete")

        specs = (types.SimpleNamespace(image="missing"), types.SimpleNamespace(image="current"))
        subject = types.SimpleNamespace(
            registry=types.SimpleNamespace(all=lambda: specs),
            client=types.SimpleNamespace(
                images=types.SimpleNamespace(
                    get=mock.Mock(
                        side_effect=(ImageNotFound("missing"), types.SimpleNamespace(id="target"))
                    )
                )
            ),
        )
        self.assertTrue(assistant_lifecycle._binding_uses_image(subject, "target"))

        subject.registry.all = mock.Mock(side_effect=bindings.DynamicAssistantError("unavailable"))
        self.assertIsNone(assistant_lifecycle._binding_uses_image(subject, "target"))

        subject.registry.all = lambda: (types.SimpleNamespace(image="other"),)
        subject.client.images.get = lambda _image: types.SimpleNamespace(id="other")
        self.assertFalse(assistant_lifecycle._binding_uses_image(subject, "target"))

    def test_retired_image_deletion_inventory_and_journal_errors_are_deferred(self) -> None:
        subject = types.SimpleNamespace(
            client=types.SimpleNamespace(
                images=types.SimpleNamespace(remove=mock.Mock(side_effect=ImageNotFound("gone"))),
                containers=types.SimpleNamespace(
                    list=mock.Mock(side_effect=DockerException("unavailable"))
                ),
            ),
            _binding_uses_image=lambda _image_id: False,
        )
        self.assertTrue(assistant_lifecycle._delete_retired_image(subject, "image"))
        subject.client.images.remove.side_effect = DockerException("referenced")
        self.assertFalse(assistant_lifecycle._delete_retired_image(subject, "image"))
        self.assertFalse(assistant_lifecycle._remove_retired_image(subject, "image"))

        subject.updates = types.SimpleNamespace(
            clear=mock.Mock(side_effect=bindings.DynamicAssistantError("unavailable"))
        )
        self.assertIsNone(assistant_lifecycle._clear_update(subject, object()))

    def test_residue_sweep_and_queue_defer_unavailable_state(self) -> None:
        subject = types.SimpleNamespace(
            residues=types.SimpleNamespace(
                list=mock.Mock(side_effect=bindings.DynamicAssistantError("unavailable"))
            )
        )
        self.assertIsNone(assistant_lifecycle.sweep_residues(subject))

        residue = types.SimpleNamespace(image_id="image")
        subject.residues.list.side_effect = None
        subject.residues.list.return_value = (residue,)
        subject.residues.clear = mock.Mock(
            side_effect=bindings.DynamicAssistantError("unavailable")
        )
        subject._remove_retired_image = lambda _image_id: True
        assistant_lifecycle.sweep_residues(subject)
        subject.residues.clear.assert_called_once_with(residue)

        subject._remove_retired_image = lambda _image_id: False
        subject.residues.clear.reset_mock()
        assistant_lifecycle.sweep_residues(subject)
        subject.residues.clear.assert_not_called()

        subject.residues.add = mock.Mock(side_effect=OSError("unavailable"))
        subject._remove_retired_image = mock.Mock(return_value=False)
        assistant_lifecycle._queue_residue(subject, "image")
        subject._remove_retired_image.assert_called_once_with("image")

        subject._remove_retired_image.return_value = True
        assistant_lifecycle._queue_residue(subject, "image")


class LocalAssistantLifecycleUpdateEdgeTests(LocalContractCase):
    def _update_specs(self):
        controller, container, events = self._lifecycle_controller()
        previous = copy.copy(controller.registry["shimpz-cloudflare"])
        successor = copy.copy(previous)
        previous_binding = types.SimpleNamespace(
            binding_digest="sha256:" + "1" * 64
        )
        return controller, container, events, previous, successor, previous_binding

    def test_update_maps_previous_image_and_replacement_failures(self) -> None:
        controller, _container, _events, previous, successor, binding = self._update_specs()
        controller.assistant_lifecycle._validate_container_security = mock.Mock()
        controller.assistant_lifecycle._trusted_image = mock.Mock(return_value=object())
        controller.client.images = types.SimpleNamespace(
            get=mock.Mock(side_effect=DockerException("unavailable"))
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle.update_assistant(
                "team_1",
                previous,
                successor,
                previous_binding=binding,
                resolution={},
                authorize_start=lambda: None,
            )
        self.assertEqual(caught.exception.code, "docker-image-unavailable")

        controller, container, _events, previous, successor, binding = self._update_specs()
        previous.allowed_hosts = ("api.example.com",)
        successor.allowed_hosts = previous.allowed_hosts
        previous_image = types.SimpleNamespace(id="sha256:" + "a" * 64)
        successor_image = types.SimpleNamespace(id="sha256:" + "b" * 64)
        controller.assistant_lifecycle._validate_container_security = mock.Mock()
        controller.assistant_lifecycle._trusted_image = mock.Mock(
            return_value=successor_image
        )
        controller.client.images = types.SimpleNamespace(get=lambda _image: previous_image)
        controller.assistant_lifecycle.updates = types.SimpleNamespace(
            begin=lambda *_args: object()
        )
        controller.assistant_lifecycle._team_has_egress_assistant = mock.Mock(
            return_value=False
        )
        controller.assistant_lifecycle._release_assistant_egress = mock.Mock()
        controller.assistant_lifecycle._create_assistant_container = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.BAD_GATEWAY,
                "not ready",
                code="assistant-not-ready",
            )
        )
        controller.assistant_lifecycle._restore_previous_assistant = mock.Mock()
        controller.assistant_lifecycle._queue_residue = mock.Mock()
        controller.assistant_lifecycle._clear_update = mock.Mock()
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle.update_assistant(
                "team_1",
                previous,
                successor,
                previous_binding=binding,
                resolution={},
                authorize_start=lambda: None,
            )
        self.assertEqual(caught.exception.code, "assistant-not-ready")
        controller.assistant_lifecycle._release_assistant_egress.assert_called_once()

        controller.assistant_lifecycle._create_assistant_container.side_effect = None
        container.remove = mock.Mock(side_effect=DockerException("unavailable"))
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle.update_assistant(
                "team_1",
                previous,
                successor,
                previous_binding=binding,
                resolution={},
                authorize_start=lambda: None,
            )
        self.assertEqual(caught.exception.code, "docker-remove-failed")

    def test_binding_commit_failure_rolls_back_or_reports_incomplete_cleanup(self) -> None:
        for cleanup_error in (
            None,
            local_app.ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "rollback incomplete",
                code="assistant-install-rollback-incomplete",
            ),
        ):
            with self.subTest(cleanup_error=cleanup_error):
                controller, _container, _events, previous, successor, binding = (
                    self._update_specs()
                )
                previous_image = types.SimpleNamespace(id="sha256:" + "a" * 64)
                successor_image = types.SimpleNamespace(id="sha256:" + "b" * 64)
                controller.assistant_lifecycle._validate_container_security = mock.Mock()
                controller.assistant_lifecycle._trusted_image = mock.Mock(
                    return_value=successor_image
                )
                controller.client.images = types.SimpleNamespace(
                    get=lambda _image, current=previous_image: current
                )
                transaction = types.SimpleNamespace(previous_image_id=previous_image.id)
                controller.assistant_lifecycle.updates = types.SimpleNamespace(
                    begin=lambda *_args, current=transaction: current
                )
                controller.assistant_lifecycle._create_assistant_container = mock.Mock()
                controller.registry.commit_replacement = mock.Mock(
                    side_effect=bindings.DynamicAssistantError("conflict")
                )
                controller.assistant_lifecycle._rollback_assistant_install = mock.Mock(
                    return_value=cleanup_error
                )
                controller.assistant_lifecycle._restore_previous_assistant = mock.Mock()
                controller.assistant_lifecycle._queue_residue = mock.Mock()
                controller.assistant_lifecycle._clear_update = mock.Mock()
                with self.assertRaises(local_app.ApiProblem) as caught:
                    controller.assistant_lifecycle.update_assistant(
                        "team_1",
                        previous,
                        successor,
                        previous_binding=binding,
                        resolution={},
                        authorize_start=lambda: None,
                    )
                expected = (
                    "assistant-update-conflict"
                    if cleanup_error is None
                    else "assistant-install-rollback-incomplete"
                )
                self.assertEqual(caught.exception.code, expected)

    def test_recovery_target_covers_absent_current_unknown_and_removal_failures(self) -> None:
        target = types.SimpleNamespace(
            assistant_id="assistant",
            allowed_hosts=(),
        )
        previous = copy.copy(target)
        successor = copy.copy(target)
        update = types.SimpleNamespace(
            team_id="team_1",
            previous=object(),
            successor=object(),
        )
        network = types.SimpleNamespace(name="network")
        subject = types.SimpleNamespace(
            _network=lambda _team_id: network,
            _assistant_container=mock.Mock(return_value=None),
            _trusted_image=mock.Mock(return_value=object()),
            _create_assistant_container=mock.Mock(),
        )
        assistant_lifecycle._recover_update_target(subject, update, target)
        subject._create_assistant_container.assert_called_once()

        existing = types.SimpleNamespace(
            id="container",
            status="exited",
            start=mock.Mock(),
            reload=mock.Mock(),
            remove=mock.Mock(),
        )
        subject._assistant_container.return_value = existing
        subject.registry = types.SimpleNamespace(
            spec=lambda binding: previous if binding is update.previous else successor
        )
        subject._validate_container_profile = mock.Mock(return_value=({}, {}))
        subject._has_current_assistant_artifact = mock.Mock(return_value=True)
        subject._validate_container_security = mock.Mock()
        subject._wait_ready = mock.Mock()
        subject._active_assistant_genesis = mock.Mock()
        assistant_lifecycle._recover_update_target(subject, update, target)
        existing.start.assert_called_once_with()

        existing.start.side_effect = DockerException("unavailable")
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._recover_update_target(subject, update, target)
        self.assertEqual(caught.exception.code, "docker-start-failed")

        existing.start.side_effect = None
        existing.status = "running"
        assistant_lifecycle._recover_update_target(subject, update, target)

        subject._has_current_assistant_artifact.side_effect = (False, False, False)
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._recover_update_target(subject, update, target)
        self.assertEqual(caught.exception.code, "assistant-update-conflict")

        actual = previous
        actual.allowed_hosts = ("api.example.com",)
        target.allowed_hosts = actual.allowed_hosts
        subject._has_current_assistant_artifact.side_effect = (False, True, True)
        existing.remove.side_effect = DockerException("unavailable")
        subject._team_has_egress_assistant = mock.Mock(return_value=False)
        with self.assertRaises(local_app.ApiProblem) as caught:
            assistant_lifecycle._recover_update_target(subject, update, target)
        self.assertEqual(caught.exception.code, "docker-remove-failed")

        subject._has_current_assistant_artifact.side_effect = (False, True, True)
        existing.remove.side_effect = None
        subject._release_assistant_egress = mock.Mock()
        subject._create_assistant_container.reset_mock()
        assistant_lifecycle._recover_update_target(subject, update, target)
        subject._release_assistant_egress.assert_called_once()
        subject._create_assistant_container.assert_called_once()

    def test_recover_updates_handles_previous_successor_and_mismatched_bindings(self) -> None:
        previous = object()
        successor = object()
        updates = (
            types.SimpleNamespace(
                team_id="team_1",
                assistant_id="previous",
                previous=previous,
                successor=successor,
                previous_image_id="image-1",
            ),
            types.SimpleNamespace(
                team_id="team_1",
                assistant_id="successor",
                previous=previous,
                successor=successor,
                previous_image_id="image-2",
            ),
            types.SimpleNamespace(
                team_id="team_1",
                assistant_id="mismatch",
                previous=previous,
                successor=successor,
                previous_image_id="image-3",
            ),
        )
        bindings_by_id = {
            "previous": previous,
            "successor": successor,
            "mismatch": object(),
        }
        subject = types.SimpleNamespace(
            updates=types.SimpleNamespace(list=lambda: updates),
            _lock=lambda _team_id: nullcontext(),
            registry=types.SimpleNamespace(
                binding=lambda _team_id, assistant_id: bindings_by_id[assistant_id],
                spec=lambda binding: types.SimpleNamespace(binding=binding),
            ),
            _recover_update_target=mock.Mock(),
            chat_turn_service=types.SimpleNamespace(
                _retain_declared_assistant_integration_state=mock.Mock()
            ),
            _queue_residue=mock.Mock(),
            _clear_update=mock.Mock(),
            sweep_residues=mock.Mock(),
        )
        assistant_lifecycle.recover_updates(subject)
        self.assertEqual(subject._recover_update_target.call_count, 2)
        subject.chat_turn_service._retain_declared_assistant_integration_state.assert_called_once()
        subject._queue_residue.assert_called_once_with("image-2")
        subject.sweep_residues.assert_called_once_with()


class LocalAssistantLifecycleOperationEdgeTests(LocalContractCase):
    def test_install_starts_stopped_current_assistant_with_authorization(self) -> None:
        controller, container, events = self._lifecycle_controller()
        controller.assistant_lifecycle._validate_container_isolation = mock.Mock(
            return_value={}
        )
        controller.assistant_lifecycle._has_current_assistant_artifact = mock.Mock(
            return_value=True
        )
        controller.assistant_lifecycle._validate_container_security = mock.Mock()
        container.status = "exited"
        container.start = lambda: events.append("start")
        controller.assistant_lifecycle._wait_ready = mock.Mock()
        controller.assistant_lifecycle._active_assistant_genesis = mock.Mock()
        authorize = mock.Mock()

        result = controller.assistant_lifecycle.install_assistant(
            "team_1",
            "shimpz-cloudflare",
            authorize_start=authorize,
        )

        self.assertEqual(result, {"assistant": "shimpz-cloudflare", "installed": False})
        authorize.assert_called_once_with()
        self.assertIn("start", events)

    def test_install_maps_start_failure_and_replaces_only_readiness_failure(self) -> None:
        controller, container, _events = self._lifecycle_controller()
        controller.assistant_lifecycle._validate_container_isolation = mock.Mock(
            return_value={}
        )
        controller.assistant_lifecycle._has_current_assistant_artifact = mock.Mock(
            return_value=True
        )
        controller.assistant_lifecycle._validate_container_security = mock.Mock()
        container.status = "exited"
        container.start = mock.Mock(side_effect=DockerException("unavailable"))
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle.install_assistant(
                "team_1",
                "shimpz-cloudflare",
            )
        self.assertEqual(caught.exception.code, "docker-start-failed")

        controller, container, _events = self._lifecycle_controller()
        controller.assistant_lifecycle._validate_container_isolation = mock.Mock(
            return_value={}
        )
        controller.assistant_lifecycle._has_current_assistant_artifact = mock.Mock(
            return_value=True
        )
        controller.assistant_lifecycle._validate_container_security = mock.Mock()
        controller.assistant_lifecycle._wait_ready = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.CONFLICT,
                "drift",
                code="assistant-isolation-drift",
            )
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle.install_assistant(
                "team_1",
                "shimpz-cloudflare",
            )
        self.assertEqual(caught.exception.code, "assistant-isolation-drift")

        controller.assistant_lifecycle._wait_ready.side_effect = local_app.ApiProblem(
            HTTPStatus.BAD_GATEWAY,
            "not ready",
            code="assistant-not-ready",
        )
        controller.assistant_lifecycle._replace_unready_assistant = mock.Mock()
        controller.assistant_lifecycle.install_assistant("team_1", "shimpz-cloudflare")
        controller.assistant_lifecycle._replace_unready_assistant.assert_called_once()

    def test_new_install_forwards_authorization_and_uninstall_maps_removal(self) -> None:
        controller, container, _events = self._lifecycle_controller()
        controller.assistant_lifecycle._assistant_container = lambda *_args, **_kwargs: None
        controller.assistant_lifecycle._trusted_image = mock.Mock(return_value=object())
        controller.assistant_lifecycle._create_assistant_container = mock.Mock()
        authorize = mock.Mock()
        result = controller.assistant_lifecycle.install_assistant(
            "team_1",
            "shimpz-cloudflare",
            authorize_start=authorize,
        )
        self.assertEqual(result, {"assistant": "shimpz-cloudflare", "installed": True})
        self.assertIs(
            controller.assistant_lifecycle._create_assistant_container.call_args.kwargs[
                "authorize_start"
            ],
            authorize,
        )

        controller.assistant_lifecycle._create_assistant_container.reset_mock()
        controller.assistant_lifecycle.install_assistant(
            "team_1",
            "shimpz-cloudflare",
        )
        self.assertEqual(
            controller.assistant_lifecycle._create_assistant_container.call_args.kwargs,
            {},
        )

        controller, container, _events = self._lifecycle_controller()
        container.remove = mock.Mock(side_effect=DockerException("unavailable"))
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.assistant_lifecycle.uninstall_assistant(
                "team_1",
                "shimpz-cloudflare",
            )
        self.assertEqual(caught.exception.code, "docker-remove-failed")

    def test_uninstall_without_container_releases_residual_egress_and_icon(self) -> None:
        controller, _container, _events = self._lifecycle_controller()
        binding = types.SimpleNamespace(
            resolution={"source_digest": "sha256:" + "c" * 64}
        )
        controller.registry.binding = lambda *_args: binding
        controller.registry.bindings = lambda: ()
        controller.assistant_lifecycle._assistant_container = lambda *_args, **_kwargs: None
        controller.assistant_lifecycle._egress_token = mock.Mock(return_value="token")
        controller.assistant_lifecycle._team_has_egress_assistant = mock.Mock(
            return_value=False
        )
        controller.assistant_lifecycle._release_assistant_egress = mock.Mock()
        controller.icons = types.SimpleNamespace(discard_unreferenced=mock.Mock())
        controller.assistant_lifecycle.icons = controller.icons

        result = controller.assistant_lifecycle.uninstall_assistant(
            "team_1",
            "shimpz-cloudflare",
        )

        self.assertEqual(result, {"assistant": "shimpz-cloudflare", "uninstalled": False})
        controller.assistant_lifecycle._release_assistant_egress.assert_called_once()
        controller.icons.discard_unreferenced.assert_called_once()

    def test_uninstall_existing_container_discards_unreferenced_icon(self) -> None:
        controller, _container, _events = self._lifecycle_controller()
        binding = types.SimpleNamespace(
            resolution={"source_digest": "sha256:" + "c" * 64}
        )
        controller.registry.binding = lambda *_args: binding
        controller.registry.bindings = lambda: ()
        controller.icons = types.SimpleNamespace(discard_unreferenced=mock.Mock())
        controller.assistant_lifecycle.icons = controller.icons

        result = controller.assistant_lifecycle.uninstall_assistant(
            "team_1",
            "shimpz-cloudflare",
        )

        self.assertTrue(result["uninstalled"])
        controller.icons.discard_unreferenced.assert_called_once()


if __name__ == "__main__":
    unittest.main()
