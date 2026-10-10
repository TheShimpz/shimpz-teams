"""Team's Supervisor-controlled confirmation of mutating chat Actions and the input every confirmation card shows."""

import sys
import tempfile
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LOOKUP_INPUT, LOOKUP_RESULT, LocalContractCase, chat_body

from action import confirmation as action_confirmation
from action import human as action_human
from inference import client as brain_runtime_client
from inference import config as inference_config
from local import audit as local_audit
from local.chat import continuation as local_chat_continuations
from local.chat import segment as local_chat_segment
from local.errors import ApiProblemError
from tests import human_request_fixtures

CHANGE = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)


class Runtime:
    purpose = staticmethod(lambda *_args: None)

    def __init__(self) -> None:
        self.resumes = 0

    def start(self, _context, _message, *, conversation=()):
        return brain_runtime_client.RuntimeTurn("action-required", "", (CHANGE,))

    def resume(self, _context, results):
        self.resumes += 1
        if results != {"action-1": LOOKUP_RESULT}:
            raise AssertionError("the confirmed result changed")
        return brain_runtime_client.RuntimeTurn("completed", "Changed", ())


def _chat(controller) -> dict[str, object]:
    body = chat_body("Change it", assistant_ids=["shimpz-cloudflare"])
    return controller.chat_turn_service.chat("team_1", body, "openai", "sk-test-0123456789")


def _resume(controller, challenge_id: str, decision: str = "submit") -> dict[str, object]:
    body = {"challenge_id": challenge_id, "decision": decision}
    if decision == "submit":
        body["value"] = True
    return controller.chat_turn_service.resume_chat_human("team_1", body, "openai", "sk-test-0123456789")


