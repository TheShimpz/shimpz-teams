from __future__ import annotations

import runpy
import threading
import types
import unittest
from contextlib import nullcontext
from http import HTTPStatus
from unittest import mock

from docker.errors import APIError, DockerException

from inference import config as inference_config
from local import app as local_app
from storage import files as team_storage


class LocalChatServiceEdgeTests(unittest.TestCase):
    def test_terminal_commit_exclusive_conflict_and_cleanup_paths(self) -> None:
        service = local_app.ChatTurnService(local_app.ChatTurnDependencies())
        self.assertFalse(service._commit_chat_terminal("team_1", "missing"))

        lock = service._chat_lock("team_1")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            with (
                self.assertRaises(local_app.ApiProblem) as caught,
                service._exclusive_chat_turn("team_1"),
            ):
                self.fail("locked chat must not start")
            self.assertEqual(caught.exception.code, "chat-active")
        finally:
            lock.release()

        with service._exclusive_chat_turn("team_1") as token:
            service._active_action_containers["team_1"] = (token, object())
        self.assertNotIn("team_1", service._active_action_containers)

    def test_allowed_host_admission_delegates_to_assistant_lifecycle(self) -> None:
        service = local_app.ChatTurnService(local_app.ChatTurnDependencies())
        service.assistant_lifecycle = types.SimpleNamespace(
            _admit_assistant_allowed_hosts=mock.Mock(return_value=("api.example.com",))
        )
        self.assertEqual(
            service._admit_assistant_allowed_hosts(object(), object()),
            ("api.example.com",),
        )


class LocalControllerConstructionEdgeTests(unittest.TestCase):
    def test_account_egress_and_publication_dependencies_are_required(self) -> None:
        with (
            mock.patch.dict(local_app.os.environ, {}, clear=True),
            self.assertRaises(RuntimeError),
        ):
            local_app._account_egress_transport()

        dependencies = local_app.LocalControllerDependencies(
            inference_store=object(),
            brain_runtime=object(),
            action_state=object(),
            assistant_integrations=object(),
            integration_challenges=object(),
            human_challenges=object(),
            oauth_pkce=object(),
            oauth_broker=object(),
            oauth_service=object(),
            chat_continuations=object(),
        )
        with self.assertRaisesRegex(RuntimeError, "publication installation"):
            local_app.LocalController(
                object(),
                "local-space",
                object(),
                object(),
                dependencies,
            )

    def test_seccomp_requires_available_daemon_and_default_profile(self) -> None:
        controller = object.__new__(local_app.LocalController)
        controller.client = types.SimpleNamespace(info=mock.Mock(side_effect=DockerException("unavailable")))
        with self.assertRaisesRegex(RuntimeError, "daemon is unavailable"):
            controller._require_default_seccomp()

        controller.client.info.side_effect = None
        controller.client.info.return_value = {"SecurityOptions": []}
        with self.assertRaisesRegex(RuntimeError, "default seccomp"):
            controller._require_default_seccomp()


