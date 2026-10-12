"""The validated input a chat card of an Action's declared authorization shows (ADR-0115)."""

import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))
from local_controller_harness import LOOKUP_INPUT, LocalContractCase, chat_body

from action import challenges as action_challenges
from action import human as action_human
from inference import client as brain_runtime_client
from local import audit as local_audit
from local.chat import continuation as local_chat_continuations
from local.errors import ApiProblemError
from tests import human_request_fixtures

CHANGE = brain_runtime_client.ActionRequest("action-1", "shimpz-cloudflare", "list-zones", LOOKUP_INPUT)


class Runtime:
    purpose = staticmethod(lambda *_args: None)

    def start(self, _context, _message, *, conversation=()):
        return brain_runtime_client.RuntimeTurn("action-required", "", (CHANGE,))


def _chat(controller) -> dict[str, object]:
    body = chat_body("Change it", assistant_ids=["shimpz-cloudflare"])
    return controller.chat_turn_service.chat("team_1", body, "openai", "sk-test-0123456789")


class LocalAuthorizationInputTests(LocalContractCase):
    def _controller(self, directory: str, invocations: list[object]):
        controller = self._chat_controller(directory, Runtime())
        spec = controller.registry["shimpz-cloudflare"]
        action = replace(spec.actions["list-zones"], effect="mutating", human_requests=("approval",))
        controller.registry["shimpz-cloudflare"] = replace(spec, actions={**spec.actions, "list-zones": action})
        admitted = human_request_fixtures.list_zones_approval()

        def invoke(*args):
            invocations.append(args[4])
            raise action_human.HumanRequestSuspensionError(admitted)

        controller.assistant_lifecycle.invoke = invoke
        return controller

    def test_a_declared_authorization_card_shows_the_validated_input_and_keeps_it_while_paused(self) -> None:
        invocations: list[object] = []
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, invocations)
            with mock.patch.object(local_audit, "record_request", return_value="a" * 32):
                paused = _chat(controller)
            decoded = local_chat_continuations.decode(controller.chat_continuations.current("team_1"))

        self.assertEqual(paused["request"]["kind"], "approval")
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
        # The Action asked for its own authorization, so its workload ran once to ask it; Team adds no ceremony.
        self.assertEqual(len(invocations), 1)
        self.assertEqual(dict(decoded.requirements[0].input), paused["input"])

    def test_an_input_that_cannot_be_shown_never_opens_the_card(self) -> None:
        invocations: list[object] = []
        with tempfile.TemporaryDirectory() as directory:
            controller = self._controller(directory, invocations)
            with (
                mock.patch.object(local_audit, "record_request", return_value="a" * 32),
                mock.patch.object(action_challenges, "input_projection", side_effect=ValueError("unshowable")),
                self.assertRaises(ApiProblemError),
            ):
                _chat(controller)
            self.assertIsNone(controller.chat_turn_service.human_challenges.current("team_1"))