class LocalActionConfirmationTests(LocalContractCase):
    def _controller(self, directory: str, *, human_requests: tuple[str, ...] = (), effect: str = "mutating"):
        runtime = Runtime()
        controller = self._chat_controller(directory, runtime)
        spec = controller.registry["shimpz-cloudflare"]
        action = replace(spec.actions["list-zones"], effect=effect, human_requests=human_requests)
        controller.registry["shimpz-cloudflare"] = replace(spec, actions={**spec.actions, "list-zones": action})
        invocations: list[object] = []

        def invoke(*args):
            invocations.append(args[4])
            return {"result": LOOKUP_RESULT}

        controller.assistant_lifecycle.invoke = invoke
        return controller, runtime, invocations

    def test_a_mutating_action_without_authorization_waits_for_the_supervisor_before_it_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _runtime, invocations = self._controller(directory)
            with mock.patch.object(local_audit, "record_request", return_value="a" * 32):
                paused = _chat(controller)
                self.assertEqual(invocations, [])
                completed = _resume(controller, paused["challenge_id"])

        self.assertEqual(paused["status"], "human-required")
        request = paused["request"]
        self.assertEqual(
            (request["kind"], request["ordinal"], request["policy"]), ("confirmation", 0, "mutating-actions")
        )
        self.assertEqual(paused["rendered"], {})
        self.assertEqual(
            paused["input"],
            {
                "fields": [
                    {"name": "page", "value": "1", "truncated": False},
                    {"name": "per_page", "value": "25", "truncated": False},
                ],
                "omitted": 0,
            },
        )
        self.assertEqual(completed["reply"], "Changed")
        self.assertEqual(len(invocations), 1)
        # The workload never sees Team's confirmation in its replay transcript.
        self.assertEqual(invocations[0].transcript.payloads(), ())
        self.assertEqual(invocations[0].transcript.confirmation.kind, "confirmation")

    def test_a_denied_confirmation_never_runs_the_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, runtime, invocations = self._controller(directory)
            with mock.patch.object(local_audit, "record_request", return_value="a" * 32):
                paused = _chat(controller)
                denied = _resume(controller, paused["challenge_id"], "deny")

        self.assertEqual(denied["status"], "human-denied")
        self.assertEqual((invocations, runtime.resumes), ([], 0))

    def test_the_supervisor_can_turn_the_confirmation_off_for_a_team(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _runtime, invocations = self._controller(directory)
            with mock.patch.object(local_audit, "record_request", return_value="a" * 32) as audit:
                self.assertEqual(
                    controller.action_confirmation_status("team_1"), {"team_id": "team_1", "confirm_mutating": True}
                )
                self.assertEqual(
                    controller.configure_action_confirmation("team_1", {"confirm_mutating": False}),
                    {"team_id": "team_1", "confirm_mutating": False},
                )
                completed = _chat(controller)
            self.assertEqual(
                controller.action_confirmation_status("team_1"), {"team_id": "team_1", "confirm_mutating": False}
            )

        self.assertEqual(completed["reply"], "Changed")
        self.assertEqual(len(invocations), 1)
        audit.assert_any_call("action-confirmation", result="ok", team_id="team_1", detail="disabled")

    def test_an_action_with_its_own_authorization_and_a_read_only_action_keep_one_ceremony(self) -> None:
        for human_requests, effect in ((("approval",), "mutating"), ((), "read_only")):
            with self.subTest(effect=effect), tempfile.TemporaryDirectory() as directory:
                controller, _runtime, invocations = self._controller(
                    directory, human_requests=human_requests, effect=effect
                )
                with mock.patch.object(local_audit, "record_request", return_value="a" * 32):
                    completed = _chat(controller)
                self.assertEqual(completed["reply"], "Changed")
                self.assertEqual(len(invocations), 1)

    def test_a_declared_authorization_card_shows_the_validated_input(self) -> None:
        admitted = human_request_fixtures.list_zones_approval()
        with tempfile.TemporaryDirectory() as directory:
            controller, _runtime, _invocations = self._controller(directory, human_requests=("approval",))
            controller.assistant_lifecycle.invoke = lambda *_args: (_ for _ in ()).throw(
                action_human.HumanRequestSuspensionError(admitted)
            )
            with mock.patch.object(local_audit, "record_request", return_value="a" * 32):
                paused = _chat(controller)

        self.assertEqual(paused["request"]["kind"], "approval")
        self.assertEqual([field["name"] for field in paused["input"]["fields"]], ["page", "per_page"])

    def test_an_unreadable_setting_or_another_calls_confirmation_never_runs_the_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _runtime, invocations = self._controller(directory)
            active = mock.Mock()
            active.spec = controller.registry["shimpz-cloudflare"]
            active.container_id = "container"
            other = action_confirmation.request(
                "team_1", ("shimpz-cloudflare", active.spec.image, "container"), "list-zones", "action-1", {"page": 2}
            )
            confirmed = action_human.ActionTranscript("action-1").confirm(other, True)
            with self.assertRaises(local_chat_segment.chat_orchestrator.ChatOrchestrationError) as refused:
                local_chat_segment._confirm_before_run(controller, "team_1", active, CHANGE, confirmed)
            self.assertTrue(local_chat_segment.action_dispatch.never_dispatched(refused.exception))
            with (
                mock.patch.object(
                    controller.inference_store,
                    "load_action_confirmation",
                    side_effect=inference_config.InferenceConfigError("unavailable"),
                ),
                self.assertRaises(local_chat_segment.chat_orchestrator.ChatOrchestrationError) as unavailable,
            ):
                local_chat_segment._confirm_before_run(controller, "team_1", active, CHANGE, confirmed)
            self.assertTrue(local_chat_segment.action_dispatch.never_dispatched(unavailable.exception))
        self.assertEqual(invocations, [])

    def test_the_setting_admits_only_a_boolean_and_fails_closed_on_its_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _runtime, _invocations = self._controller(directory)
            for body in ({}, {"confirm_mutating": 1}, {"confirm_mutating": True, "extra": 1}, None):
                with self.subTest(body=body), self.assertRaises(ApiProblemError) as invalid:
                    controller.configure_action_confirmation("team_1", body)
                self.assertEqual(invalid.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)
            failing = mock.Mock(side_effect=inference_config.InferenceConfigError("unavailable"))
            for name, call in (
                ("load_action_confirmation", lambda: controller.action_confirmation_status("team_1")),
                (
                    "save_action_confirmation",
                    lambda: controller.configure_action_confirmation("team_1", {"confirm_mutating": True}),
                ),
            ):
                with (
                    self.subTest(name=name),
                    mock.patch.object(controller.inference_store, name, failing),
                    self.assertRaises(ApiProblemError) as unavailable,
                ):
                    call()
                self.assertEqual(unavailable.exception.code, "inference-store-failed")

    def test_an_input_that_cannot_be_shown_never_opens_a_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _runtime, invocations = self._controller(directory)
            with (
                mock.patch.object(local_audit, "record_request", return_value="a" * 32),
                mock.patch.object(action_confirmation, "input_projection", side_effect=ValueError("unshowable")),
                self.assertRaises(ApiProblemError),
            ):
                _chat(controller)
            self.assertIsNone(controller.chat_turn_service.human_challenges.current("team_1"))
        self.assertEqual(invocations, [])

    def test_a_paused_confirmation_survives_its_continuation_codec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller, _runtime, _invocations = self._controller(directory)
            with mock.patch.object(local_audit, "record_request", return_value="a" * 32):
                paused = _chat(controller)
            stored = controller.chat_continuations.current("team_1")
            decoded = local_chat_continuations.decode(stored)

        requirement = decoded.requirements[0]
        self.assertEqual(requirement.request.kind, "confirmation")
        self.assertEqual(requirement.request.fingerprint, paused["request"]["fingerprint"])
        self.assertEqual(dict(requirement.input), paused["input"])