class LocalControllerResourceEdgeTests(unittest.TestCase):
    @staticmethod
    def controller() -> local_app.LocalController:
        controller = object.__new__(local_app.LocalController)
        controller.space_id = "local-space"
        controller._locks = tuple(threading.RLock() for _ in range(64))
        controller.assistant_lifecycle = types.SimpleNamespace(
            _network=mock.Mock(return_value=None),
            _validate_network=mock.Mock(return_value="Team"),
            _base_labels=lambda _team_id, _kind: {},
            _network_name=lambda team_id: f"network-{team_id}",
        )
        controller.storage = types.SimpleNamespace(
            destroy=mock.Mock(),
            put=mock.Mock(return_value={"id": "file"}),
            list=mock.Mock(return_value={"files": []}),
            delete=mock.Mock(return_value={"deleted": True}),
        )
        controller.inference_store = types.SimpleNamespace(
            delete=mock.Mock(),
            load=mock.Mock(return_value=types.SimpleNamespace(provider="openai", model="gpt-5.5")),
            save=mock.Mock(),
        )
        controller.client = types.SimpleNamespace(
            networks=types.SimpleNamespace(list=mock.Mock(return_value=[]), create=mock.Mock()),
            ping=mock.Mock(return_value=True),
        )
        controller._raise_storage_problem = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "storage failed",
                code="storage-safety-failed",
            )
        )
        controller._raise_inference_problem = mock.Mock(
            side_effect=local_app.ApiProblem(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "inference failed",
                code="inference-store-failed",
            )
        )
        return controller

    def test_team_listing_maps_docker_and_rejects_invalid_labels(self) -> None:
        controller = self.controller()
        controller.client.networks.list.side_effect = DockerException("unavailable")
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.list_teams()
        self.assertEqual(caught.exception.code, "docker-unavailable")

        network = types.SimpleNamespace(attrs={"Labels": {local_app.TEAM_LABEL: 7}})
        controller.client.networks.list.side_effect = None
        controller.client.networks.list.return_value = [network]
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.list_teams()
        self.assertEqual(caught.exception.code, "ownership-conflict")

        first = types.SimpleNamespace(attrs={"Labels": {local_app.TEAM_LABEL: "team_b"}})
        second = types.SimpleNamespace(attrs={"Labels": {local_app.TEAM_LABEL: "team_a"}})
        controller.client.networks.list.return_value = [first, second]
        controller.assistant_lifecycle._validate_network.side_effect = (
            "Team B",
            "Team A",
        )
        self.assertEqual(
            [item["team_id"] for item in controller.list_teams()["teams"]],
            ["team_a", "team_b"],
        )

    def test_team_creation_covers_existing_cleanup_concurrency_and_success(self) -> None:
        controller = self.controller()
        existing = object()
        controller.assistant_lifecycle._network.return_value = existing
        controller.assistant_lifecycle._validate_network.return_value = "Other"
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.create_team("team_1", "Team")
        self.assertEqual(caught.exception.code, "team-name-conflict")

        controller.assistant_lifecycle._validate_network.return_value = "Team"
        self.assertFalse(controller.create_team("team_1", "Team")["created"])

        controller.assistant_lifecycle._network.return_value = None
        controller.storage.destroy.side_effect = team_storage.StorageError("unavailable")
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.create_team("team_1", "Team")
        self.assertEqual(caught.exception.code, "storage-safety-failed")

        controller.storage.destroy.side_effect = None
        controller.inference_store.delete.side_effect = inference_config.InferenceConfigError("unavailable")
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.create_team("team_1", "Team")
        self.assertEqual(caught.exception.code, "inference-store-failed")

        controller.inference_store.delete.side_effect = None
        controller.client.networks.create.side_effect = APIError("conflict")
        controller.assistant_lifecycle._network.side_effect = (None, None)
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.create_team("team_1", "Team")
        self.assertEqual(caught.exception.code, "docker-create-failed")

        concurrent = object()
        controller.assistant_lifecycle._network.side_effect = (None, concurrent)
        controller.assistant_lifecycle._validate_network.return_value = "Other"
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.create_team("team_1", "Team")
        self.assertEqual(caught.exception.code, "team-name-conflict")

        controller.assistant_lifecycle._network.side_effect = (None, concurrent)
        controller.assistant_lifecycle._validate_network.return_value = "Team"
        self.assertFalse(controller.create_team("team_1", "Team")["created"])

        controller.assistant_lifecycle._network.side_effect = None
        controller.assistant_lifecycle._network.return_value = None
        controller.client.networks.create.side_effect = None
        network = object()
        controller.client.networks.create.return_value = network
        self.assertTrue(controller.create_team("team_1", "Team")["created"])
        controller.assistant_lifecycle._validate_network.assert_called_with(
            network,
            "team_1",
        )

    def test_storage_problem_mapping_and_file_operations_cover_all_families(self) -> None:
        cases = (
            (team_storage.StorageQuotaError("quota"), "storage-quota-exceeded"),
            (team_storage.StorageNotFoundError("missing"), "file-not-found"),
            (team_storage.StorageInputError("invalid"), "invalid-file"),
            (team_storage.StorageError("failed"), "storage-safety-failed"),
        )
        for failure, expected_code in cases:
            with self.subTest(expected_code=expected_code), self.assertRaises(local_app.ApiProblem) as caught:
                local_app.LocalController._raise_storage_problem(failure)
            self.assertEqual(caught.exception.code, expected_code)

        controller = self.controller()
        operations = (
            (controller.put_file, controller.storage.put, ("team_1", "file", b"x", "text/plain")),
            (controller.list_files, controller.storage.list, ("team_1",)),
            (controller.delete_file, controller.storage.delete, ("team_1", "a" * 32)),
        )
        for operation, storage_operation, arguments in operations:
            with self.subTest(operation=operation.__name__):
                storage_operation.side_effect = team_storage.StorageError("failed")
                with self.assertRaises(local_app.ApiProblem):
                    operation(*arguments)
                storage_operation.side_effect = None

        self.assertEqual(controller.put_file("team_1", "file", b"x", "text/plain")["file"], {"id": "file"})
        self.assertEqual(controller.list_files("team_1"), {"team_id": "team_1", "files": []})
        self.assertTrue(controller.delete_file("team_1", "a" * 32)["deleted"])

    def test_inference_registry_and_health_cover_failure_and_success(self) -> None:
        controller = self.controller()
        controller.inference_store.load.side_effect = inference_config.InferenceConfigError("missing")
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.inference_status("team_1")
        self.assertEqual(caught.exception.code, "inference-not-configured")

        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.configure_inference(
                "team_1",
                {"provider": "unknown", "model": "model"},
            )
        self.assertEqual(caught.exception.code, "invalid-inference")

        controller.inference_store.save.side_effect = inference_config.InferenceConfigError("unavailable")
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.configure_inference(
                "team_1",
                {"provider": "openai", "model": "gpt-5.5"},
            )
        self.assertEqual(caught.exception.code, "inference-store-failed")

        spec = types.SimpleNamespace(
            assistant_id="assistant",
            name="Assistant",
            summary="Summary",
            actions={"b": object(), "a": object()},
        )
        controller.registry = types.SimpleNamespace(catalog=lambda: (spec,))
        self.assertEqual(controller.list_registry()["assistants"][0]["actions"], ["a", "b"])

        controller.client.ping.return_value = False
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.health()
        self.assertEqual(caught.exception.code, "docker-unavailable")
        controller.client.ping.return_value = True
        self.assertEqual(controller.health(), {"status": "ok"})


