from __future__ import annotations

import runpy
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from threading import RLock
from unittest import mock

from docker.errors import DockerException, NotFound

from action import execution as action_execution
from action import journal as action_journal
from chat import orchestrator as chat_orchestrator
from install import icons
from local import healthcheck as local_healthcheck
from local import validation
from local.assistant import api as assistant_api
from local.assistant import rpc as assistant_rpc
from local.chat import resume as chat_resume
from local.chat import segment as chat_segment
from local.chat import types as chat_types
from local.errors import ApiProblemError


class LocalLeafContractTests(unittest.TestCase):
    def test_local_identity_and_cpu_inputs_are_strict(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "SHIMPZ_SPACE_ID"):
            validation.validate_space_id("INVALID")
        for processors in (True, 0, "2"):
            with self.subTest(processors=processors), self.assertRaisesRegex(RuntimeError, "CPU count"):
                validation.half_cpu_set(processors)

    def test_healthcheck_script_maps_failures_to_unhealthy(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            runpy.run_path(str(Path(local_healthcheck.__file__)), run_name="__main__")
        self.assertEqual(caught.exception.code, 1)

    def test_required_assistant_rejects_an_unavailable_binding(self) -> None:
        with self.assertRaisesRegex(ApiProblemError, "unavailable Assistant") as caught:
            chat_types.required_active_assistant({}, "missing")
        self.assertEqual(caught.exception.code, "assistant-unavailable")

    def test_segment_rejects_changed_action_contract_and_journal_failure(self) -> None:
        active = chat_segment._ActiveAssistant(
            types.SimpleNamespace(assistant_id="helper", name="Helper", actions={}, image="image"),
            "container-id",
        )
        controller = types.SimpleNamespace(
            storage=types.SimpleNamespace(metadata_connection=lambda *_args: nullcontext(None)),
            _chat_setup=lambda *_args: (
                "Team",
                "a" * 12,
                (active,),
                (),
                types.SimpleNamespace(provider="openai", model="model"),
            ),
            _chat_identity=lambda *_args: ("identity",),
            _active_assistant_genesis=lambda _active: "genesis",
            space_id="local",
            action_state=mock.Mock(),
            brain_runtime=object(),
            _validate_chat_action=mock.Mock(),
            _require_chat_private_inputs=mock.Mock(return_value=True),
            _chat_cancelled=mock.Mock(return_value=False),
            _validate_chat_context=mock.Mock(),
            _raise_chat_problem=mock.Mock(),
            _require_action_rpc_envelope=mock.Mock(),
            _action_integration_generations=mock.Mock(return_value=()),
            _invoke_chat_action=mock.Mock(),
        )
        request = chat_segment.SegmentRequest(
            team_id="team_1",
            file_ids=[],
            assistant_ids=("helper",),
            provider="openai",
            api_key="x" * 20,
            token="t" * 32,
            continuation=object(),
        )

        def changed_action(strategy: object, **_kwargs: object) -> object:
            strategy.prepare()
            return strategy.human_requirement(
                types.SimpleNamespace(assistant_id="helper", action="missing", interrupt_id="interrupt"),
                object(),
            )

        with (
            mock.patch.object(chat_segment.chat_turn_engine, "run_segment", side_effect=changed_action),
            self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "contract changed"),
        ):
            chat_segment._run_chat_segment_with_metadata(controller, request, None)

        controller.action_state.purge_replayable.side_effect = action_journal.ActionJournalError("unavailable")
        controller._raise_chat_problem = mock.Mock(side_effect=RuntimeError("mapped journal failure"))
        fresh_request = chat_segment.SegmentRequest(
            team_id="team_1",
            file_ids=[],
            assistant_ids=("helper",),
            provider="openai",
            api_key="x" * 20,
            token="t" * 32,
        )

        def prepare_only(strategy: object, **_kwargs: object) -> object:
            return strategy.prepare()

        with (
            mock.patch.object(chat_segment.chat_turn_engine, "run_segment", side_effect=prepare_only),
            self.assertRaisesRegex(RuntimeError, "mapped journal failure"),
        ):
            chat_segment._run_chat_segment_with_metadata(controller, fresh_request, None)
        controller._raise_chat_problem.assert_called_once()

    def test_stop_chat_cancels_human_generation_and_matching_action(self) -> None:
        container = object()
        lifecycle = types.SimpleNamespace(
            _network=lambda _team_id: types.SimpleNamespace(id="network-id"),
            _fail_stop_action=mock.Mock(),
        )
        controller = types.SimpleNamespace(
            assistant_lifecycle=lifecycle,
            integration_challenges=types.SimpleNamespace(cancel_team=lambda _team_id: False),
            human_challenges=types.SimpleNamespace(cancel_team=lambda _team_id: True),
            oauth_pkce=types.SimpleNamespace(cancel_team=mock.Mock()),
            _delete_chat_continuation=lambda _team_id: False,
            _purge_human_generation=mock.Mock(),
            _active_chat_guard=RLock(),
            _active_chat_tokens={"team_1": "token"},
            _cancelled_chat_tokens=set(),
            _active_action_containers={"team_1": ("token", container)},
        )
        result = chat_resume.stop_chat(controller, "team_1")
        self.assertTrue(result["accepted"])
        self.assertTrue(result["confirmed"])
        controller._purge_human_generation.assert_called_once_with("network-id")
        lifecycle._fail_stop_action.assert_called_once_with(container)

        controller.human_challenges.cancel_team = lambda _team_id: False
        controller._active_action_containers = {"team_1": ("other-token", container)}
        result = chat_resume.stop_chat(controller, "team_1")
        self.assertFalse(result["confirmed"])

        controller._active_chat_tokens = {}
        controller._active_action_containers = {}
        result = chat_resume.stop_chat(controller, "team_1")
        self.assertFalse(result["accepted"])

    def test_assistant_inventory_maps_icon_docker_registry_and_egress_failures(self) -> None:
        binding = types.SimpleNamespace(resolution={})
        controller = types.SimpleNamespace(
            _lock=lambda _team_id: nullcontext(),
            registry=types.SimpleNamespace(binding=lambda *_args: binding),
            assistant_icons=types.SimpleNamespace(read=mock.Mock(side_effect=icons.AssistantIconError("bad icon"))),
        )
        with self.assertRaisesRegex(ApiProblemError, "icon is unavailable"):
            assistant_api.assistant_icon(controller, "team_1", "helper")

        lifecycle = types.SimpleNamespace(
            _network=lambda _team_id: object(),
            _assistant_filters=lambda _team_id: {},
            _network_name=lambda _team_id: "network",
            _egress_proxy=lambda: object(),
            _validate_container_profile=lambda *_args: (object(), {}),
            _validate_container_egress=lambda *_args: None,
            _has_current_assistant_artifact=lambda *_args: True,
            _admit_assistant_allowed_hosts=lambda *_args: None,
        )
        controller = types.SimpleNamespace(
            _lock=lambda _team_id: nullcontext(),
            assistant_lifecycle=lifecycle,
            client=types.SimpleNamespace(containers=types.SimpleNamespace(list=mock.Mock())),
            registry=types.SimpleNamespace(get_versioned=mock.Mock()),
        )
        controller.client.containers.list.side_effect = DockerException("unavailable")
        with self.assertRaisesRegex(ApiProblemError, "Docker is unavailable"):
            assistant_api.list_assistants(controller, "team_1")

        container = types.SimpleNamespace(labels={assistant_api.ASSISTANT_LABEL: "helper"}, status="running")
        controller.client.containers.list.side_effect = None
        controller.client.containers.list.return_value = [container]
        controller.registry.get_versioned.return_value = None
        with self.assertRaisesRegex(ApiProblemError, "no longer allowlisted"):
            assistant_api.list_assistants(controller, "team_1")

        controller.registry.get_versioned.return_value = (object(), "0.1.0")
        lifecycle._validate_container_egress = mock.Mock(
            side_effect=ApiProblemError(409, "unexpected egress failure", code="unexpected")
        )
        with self.assertRaisesRegex(ApiProblemError, "unexpected egress failure"):
            assistant_api.list_assistants(controller, "team_1")

        lifecycle._validate_container_egress.side_effect = None
        self.assertEqual(
            assistant_api.list_assistants(controller, "team_1"),
            {"assistants": [{"assistant": "helper", "assistant_version": "0.1.0", "status": "running"}]},
        )

    def test_assistant_rpc_maps_absence_encoding_and_readiness_states(self) -> None:
        missing = mock.Mock()
        missing.stop.side_effect = NotFound("missing")
        assistant_rpc._fail_stop_action(types.SimpleNamespace(), missing)

        killed = mock.Mock()
        killed.stop.side_effect = DockerException("stop failed")
        killed.kill.side_effect = NotFound("missing")
        controller = types.SimpleNamespace(_action_not_running=lambda _container: False)
        assistant_rpc._fail_stop_action(controller, killed)

        absent = mock.Mock()
        absent.reload.side_effect = NotFound("missing")
        self.assertTrue(assistant_rpc._action_not_running(absent))

        rpc_controller = types.SimpleNamespace()
        with (
            mock.patch.object(action_execution, "encode_rpc_invocation", side_effect=ValueError("large")),
            self.assertRaisesRegex(ApiProblemError, "request is too large"),
        ):
            assistant_rpc._rpc(rpc_controller, types.SimpleNamespace(), "action", {})

        running = mock.Mock(status="running")
        with mock.patch.object(assistant_rpc.time, "monotonic", side_effect=(0, 0)):
            assistant_rpc._wait_ready(types.SimpleNamespace(), running, object())

        created = mock.Mock(status="created")
        with (
            mock.patch.object(assistant_rpc.time, "monotonic", side_effect=(0, 0, 16)),
            mock.patch.object(assistant_rpc.time, "sleep"),
            self.assertRaisesRegex(ApiProblemError, "did not become ready"),
        ):
            assistant_rpc._wait_ready(types.SimpleNamespace(), created, object())

        exited = mock.Mock(status="exited")
        with (
            mock.patch.object(assistant_rpc.time, "monotonic", side_effect=(0, 0)),
            self.assertRaisesRegex(ApiProblemError, "did not become ready"),
        ):
            assistant_rpc._wait_ready(types.SimpleNamespace(), exited, object())


if __name__ == "__main__":
    unittest.main()