class LocalControllerInvokeEdgeTests(unittest.TestCase):
    @staticmethod
    def controller() -> tuple[local_app.LocalController, object, object]:
        controller = object.__new__(local_app.LocalController)
        controller._locks = tuple(threading.RLock() for _ in range(64))
        action_spec = types.SimpleNamespace(human_requests=())
        spec = types.SimpleNamespace(actions={"action": action_spec})
        container = types.SimpleNamespace(id="container", status="running", reload=mock.Mock())
        controller.assistant_lifecycle = types.SimpleNamespace(
            _resolve=lambda *_args: spec,
            _network=lambda _team_id: types.SimpleNamespace(name="network"),
            _assistant_container=lambda *_args: container,
            _validate_container=mock.Mock(),
            _blocked_action_workloads=set(),
            _rpc=mock.Mock(return_value={"result": "raw"}),
        )
        controller.chat_turn_service = types.SimpleNamespace(
            _active_chat_guard=nullcontext(),
            _active_action_containers={},
            _resolve_action_integrations=lambda *_args: {},
        )
        return controller, spec, container

    def test_invoke_rejects_contract_runtime_and_rpc_failures(self) -> None:
        controller, spec, container = self.controller()
        spec.actions = {}
        with self.assertRaises(local_app.ApiProblem) as caught:
            controller.invoke("team_1", "assistant", "action", {})
        self.assertEqual(caught.exception.code, "action-not-declared")

        spec.actions = {"action": types.SimpleNamespace(human_requests=())}
        with (
            mock.patch.object(
                local_app,
                "validate_action_payload",
                side_effect=ValueError("invalid"),
            ),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            controller.invoke("team_1", "assistant", "action", {})
        self.assertEqual(caught.exception.code, "invalid-action-input")

        controller.assistant_lifecycle._blocked_action_workloads.add("container")
        with (
            mock.patch.object(local_app, "validate_action_payload", return_value={}),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            controller.invoke("team_1", "assistant", "action", {})
        self.assertEqual(caught.exception.code, "assistant-action-blocked")

        controller.assistant_lifecycle._blocked_action_workloads.clear()
        container.status = "exited"
        with (
            mock.patch.object(local_app, "validate_action_payload", return_value={}),
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            controller.invoke("team_1", "assistant", "action", {})
        self.assertEqual(caught.exception.code, "assistant-not-running")

        container.status = "running"
        controller.assistant_lifecycle._rpc.side_effect = local_app.ApiProblem(
            HTTPStatus.BAD_GATEWAY,
            "failed",
            code="assistant-rpc-failed",
        )
        with (
            mock.patch.object(local_app, "validate_action_payload", return_value={}),
            mock.patch.object(local_app.local_audit, "record_request"),
            self.assertRaises(local_app.ApiProblem),
        ):
            controller.invoke("team_1", "assistant", "action", {}, ({"value": True},))

    def test_invoke_maps_projection_failures_and_returns_valid_result(self) -> None:
        controller, _spec, _container = self.controller()
        failures = (
            (local_app.action_execution.RpcSecretExposureError("unsafe"), "assistant-secret-exposure"),
            (local_app.action_execution.RpcInvalidResultError("invalid"), "invalid-action-output"),
        )
        for failure, expected_code in failures:
            with (
                self.subTest(expected_code=expected_code),
                mock.patch.object(local_app, "validate_action_payload", return_value={}),
                mock.patch.object(
                    local_app.action_execution,
                    "project_rpc_result",
                    side_effect=failure,
                ),
                mock.patch.object(local_app.local_audit, "record_request"),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                controller.invoke("team_1", "assistant", "action", {})
            self.assertEqual(caught.exception.code, expected_code)

        with (
            mock.patch.object(local_app, "validate_action_payload", return_value={}),
            mock.patch.object(
                local_app.action_execution,
                "project_rpc_result",
                return_value={"ok": True},
            ),
            mock.patch.object(local_app.local_audit, "record_request"),
        ):
            result = controller.invoke("team_1", "assistant", "action", {})
        self.assertEqual(result["result"], {"ok": True})


class LocalAppMainEdgeTests(unittest.TestCase):
    def test_main_maps_startup_failure_and_closes_successful_runtime(self) -> None:
        with mock.patch.dict(local_app.os.environ, {}, clear=True):
            self.assertEqual(local_app.main(), 1)

        client = types.SimpleNamespace(close=mock.Mock())
        server = types.SimpleNamespace(
            serve_forever=mock.Mock(side_effect=KeyboardInterrupt),
            server_close=mock.Mock(),
        )
        updater = types.SimpleNamespace(start=mock.Mock(), close=mock.Mock())
        with (
            mock.patch.dict(local_app.os.environ, {"SHIMPZ_SPACE_ID": "local-space"}),
            mock.patch.object(local_app.local_token_store, "ensure_token", return_value="token"),
            mock.patch.object(local_app.brain_runtime_token_store, "ensure"),
            mock.patch.object(local_app.docker, "from_env", return_value=client),
            mock.patch.object(local_app, "PublicationRegistry", return_value=object()),
            mock.patch.object(local_app.bindings, "DynamicAssistantStore", return_value=object()),
            mock.patch.object(local_app.team_storage, "TeamStorage", return_value=object()),
            mock.patch.object(local_app.local_developers, "DevelopersClient", return_value=object()),
            mock.patch.object(local_app.artifact_trust, "ArtifactTrustVerifier", return_value=object()),
            mock.patch.object(local_app.assistant_update, "AssistantUpdateStore", return_value=object()),
            mock.patch.object(local_app.assistant_update, "AssistantResidueStore", return_value=object()),
            mock.patch.object(local_app.icons, "AssistantIconStore", return_value=object()),
            mock.patch.object(local_app, "LocalController", return_value=object()),
            mock.patch.object(local_app, "BoundedServer", return_value=server),
            mock.patch.object(
                local_app.local_automatic_updates,
                "AutomaticAssistantUpdater",
                return_value=updater,
            ),
            mock.patch.object(local_app.local_audit, "record"),
            mock.patch.object(local_app.local_audit, "close"),
        ):
            self.assertEqual(local_app.main(), 0)
        updater.close.assert_called_once_with()
        server.server_close.assert_called_once_with()
        client.close.assert_called_once_with()

    def test_module_entrypoint_exits_on_missing_environment(self) -> None:
        with (
            mock.patch.dict(local_app.os.environ, {}, clear=True),
            self.assertRaises(SystemExit) as caught,
        ):
            runpy.run_path(local_app.__file__, run_name="__main__")
        self.assertEqual(caught.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
